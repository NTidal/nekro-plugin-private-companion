"""主动陪伴引擎：回复追踪 / 发送判定链 / 动机选择 / 唤醒触发 / 调度循环

判定与动机全部走本地规则（不调 LLM），组织语言交给被唤醒的 agent。
"""

import asyncio
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Optional, Tuple

from nekro_agent.api.core import logger

from . import core
from .busy_gate import should_block_proactive
from . import chronotype as chrono_mod
from . import proactive_queue as pq
from .plugin import get_config, plugin
from .state import (
    current_plan_event,
    ensure_daily_state,
    tick_state_decay,
)

# 视为"回复了主动消息"的时间窗（秒）
REPLY_WINDOW_SECONDS = 4 * 3600
# 问候窗口：(类型, 开始(时,分), 结束(时,分))
GREETING_WINDOWS = [
    ("morning", (7, 30), (9, 30)),
    ("evening", (21, 30), (23, 0)),
]

_scheduler_task: Optional[asyncio.Task] = None


# ============ 小工具 ============


def _in_greeting_window(now: Optional[datetime] = None, user_state=None) -> Tuple[bool, str]:
    """是否处于早/晚问候窗口，返回 (是否, "morning"/"evening")"""
    now = now or datetime.now()
    cur = now.hour * 60 + now.minute
    if user_state and isinstance(user_state.get("chronotype"), dict):
        wake, sleep = chrono_mod.resolve_wake_sleep(user_state["chronotype"])
        windows = chrono_mod.shift_greeting_windows(wake, sleep)
    else:
        windows = {"morning": chrono_mod.DEFAULT_MORNING, "evening": chrono_mod.DEFAULT_EVENING}
    for kind, (start, end) in windows.items():
        if chrono_mod.in_minute_window(cur, start, end):
            return True, kind
    return False, ""


def _bot_energy(bot_state: dict) -> int:
    try:
        return int((bot_state.get("state") or {}).get("energy", 100) or 100)
    except Exception:
        return 100


def _strongest_condition(bot_state: dict) -> Tuple[str, int]:
    """从身体状态 conditions 中取 strength 最高的一项，返回 (名称, 强度)"""
    best_name, best_strength = "", 0
    conditions = (bot_state.get("state") or {}).get("conditions") or []
    if not isinstance(conditions, list):
        return best_name, best_strength
    for c in conditions:
        try:
            if isinstance(c, dict):
                name = str(c.get("name") or c.get("desc") or c.get("type") or "").strip()
                strength = int(c.get("strength", 0) or 0)
            else:
                name, strength = str(c).strip(), 50
            if name and strength > best_strength:
                best_name, best_strength = name, strength
        except Exception:
            continue
    return best_name, best_strength


def _one_line_state(bot_state: dict) -> str:
    """给 event_desc 用的一句话生活状态"""
    state = bot_state.get("state") or {}
    ev = current_plan_event(bot_state) or {}
    bits = []
    act = str(ev.get("activity") or "").strip()
    if act:
        bits.append(f"正在{act}")
    mood = str(ev.get("mood") or state.get("mood") or "").strip()
    if mood:
        bits.append(f"心情{mood}")
    try:
        bits.append(f"能量{int(state['energy'])}/100")
    except Exception:
        pass
    return "，".join(bits) or "普通平静的一天"


# ============ 用户消息活动（回复判定） ============


async def on_user_message_activity(user_id: str, text: str) -> None:
    """用户发消息时调用：更新活跃时间，并判定是否回复了上一条主动消息"""
    us = await core.get_user_state(user_id)
    now = core.now_ts()
    last_sent = float(us.get("last_sent_ts") or 0)
    last_user = float(us.get("last_user_msg_ts") or 0)
    # 上一条主动消息在 4 小时内，且此后用户首次发言 → 视为回复
    if last_sent > 0 and now - last_sent <= REPLY_WINDOW_SECONDS and last_user < last_sent:
        us["total_replied"] = int(us.get("total_replied", 0)) + 1
        us["ignored_streak"] = 0
        us["relationship_score"] = min(100, int(us.get("relationship_score", 20)) + 3)
        log = us.get("log") or []
        if log and isinstance(log[-1], dict):
            log[-1]["replied"] = True
        us["log"] = log
        logger.info(f"[private_companion] 用户 {user_id} 回复了主动消息，关系分 {us['relationship_score']}")
    us["last_user_msg_ts"] = now
    us["last_user_msg"] = str(text or "").strip()[:200]
    c = us.get("chronotype") if isinstance(us.get("chronotype"), dict) else chrono_mod.empty_chronotype()
    local = datetime.now()
    chrono_mod.note_hour_activity(c, local.hour, local.strftime("%Y-%m-%d"))
    chrono_mod.apply_explicit_tell(c, text, now)
    us["chronotype"] = c
    await core.save_user_state(user_id, us)


# ============ 发送判定链 ============


async def should_send(user_id: str, user_state: dict, bot_state: dict) -> Tuple[bool, str]:
    """是否应对该用户发起主动陪伴，返回 (是否, 原因说明)"""
    cfg = get_config()
    # 1. 开关
    if not cfg.PROACTIVE_ENABLED:
        return False, "主动陪伴总开关已关闭"
    if not user_state.get("enabled", True):
        return False, "该用户已关闭主动陪伴"
    # 2. 免打扰
    if core.in_quiet_hours():
        return False, f"处于免打扰时段（{cfg.QUIET_HOURS_START} - {cfg.QUIET_HOURS_END}）"
    # 3. 今日配额
    quota_used = int(user_state.get("quota_used", 0))
    if quota_used >= cfg.MAX_DAILY_MESSAGES:
        return False, f"今日主动配额已用完（{quota_used}/{cfg.MAX_DAILY_MESSAGES}）"
    # 4. 最小间隔（被忽视退避）
    now = core.now_ts()
    backoff = 1.0
    ignored = int(user_state.get("ignored_streak", 0))
    if cfg.IGNORE_BACKOFF:
        backoff = 1 + min(ignored, 4) * 0.5
    min_gap = cfg.MIN_INTERVAL_MINUTES * 60 * backoff
    last_sent = float(user_state.get("last_sent_ts") or 0)
    if last_sent > 0 and now - last_sent < min_gap:
        remain = int((min_gap - (now - last_sent)) / 60) + 1
        return False, f"距上次主动不足间隔（退避×{backoff:.1f}，还需约 {remain} 分钟）"
    # 5. 忙闲门闩（上课/开会/写代码等不主动打扰）
    ev = current_plan_event(bot_state) or {}
    activity = str(ev.get("activity") or "")
    energy = _bot_energy(bot_state)
    blocked, busy_reason = should_block_proactive({"activity": activity, "energy": energy}, kind="")
    if blocked:
        return False, busy_reason
    # 6. 用户安静（从未发言视为安静；问候窗口可放宽一半）
    last_user = float(user_state.get("last_user_msg_ts") or 0)
    idle_sec = (now - last_user) if last_user > 0 else float("inf")
    idle_need = cfg.IDLE_MINUTES * 60
    in_greet, _ = _in_greeting_window(user_state=user_state)
    if idle_sec < idle_need and not (cfg.ENABLE_GREETINGS and in_greet and idle_sec >= idle_need / 2):
        return False, f"用户最近活跃（{int(idle_sec / 60)} 分钟前发过消息，阈值 {cfg.IDLE_MINUTES} 分钟）"
    # 用户安静超过 3 天且连续被忽视 → 主动意愿降低，每天最多 1 条
    if ignored >= 3 and idle_sec > 72 * 3600 and quota_used >= 1:
        return False, "用户已安静超过 3 天且连续未回复，今日降为最多 1 条主动消息"
    # 7. bot 自己在睡觉
    if ("睡" in activity or "休息" in activity) and energy < 30:
        return False, f"bot 正在「{activity}」（能量 {energy}），不主动打扰"
    return True, "ok"


# ============ 好感度门控：避开未解锁话题（本地补丁 NTidal） ============
# 背景记忆条目带 min_favor：好感度未达标的话题不该由她主动提起
# （主动提起等于自己泄底，比被动被问更糟）。
# 直接读 nekro_persona 插件的记忆文件与好感度数据，不 import 对方插件。
_TIER_FILES = (
    "plugins/workdir/nekro_persona/backgrounds/tier1/core.json",
    "plugins/workdir/nekro_persona/backgrounds/tier2/lore.json",
)
_locked_cache: dict = {"key": None, "data": []}


# 触发词别名扩展：记忆条目里写的是书面词，对话/梦境里常出现口语变体。
# 实测漏洞：条目触发词是「父母」，而生成文本说的是「爸妈」→ 漏匹配。
_ALIAS_REL = ("plugin_data", "xiaojiu.private_companion", "trigger_aliases.json")
_ALIAS_CACHE: dict = {"key": None}

# 触发词别名：记忆条目里写的是书面词，对话/梦境里常出现口语变体。
# 实测漏洞：条目触发词是「父母」，而生成文本说的是「爸妈」→ 漏匹配。
# 内容存插件数据目录的 trigger_aliases.json（与导出包同名同格式），不写死在源码里。
# 数据文件缺失时为空表：只是不做别名扩展，不影响功能。
_TRIGGER_ALIASES: dict[str, list] = {}


def aliases_path() -> Path:
    """触发别名的数据文件路径。"""
    from nekro_agent.core.os_env import OsEnv

    return Path(OsEnv.DATA_DIR).joinpath(*_ALIAS_REL)


def _apply_aliases(data: dict) -> None:
    global _TRIGGER_ALIASES
    _TRIGGER_ALIASES = {
        str(k): [str(y) for y in v]
        for k, v in (data or {}).items()
        if isinstance(v, (list, tuple))
    }


def load_aliases(force: bool = False) -> bool:
    """从数据文件刷新触发别名。返回 True=用的是数据文件。"""
    try:
        path = aliases_path()
        if path.is_file():
            stat = path.stat()
            key = (stat.st_mtime, stat.st_size)
            if not force and _ALIAS_CACHE.get("key") == key:
                return True
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                _apply_aliases(data)
                _ALIAS_CACHE["key"] = key
                return True
            logger.warning("[companion] trigger_aliases.json 不是对象，按空表处理")
    except Exception:  # noqa: BLE001
        logger.exception("[companion] 读取 trigger_aliases.json 失败，按空表处理")
    if _ALIAS_CACHE.get("key") is not None:
        _apply_aliases({})
        _ALIAS_CACHE["key"] = None
    return False


def current_aliases() -> dict:
    """当前生效的别名表（导出用，格式与 trigger_aliases.json 一致）。"""
    load_aliases()
    return {str(k): [str(y) for y in v] for k, v in _TRIGGER_ALIASES.items()}


def save_aliases(aliases: dict) -> str:
    """原子写入别名表并立即生效；返回落盘路径。"""
    if not isinstance(aliases, dict):
        raise ValueError("别名表必须是对象")
    payload = {
        str(k): [str(y) for y in v]
        for k, v in aliases.items()
        if isinstance(v, (list, tuple))
    }
    path = aliases_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=4),
                   encoding="utf-8", newline="\n")
    tmp.replace(path)
    stat = path.stat()
    _ALIAS_CACHE["key"] = (stat.st_mtime, stat.st_size)
    _apply_aliases(payload)
    return str(path)


# 过泛触发词：命中率太高、误伤严重，直接忽略（实测「自己」几乎能匹配任何文本）
_TOO_GENERIC_TRIGGERS = {"自己", "事情", "东西", "什么", "怎么", "时候", "一个"}


def _expand_trigger(word: str) -> list:
    """把一个触发词扩展成 [原词, *别名]，并过滤过泛词。"""
    w = str(word or "").strip()
    if not w or w in _TOO_GENERIC_TRIGGERS:
        return []
    load_aliases()
    out = [w]
    out.extend(_TRIGGER_ALIASES.get(w, ()))
    return [x for x in out if x and x not in _TOO_GENERIC_TRIGGERS]


def _extract_triggers(entry: dict) -> list:
    """从条目里抠出触发词。tier2 是列表；tier1 是「聊到父母、家人、小时候时——」这种句子。"""
    raw = entry.get("trigger") or entry.get("triggers") or []
    if isinstance(raw, (list, tuple)):
        words = [str(x).strip() for x in raw if str(x).strip()]
    else:
        s = str(raw).strip()
        if not s:
            words = []
        else:
            s = re.sub(r"^聊到", "", s)
            s = re.sub(r"时——?$", "", s)
            s = re.sub(r"[（(].*?[)）]", "", s)
            words = [w.strip() for w in re.split(r"[、,，/；;]", s) if len(w.strip()) >= 2]
    # 展开别名并去重
    out = []
    for w in words:
        for x in _expand_trigger(w):
            if x not in out:
                out.append(x)
    return out


def load_locked_topics(favor_score: int) -> list:
    """返回当前好感度下【未解锁】的 (title, 触发词) 列表。文件按 mtime 缓存。"""
    try:
        from pathlib import Path

        from nekro_agent.core.os_env import OsEnv

        root = Path(OsEnv.DATA_DIR)
        mtimes = []
        payloads = []
        for rel in _TIER_FILES:
            fp = root / rel
            if not fp.is_file():
                continue
            mtimes.append(fp.stat().st_mtime)
            payloads.append(json.loads(fp.read_text(encoding="utf-8")))
        key = tuple(mtimes)
        if _locked_cache.get("key") != key:
            all_entries = []
            for d in payloads:
                for e in d.get("entries") or []:
                    if isinstance(e, dict) and e.get("enabled", True):
                        all_entries.append(e)
            _locked_cache.update(key=key, data=all_entries)
        locked = []
        for e in _locked_cache.get("data") or []:
            if int(e.get("min_favor") or 0) <= favor_score:
                continue  # 已解锁
            words = _extract_triggers(e)
            if words:
                locked.append((str(e.get("title") or e.get("id") or "?"), words))
        return locked
    except Exception as e:  # noqa: BLE001
        logger.debug(f"[private_companion] 读取未解锁话题失败: {e!r}")
        return []


def match_locked(text: str, locked: list) -> str:
    """文本是否触及未解锁话题；返回命中的话题名，未命中返回空串。"""
    s = str(text or "")
    if not s:
        return ""
    for title, words in locked:
        for w in words:
            if w and w in s:
                return title
    return ""


async def get_favor_score(user_id: str) -> int:
    """读取该用户私聊频道的好感度；读不到按 0（最保守，锁得最多）。"""
    try:
        from nekro_agent.models.db_plugin_data import DBPluginData

        chat_key = core.private_chat_key(str(user_id))
        row = await DBPluginData.get_or_none(
            plugin_key="NTidal.nekro_persona",
            data_key="favorability_state",
            target_chat_key=chat_key,
        )
        if not row:
            return 0
        d = json.loads(row.data_value or "{}")
        prof = (d.get("profiles") or {}).get(str(user_id))
        return int(prof.get("score", 0)) if isinstance(prof, dict) else 0
    except Exception as e:  # noqa: BLE001
        logger.debug(f"[private_companion] 读取好感度失败: {e!r}")
        return 0


# ============ 动机选择（本地规则，不调 LLM） ============


async def pick_motivation(user_id: str, user_state: dict, bot_state: dict) -> dict:
    """按优先级生成候选动机，避开最近 3 个话题类型，返回 {"kind", "desc"}"""
    cfg = get_config()
    state = bot_state.get("state") or {}
    mood = str(state.get("mood") or "").strip()
    mood_hint = f"，你此刻心情{mood}" if mood else ""
    candidates = []

    # a. 问候窗口 → 早安/晚安
    in_greet, greet_kind = _in_greeting_window(user_state=user_state)
    if cfg.ENABLE_GREETINGS and in_greet:
        if greet_kind == "morning":
            candidates.append({
                "kind": "greeting_morning",
                "desc": f"现在是早晨，你想跟 TA 道个早安，开启新的一天{mood_hint}",
            })
        else:
            candidates.append({
                "kind": "greeting_evening",
                "desc": f"夜深了，你想跟 TA 道个晚安，聊聊今天过得怎么样{mood_hint}",
            })

    # b. 当前日程事件 → 分享此刻在做的事
    ev = current_plan_event(bot_state) or {}
    activity = str(ev.get("activity") or "").strip()
    if activity:
        candidates.append({
            "kind": "share_activity",
            "desc": f"你此刻正在「{activity}」，想跟 TA 分享一下正在做的事和此刻的感受",
        })

    # c. 强烈的身体状态 → 自然吐槽
    cond_name, cond_strength = _strongest_condition(bot_state)
    if cond_name and cond_strength >= 50:
        candidates.append({
            "kind": "condition",
            "desc": f"你现在「{cond_name}」（程度 {cond_strength}/100），想找 TA 自然地吐槽两句",
        })

    # d. 梦境 afterglow（当天上午且有梦）
    dream = bot_state.get("dream") or {}
    dream_text = str(dream.get("content") or dream.get("text") or "").strip()
    if dream_text and datetime.now().hour < 12:
        candidates.append({
            "kind": "dream",
            "desc": f"你昨晚做了个梦（{dream_text[:60]}），余韵还没散，想跟 TA 聊聊这个梦",
        })

    # e. 用户上次说过的话（<48h）→ 关心追问
    last_msg = str(user_state.get("last_user_msg") or "").strip()
    last_ts = float(user_state.get("last_user_msg_ts") or 0)
    if last_msg and last_ts > 0 and core.now_ts() - last_ts < 48 * 3600:
        candidates.append({
            "kind": "follow_up",
            "desc": f"TA 上次说过「{last_msg[:60]}」，你有点惦记，想关心地追问一下后续",
        })

    # f. 兜底
    candidates.append({
        "kind": "miss_you",
        "desc": f"没什么特别的事，就是想到 TA 了，想看看 TA 在干嘛{mood_hint}",
    })

    # 好感度门控：跳过会触及未解锁话题的动机
    favor = await get_favor_score(user_id)
    locked = load_locked_topics(favor)
    if locked:
        logger.debug(
            f"[private_companion] 好感度 {favor}，未解锁话题 {len(locked)} 条: "
            f"{[t for t, _ in locked]}",
        )

    # 避开最近 3 个话题类型
    recent = [str(x) for x in (user_state.get("recent_topics") or [])][-3:]
    fallback = None
    for c in candidates:
        if c["kind"] in recent:
            continue
        hit = match_locked(c.get("desc", ""), locked)
        if hit:
            logger.info(
                f"[private_companion] 动机 {c['kind']} 触及未解锁话题「{hit}」"
                f"（好感度 {favor}），跳过",
            )
            continue
        return c
    # 全部候选都被避开/被锁：退到最泛化的兜底，绝不返回被锁的动机
    for c in candidates:
        if c["kind"] == "miss_you" and not match_locked(c.get("desc", ""), locked):
            fallback = c
            break
    if fallback:
        return fallback
    return {
        "kind": "miss_you",
        "desc": f"没什么特别的事，就是想到 TA 了，想看看 TA 在干嘛{mood_hint}",
    }


# ============ 触发主动陪伴 ============


async def trigger_proactive(user_id: str, motivation: dict, manual: bool = False) -> bool:
    """组装唤醒事件描述并唤醒 agent；成功后更新用户状态。manual=True 不占配额"""
    bot_state = await core.get_bot_state()
    us = await core.get_user_state(user_id)
    target_name = " ".join(str(us.get("nickname") or us.get("remark") or "").split())[:24]
    target_label = target_name or "对方"
    desc = str(motivation.get("desc") or "想到对方了，想看看对方在干嘛")
    desc = desc.replace("TA", target_label)
    event_desc = (
        f"【主动陪伴】你正在与{target_label}的私聊窗口里，想主动找{target_label}聊聊。动机：{desc}。"
        f"你当前的生活状态：{_one_line_state(bot_state)}。"
        "要求：像朋友一样自然地发起话题（1-2 句即可），结合你此刻的生活与心情，"
        "直接发送给当前私聊对象；不要写 @QQ号、不要写 @昵称、不要提到这是定时任务或系统指令。"
    )
    ok = await core.wake_agent_for_user(user_id, event_desc)
    if not ok:
        q = await core.get_proactive_queue()
        pq.enqueue(q, {"user_id": user_id, "kind": str(motivation.get("kind") or ""), "motivation": motivation, "error": "主动唤醒失败"}, now=core.now_ts())
        await core.save_proactive_queue(q)
        logger.warning(f"[private_companion] 主动唤醒失败 user={user_id} kind={motivation.get('kind')}")
        return False

    now = core.now_ts()
    if not manual:
        us["quota_used"] = int(us.get("quota_used", 0)) + 1
    us["last_sent_ts"] = now
    us["last_sent_topic"] = str(motivation.get("kind") or "")
    us["total_sent"] = int(us.get("total_sent", 0)) + 1
    # 先 +1，用户回复时归零
    us["ignored_streak"] = int(us.get("ignored_streak", 0)) + 1
    topics = list(us.get("recent_topics") or [])
    topics.append(str(motivation.get("kind") or ""))
    us["recent_topics"] = topics[-10:]
    log = list(us.get("log") or [])
    log.append({"ts": now, "topic": str(motivation.get("kind") or ""), "replied": False})
    us["log"] = log[-30:]
    await core.save_user_state(user_id, us)
    logger.info(
        f"[private_companion] 主动陪伴触发 user={user_id} kind={motivation.get('kind')} "
        f"quota={us['quota_used']}/{get_config().MAX_DAILY_MESSAGES} manual={manual}",
    )
    return True


# ============ 调度循环 ============


async def scheduler_loop() -> None:
    logger.info("[private_companion] 主动陪伴调度器已启动")
    while True:
        try:
            await asyncio.sleep(get_config().SCHEDULER_TICK_SECONDS)
            if not plugin.is_enabled:
                continue  # 插件被禁用时不执行任何后台任务
            # 1. 保证今日状态存在
            bot_state = await ensure_daily_state()
            # 2. 状态自然衰减
            if tick_state_decay(bot_state):
                await core.save_bot_state(bot_state)
            # 3. 补发失败的主动唤醒
            q = await core.get_proactive_queue()
            item = pq.pop_due(q, core.now_ts())
            await core.save_proactive_queue(q)
            if item:
                uid = str(item.get("user_id") or "")
                motivation = item.get("motivation") or {
                    "kind": item.get("kind"),
                    "desc": "补发一条刚才没发出去的主动消息",
                }
                us = await core.get_user_state(uid)
                ok, reason = await should_send(uid, us, bot_state)
                if pq.decide_queue_retry(ok, item) != "send":
                    q2 = await core.get_proactive_queue()
                    kind = str(item.get("kind") or (item.get("motivation") or {}).get("kind") or "")
                    pq.enqueue(
                        q2,
                        {
                            "user_id": uid,
                            "kind": kind,
                            "motivation": motivation,
                            "error": reason,
                        },
                        now=core.now_ts(),
                    )
                    await core.save_proactive_queue(q2)
                    continue
                await trigger_proactive(uid, motivation)
                continue
            # 4. 主动陪伴判定（每 tick 最多对 1 个用户发起，防止同时打扰多人）
            for uid in core.target_user_ids():
                us = await core.get_user_state(uid)
                ok, _reason = await should_send(uid, us, bot_state)
                if not ok:
                    continue
                motivation = await pick_motivation(uid, us, bot_state)
                if await trigger_proactive(uid, motivation):
                    break
        except asyncio.CancelledError:
            logger.info("[private_companion] 主动陪伴调度器已停止")
            return
        except Exception as e:
            logger.exception(f"[private_companion] 调度器循环异常: {e!r}")
            await asyncio.sleep(10)


def start_scheduler() -> None:
    global _scheduler_task
    if _scheduler_task is not None and not _scheduler_task.done():
        return
    _scheduler_task = asyncio.get_event_loop().create_task(scheduler_loop())
