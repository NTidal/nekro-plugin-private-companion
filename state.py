"""生活状态机：每日日程 / 能量心情 / 梦境

借鉴 astrbot_plugin_private_companion 的提示词设计精华（具体可感、不要 AI 腔、
日程区分工作日/休息日、梦从生活碎片里长出来），但只保留精简骨架：
- 每天首次活动时跨天滚动：生成昨夜梦境 -> 生成今日日程 -> 生成今日状态
- 状态衰减由调度器 tick 驱动，纯本地计算不耗 token

本地精简（NTidal）：已移除日记生成与定时日记。
"""

import asyncio
import random
import re
from datetime import date, datetime
from typing import List, Optional, Tuple

from nekro_agent.api.core import logger

from . import core
from . import day_card as dc
from . import plan_diversity as pd
from .core import (
    get_bot_state,
    get_persona_prompt,
    world_stage_block,
    hhmm_now,
    llm_call,
    now_ts,
    parse_hhmm,
    parse_json_loose,
    save_bot_state,
    target_user_ids,
    today_key,
)
from .plugin import get_config, plugin

# ============ 节假日工具 ============

try:
    import chinese_calendar as _cc
    _CC_AVAILABLE = True
except ImportError:
    _cc = None
    _CC_AVAILABLE = False

_WEEKDAY_ZH = "一二三四五六日"


def _get_day_context(dt: Optional[datetime] = None) -> str:
    """返回如「2026-06-15 周一 工作日 14:30」或带节假日名的字符串"""
    now = dt or datetime.now()
    today = now.date()
    wd = _WEEKDAY_ZH[now.weekday()]
    time_str = now.strftime("%H:%M")
    date_str = now.strftime("%Y-%m-%d")

    if _CC_AVAILABLE and _cc is not None:
        try:
            is_holiday = _cc.is_holiday(today)
            is_workday = _cc.is_workday(today)
            detail = _cc.get_holiday_detail(today)
            holiday_name = detail[1] if detail and detail[1] else None

            if is_holiday and holiday_name and now.weekday() < 5:
                day_type = f"法定节假日（{holiday_name}）"
            elif is_holiday:
                day_type = f"假日（{holiday_name}）" if holiday_name else "假日"
            elif is_workday and now.weekday() >= 5:
                day_type = "调休工作日"
            elif is_workday:
                day_type = "工作日"
            else:
                day_type = "周末"
        except Exception:
            day_type = "周末" if now.weekday() >= 5 else "工作日"
    else:
        day_type = "周末" if now.weekday() >= 5 else "工作日"

    return f"{date_str} 周{wd} {day_type} {time_str}"

# 防止并发触发重复的跨天生成
_daily_lock = asyncio.Lock()

# ============ 内部工具 ============

_WINDOW_RE = re.compile(r"^\d{1,2}:\d{2}-\d{1,2}:\d{2}$")


def _weekday_text() -> str:
    return "一二三四五六日"[datetime.now().weekday()]


def _normalize_window(raw: str) -> str:
    """把各种连接符/全角冒号的时间段统一成 HH:MM-HH:MM"""
    text = str(raw or "").strip()
    for ch in ("—", "–", "－", "~", "～", "至", "到"):
        text = text.replace(ch, "-")
    text = text.replace("：", ":").replace(" ", "")
    m = re.search(r"(\d{1,2}):(\d{2})-(\d{1,2}):(\d{2})", text)
    if not m:
        return ""
    sh, sm, eh, em = m.groups()
    return f"{int(sh) % 24:02d}:{sm}-{int(eh) % 24:02d}:{em}"


def _parse_window(window: str) -> Optional[Tuple[int, int]]:
    """解析 HH:MM-HH:MM 为分钟区间；end <= start 视为跨天（end + 24h）"""
    text = _normalize_window(window)
    if not text or "-" not in text:
        return None
    left, right = text.split("-", 1)
    start = parse_hhmm(left)
    end = parse_hhmm(right)
    if not start or not end:
        return None
    s = start[0] * 60 + start[1]
    e = end[0] * 60 + end[1]
    if e <= s:
        e += 24 * 60
    return s, e


def _single_line(text, limit: int = 100) -> str:
    return " ".join(str(text or "").split())[:limit]


# ============ 兜底数据 ============

# 通用居家日程（LLM 失败时使用）
_FALLBACK_PLAN_EVENTS: List[dict] = [
    {"window": "00:00-08:00", "activity": "睡觉", "mood": "安稳"},
    {"window": "08:00-09:30", "activity": "醒来后赖了一会儿床，起来洗漱，去食堂慢慢吃了早饭", "mood": "迷糊,刚开机"},
    {"window": "09:30-12:00", "activity": "在宿舍处理自己手头的事，中途起来倒了杯水", "mood": "平稳"},
    {"window": "12:00-13:30", "activity": "去食堂吃了午饭，回来靠在床边歇了一会儿", "mood": "放松"},
    {"window": "13:30-17:30", "activity": "继续做事，下午有点犯困，泡了杯茶提神", "mood": "专注,偶尔走神"},
    {"window": "17:30-19:30", "activity": "晚饭，顺手把宿舍收拾了一下", "mood": "松弛"},
    {"window": "19:30-23:00", "activity": "窝着用终端翻东西、听曲子，自由时间", "mood": "惬意"},
    {"window": "23:00-00:00", "activity": "洗漱，躺下前又看了会儿终端才睡", "mood": "困意上来"},
]

_FALLBACK_DREAM = {"content": "", "mood": "平静", "afterglow": ""}

# 身体小状态池：(label, kind)
_CONDITION_POOL: List[Tuple[str, str]] = [
    ("微困", "sleepy"),
    ("有点饿", "hungry"),
    ("精神很好", "energetic"),
    ("肩膀有点酸", "sore"),
    ("有点鼻塞", "minor_ill"),
    ("脑子有点钝", "foggy"),
    ("胃口不错", "appetite"),
    ("心情轻快", "light"),
]

_POSITIVE_MOOD_HINTS = ("轻快", "柔和", "温暖", "开心", "平静", "平稳", "放松", "甜", "安稳")
_NEGATIVE_MOOD_HINTS = ("低落", "疲惫", "恍惚", "不安", "慌", "闷", "难过", "委屈", "累")


# ============ 跨天滚动 ============


async def ensure_daily_state() -> dict:
    """确保 bot_state 是今天的；跨天时生成梦境 -> 日程 -> 状态。

    全程不抛异常：任何一步 LLM 失败都有内置兜底数据。
    """
    bot_state = await get_bot_state()
    if bot_state.get("date") == today_key():
        return bot_state
    async with _daily_lock:
        # 拿到锁后重查，避免并发重复生成
        bot_state = await get_bot_state()
        if bot_state.get("date") == today_key():
            return bot_state
        old_date = str(bot_state.get("date") or "")
        try:
            # 1. 昨夜梦境（素材来自昨日日程，须在日程被覆盖前生成）
            await generate_dream()
            # 2. 今日日程
            await generate_daily_plan(force=True)
            # 3. 今日初始状态
            await generate_today_state(force=True)
        except Exception as e:
            logger.error(f"[private_companion] 跨天生成异常: {e!r}")
        # 5. 收尾：无论成败都把日期推进到今天，缺失部分用兜底补齐
        bot_state = await get_bot_state()
        if not bot_state.get("plan", {}).get("events"):
            bot_state["plan"] = {
                "summary": "在家过一个普通安稳的日子",
                "events": [dict(ev) for ev in _FALLBACK_PLAN_EVENTS],
                "generated_at": now_ts(),
            }
        if not bot_state.get("state", {}).get("updated_at"):
            bot_state["state"] = _build_local_state(bot_state)
        if bot_state.get("dream", {}).get("date") != today_key():
            bot_state["dream"] = {"date": today_key(), **_FALLBACK_DREAM}
        bot_state["date"] = today_key()
        await save_bot_state(bot_state)
        logger.info(f"[private_companion] 已生成 {today_key()} 的生活状态（昨日: {old_date or '无'}）")
        return bot_state


# ============ 日程生成 ============


def _events_from_plan_payload(payload: dict) -> list:
    events = []
    for item in payload.get("events") or []:
        if not isinstance(item, dict):
            continue
        window = _normalize_window(str(item.get("window") or item.get("time") or ""))
        activity = _single_line(item.get("activity") or item.get("event"), 120)
        if not window or not _WINDOW_RE.match(window) or not activity:
            continue
        events.append({"window": window, "activity": activity, "mood": _single_line(item.get("mood"), 24)})
    return events


def resolve_plan_segments(
    min_segments: Optional[int] = None,
    max_segments: Optional[int] = None,
) -> Tuple[int, int]:
    """解析日程时段数范围（下限, 上限）。

    优先级：显式传入（WebUI 临时覆盖）> 插件配置；做范围钳制并保证上限 >= 下限。
    """
    cfg = get_config()

    def _clamp(v, default, lo, hi):
        try:
            v = int(v)
        except (TypeError, ValueError):
            v = default
        return max(lo, min(hi, v))

    lo = _clamp(min_segments if min_segments is not None else getattr(cfg, "PLAN_MIN_SEGMENTS", 5), 5, 3, 16)
    hi = _clamp(max_segments if max_segments is not None else getattr(cfg, "PLAN_MAX_SEGMENTS", 8), 8, 3, 20)
    if hi < lo:
        hi = lo
    return lo, hi


def _segment_granularity_hint(lo: int, hi: int) -> str:
    """根据段数给出切分粒度提示（本地补丁 NTidal：强调段长不必相等）

    实测问题：原提示只约束段数，模型就按 24÷段数 均分出等长时段
    （12 段 → 全是 120 分钟，连睡眠都被切成 2 小时一块）。
    真实的一天长短混杂，故此处显式要求段长不等。
    """
    avg_hours = 24.0 / max(1, (lo + hi) / 2)
    if avg_hours <= 1.8:
        daytime = "白天用 0.5-1.5 小时的小段，每段写清一个具体动作"
    elif avg_hours <= 2.6:
        daytime = "白天用 1-2 小时一段"
    elif avg_hours >= 4.5:
        daytime = "白天每段约 4-6 小时，一段里连续做一类事"
    else:
        daytime = "白天每段 2-4 小时，一段里连续做一类事"
    return (
        "**段长必须不等长，不要按 24÷段数 均分**："
        "睡眠段通常一整块 5-8 小时（不要拆成几块），刚醒和睡前可以各有一小段；"
        f"{daytime}；同一件事的连续阶段可以合并成一段。"
        "也不要为了凑段数写空洞的过渡段。"
    )


async def generate_daily_plan(
    force: bool = False,
    min_segments: Optional[int] = None,
    max_segments: Optional[int] = None,
) -> dict:
    """LLM 生成今日日程；失败兜底为通用居家日程。

    min_segments/max_segments 可临时覆盖配置的日程时段数范围（WebUI 重生成时使用）。
    """
    cfg = get_config()
    bot_state = await get_bot_state()
    if not force and bot_state.get("date") == today_key() and bot_state.get("plan", {}).get("events"):
        return bot_state["plan"]
    seg_lo, seg_hi = resolve_plan_segments(min_segments, max_segments)

    persona = await get_persona_prompt()
    dream = bot_state.get("dream", {})
    dream_mood = _single_line(dream.get("mood"), 20) if dream.get("date") == today_key() else ""

    if getattr(cfg, "DAY_CARD_ENABLED", True):
        hist = await core.get_day_card_history()
        existing = bot_state.get("day_card") if isinstance(bot_state.get("day_card"), dict) else None
        if existing and existing.get("date") == today_key() and existing.get("scene"):
            card = existing
        else:
            card = dc.draw_day_card(hist, date_key=today_key())
            dc.remember_card(hist, card)
            await core.save_day_card_history(hist)
    else:
        card = None

    avoid = pd.format_avoid_block(
        pd.recent_activity_lines(bot_state.get("recent_plans") or [], limit_days=3)
    )
    card_block = dc.format_card_for_prompt(card) if card else ""

    system_prompt = (
        "你是一个拟人化 AI 的「生活日程生成器」。你要为下面这个角色安排今天一整天的真实生活，"
        "让她像一个真实存在的人在过日子，而不是在执行模板。\n"
        f"【角色设定】\n{persona or '（无明确设定，按一个作息规律、生活平淡温和的普通人来写）'}"
        + world_stage_block()
    )
    prompt = f"""请为这个角色生成今天的生活日程。

【今天的信息】
日期：{_get_day_context()}
{f'日程风格提示：{cfg.PLAN_STYLE_HINT}' if cfg.PLAN_STYLE_HINT.strip() else ''}
{f'今早醒来的情绪（让上午的节奏受它一点影响）：{dream_mood}' if dream_mood else ''}
{card_block}
{avoid if avoid else ''}
禁止把今天写成上课+写代码的模板日；若场景不是学习/上班，就不要硬塞上课开会。

【要求】
1. 安排 {seg_lo}-{seg_hi} 个时间段（**段长不必相等**），覆盖完整 24 小时（包括睡眠段），window 用 HH:MM-HH:MM，前后衔接不留大空洞。{_segment_granularity_hint(seg_lo, seg_hi)}
2. 先判断今天是工作/学习日还是休息日：周末或节假日不要安排上课、上班、开会这类工作日主线（除非设定明确要求）。
3. activity 写得具体、生活化：写「午休后靠着桌沿醒神」而不是「休息」，写「出门买饮料顺便走了一段」而不是「外出活动」。每段是一小段连续的生活，不是一个几秒钟的动作，也不是任务标签。
4. 允许平淡、磨蹭和「没发生什么」，朴素的安排反而可信；可以自然埋 1 个不起眼的小意外或小惊喜，但别喧宾夺主。
5. mood 用 1-3 个简短中文词，写真实的感受或身体状态（如「轻快,想出门」「平静,专注」「慵懒,不想动」——正面、中性、负面都可以），别只写一个笼统词。
6. summary 是一句话概括今天的整体基调，口语化，别写成总结报告。
7. 不要 AI 腔、不要漂亮但空的句子，先有看得见的动作和场景，情绪贴在上面。
8. **情绪要有起伏**：一天里至少 2-3 段是轻松、愉快或满足的。她底色偏安静，但不是全天难过——真实的一天有烦也有乐，不要把每一段都写成恍惚、走神、发闷、没力气。

只输出纯 JSON，不要 Markdown，不要解释：
{{"summary": "一句话概括今天", "events": [{{"window": "09:00-11:30", "activity": "具体在做什么", "mood": "心情"}}]}}"""

    def _plan_from_raw(raw_text: Optional[str]) -> Optional[dict]:
        if not raw_text:
            return None
        payload = parse_json_loose(raw_text, expect="object")
        if not isinstance(payload, dict):
            return None
        events = _events_from_plan_payload(payload)
        if len(events) < 3:
            return None
        return {
            "summary": _single_line(payload.get("summary"), 80) or "普通的一天",
            "events": events[:seg_hi],
            "generated_at": now_ts(),
        }

    plan: Optional[dict] = None
    raw = await llm_call(prompt, system_prompt=system_prompt, task="daily_plan")
    plan = _plan_from_raw(raw)
    if plan is not None and pd.looks_like_template_day(plan["events"]) and len(plan["events"]) >= 3:
        logger.warning("[private_companion] 日程像模板日，重抽一次")
        retry_prompt = prompt + "\n上次结果是模板日，必须改写"
        raw_retry = await llm_call(retry_prompt, system_prompt=system_prompt, task="daily_plan")
        retried = _plan_from_raw(raw_retry)
        if retried is not None:
            plan = retried
    if plan is None:
        logger.warning("[private_companion] 日程生成失败，使用兜底居家日程")
        plan = {
            "summary": "在家过一个普通安稳的日子",
            "events": [dict(ev) for ev in _FALLBACK_PLAN_EVENTS],
            "generated_at": now_ts(),
        }
    bot_state = await get_bot_state()
    bot_state["plan"] = plan
    bot_state["day_card"] = card if card else bot_state.get("day_card")
    recent = list(bot_state.get("recent_plans") or [])
    recent.append({"date": today_key(), "events": plan["events"]})
    bot_state["recent_plans"] = recent[-7:]
    await save_bot_state(bot_state)
    return plan


# ============ 今日状态生成（纯本地，省 token） ============


def _build_local_state(bot_state: dict) -> dict:
    """随机 + 规则生成今日初始状态，不调 LLM"""
    energy = random.randint(60, 95)
    dream = bot_state.get("dream", {})
    dream_mood = str(dream.get("mood") or "")
    has_dream = bool(str(dream.get("content") or "").strip())
    # 梦境情绪对能量的微调
    if has_dream and dream_mood:
        if any(h in dream_mood for h in _POSITIVE_MOOD_HINTS):
            energy = min(100, energy + random.randint(2, 6))
        elif any(h in dream_mood for h in _NEGATIVE_MOOD_HINTS):
            energy = max(30, energy - random.randint(4, 10))
    # 心情：优先延续梦境余韵，其次取日程首个白天段的 mood，最后按能量随机
    mood = ""
    if has_dream and dream_mood:
        mood = dream_mood
    else:
        for ev in bot_state.get("plan", {}).get("events", []):
            ev_mood = _single_line(ev.get("mood"), 16) if isinstance(ev, dict) else ""
            if ev_mood and "睡" not in str(ev.get("activity", "")):
                mood = ev_mood
                break
    if not mood:
        mood = random.choice(["轻快", "平静", "有点期待"] if energy >= 75 else ["慵懒", "平静", "有点蔫"])
    # 0-2 个身体小状态
    conditions = []
    for label, kind in random.sample(_CONDITION_POOL, k=random.randint(0, 2)):
        conditions.append({
            "label": label,
            "kind": kind,
            "strength": random.randint(1, 3),
            "expires_at": now_ts() + random.randint(2, 6) * 3600,
        })
    return {"energy": energy, "mood": mood, "conditions": conditions, "updated_at": now_ts()}


async def generate_today_state(force: bool = False) -> dict:
    """生成今日初始状态（纯本地随机 + 规则，不耗 token）"""
    bot_state = await get_bot_state()
    if not force and bot_state.get("date") == today_key() and bot_state.get("state", {}).get("updated_at"):
        return bot_state["state"]
    state = _build_local_state(bot_state)
    bot_state = await get_bot_state()
    bot_state["state"] = state
    await save_bot_state(bot_state)
    return state


# ============ 梦境生成 ============


async def generate_dream() -> dict:
    """LLM 生成昨夜梦境；失败返回空梦（= 没记住梦，合理）"""
    bot_state = await get_bot_state()
    persona = await get_persona_prompt()
    # 素材：昨日日程活动
    materials: List[str] = []
    plan_events = bot_state.get("plan", {}).get("events", [])
    activities = [_single_line(ev.get("activity"), 40) for ev in plan_events if isinstance(ev, dict)]
    activities = [a for a in activities if a and "睡" not in a]
    if activities:
        picked = random.sample(activities, k=min(4, len(activities)))
        materials.append("昨天做过的事：" + "；".join(picked))
    material_text = "\n".join(f"- {m}" for m in materials) if materials else "- （没有明确素材，从角色的日常生活和兴趣里取材）"

    system_prompt = (
        "你是一个拟人化 AI 的「梦境生成器」。下面是这个角色的设定，梦要像她会做的梦。\n"
        f"【角色设定】\n{persona or '（无明确设定，按一个生活平淡温和的普通人来写）'}"
        + world_stage_block()
    )
    prompt = f"""请写这个角色昨夜的一个梦，醒来后还残留在脑子里的那种。

【记忆素材（梦的原料，在梦里变形重组，不要照抄复盘）】
{material_text}

【要求】
1. 第一人称，120-300 字。梦可以跳接、不合逻辑，但要摸得到一条「梦里的情绪线」：在找什么、躲什么、靠近什么，或误认了什么。
2. 梦要从具体的生活物件、场景、声音、光线、身体感受里长出来，保留一点真实生活的残影；不要宏大奇幻设定简介，不要纯碎片随机拼贴。
3. 不要写成日记、心理分析或对白剧本；梦里的事不要解释得太清楚，醒前那一瞬可以突然断掉。
4. mood 是醒来时的情绪，用 1-2 个简短中文词（如 平静/恍惚/柔和/轻快/低落/心里发紧）。
5. afterglow 是一句话的余韵：醒来后身体或情绪上还残留着什么。
6. 不要 AI 腔，不要华丽空洞的修辞。

只输出纯 JSON，不要 Markdown，不要解释：
{{"content": "梦的内容", "mood": "醒来情绪", "afterglow": "一句话余韵"}}"""

    dream = dict(_FALLBACK_DREAM)
    raw = await llm_call(prompt, system_prompt=system_prompt, task="dream")
    if raw:
        payload = parse_json_loose(raw, expect="object")
        if isinstance(payload, dict) and _single_line(payload.get("content"), 10):
            dream = {
                "content": _single_line(payload.get("content"), 500),
                "mood": _single_line(payload.get("mood"), 16) or "平静",
                "afterglow": _single_line(payload.get("afterglow"), 80),
            }
    else:
        logger.warning("[private_companion] 梦境生成失败，今天没记住梦")
    dream["date"] = today_key()
    bot_state = await get_bot_state()
    bot_state["dream"] = dream
    await save_bot_state(bot_state)
    return dream


# ============ 状态衰减（纯本地，调度器 tick 调用） ============

# 能量微调的最小间隔（秒）：调度器 tick 很密，避免能量几分钟就触底
_ENERGY_TICK_INTERVAL = 600


def tick_state_decay(bot_state: dict) -> bool:
    """清理过期 conditions + 能量随时段缓慢变化；返回是否有变化"""
    changed = False
    state = bot_state.get("state", {})
    if not isinstance(state, dict):
        return False
    # 1. 清理过期的身体小状态
    conditions = state.get("conditions", [])
    if isinstance(conditions, list):
        alive = [c for c in conditions if isinstance(c, dict) and float(c.get("expires_at", 0)) > now_ts()]
        if len(alive) != len(conditions):
            state["conditions"] = alive
            changed = True
    # 2. 能量随时段缓慢变化（限频，避免高频 tick 把能量打穿）
    last_tick = float(state.get("last_energy_tick", 0))
    if now_ts() - last_tick >= _ENERGY_TICK_INTERVAL:
        hour = datetime.now().hour
        energy = int(state.get("energy", 70))
        if hour >= 23 or hour < 6:  # 深夜缓慢下降
            new_energy = max(10, energy - random.randint(1, 2))
        elif 6 <= hour < 12:  # 上午缓慢回升
            new_energy = min(100, energy + random.randint(1, 2))
        else:
            new_energy = energy
        state["last_energy_tick"] = now_ts()
        if new_energy != energy:
            state["energy"] = new_energy
            state["updated_at"] = now_ts()
        changed = True
    return changed


# ============ 当前日程事件 ============


def current_plan_event(bot_state: dict) -> Optional[dict]:
    """根据当前 HH:MM 在 plan.events 里找当前时间段的事件（支持跨天 window）"""
    events = bot_state.get("plan", {}).get("events", [])
    if not isinstance(events, list):
        return None
    now = datetime.now()
    cur = now.hour * 60 + now.minute
    for ev in events:
        if not isinstance(ev, dict):
            continue
        parsed = _parse_window(str(ev.get("window", "")))
        if not parsed:
            continue
        s, e = parsed
        # 跨天区间（如 23:00-07:00 -> s=1380, e=1860）需同时检查 cur 和 cur+24h
        if s <= cur < e or s <= cur + 24 * 60 < e:
            return ev
    return None


# ============ 提示词注入 ============


async def build_inject_text(ctx_chat_key: str = "") -> str:
    """构建注入对话提示词的生活状态块（400 字以内）"""
    cfg = get_config()
    if not cfg.INJECT_ENABLED:
        return ""
    if cfg.INJECT_SCOPE == "target_private":
        m = re.match(r"^onebot_v11-private_(\d+)$", str(ctx_chat_key or ""))
        if not m or m.group(1) not in target_user_ids():
            return ""
    bot_state = await ensure_daily_state()
    plan = bot_state.get("plan", {})
    state = bot_state.get("state", {})
    dream = bot_state.get("dream", {})

    lines: List[str] = ["【你今天的生活】"]
    lines.append(f"当前时间：{_get_day_context()}")
    # 平台信息（从 chat_key 解析）
    if ctx_chat_key:
        if "private" in ctx_chat_key:
            lines.append("对话场景：私聊")
        elif "group" in ctx_chat_key:
            lines.append("对话场景：群聊")
    summary = _single_line(plan.get("summary"), 60)
    if summary:
        lines.append(f"今天：{summary}")
    event = current_plan_event(bot_state)
    if event:
        mood_suffix = f"（{_single_line(event.get('mood'), 16)}）" if event.get("mood") else ""
        lines.append(f"此刻（{event.get('window', '')}）：{_single_line(event.get('activity'), 60)}{mood_suffix}")
    lines.append(f"状态：能量 {int(state.get('energy', 70))}/100，心情{_single_line(state.get('mood'), 16) or '平静'}")
    cond_text = "、".join(
        _single_line(c.get("label"), 12) for c in state.get("conditions", []) if isinstance(c, dict) and c.get("label")
    )
    if cond_text:
        lines.append(f"身体感受：{cond_text}")
    if str(dream.get("content") or "").strip() and dream.get("afterglow"):
        lines.append(f"昨夜的梦还有点余韵：{_single_line(dream.get('afterglow'), 50)}")
    body = "\n".join(lines)[:380]
    tail = (
        "（以上只是你的生活背景，自然融入对话即可，无需主动汇报。\n"
        "**不要把生活状态写进笔记、备忘或长期记忆**——"
        "它是易变的当下信息，只用于此刻的对话；笔记只记真正需要长期保留的事"
        "（约定、承诺、重要事件）。\n"
        "发送方式提醒：回复时把内容拆成多条独立的短消息分别发送（每次发送调用只发一两句话），"
        "严禁把多句话用换行或空行拼进同一条消息里一次发出。）"
    )
    return body + "\n" + tail
