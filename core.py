"""核心库：存储 / LLM 调用 / 人设解析 / token 预算 / 公共工具

所有模块共享的基础设施。状态数据存 plugin.store（chat_key="global"）：
- bot_state         bot 的生活状态（日程/状态/梦境/日记）
- user_{qq}         每个陪伴对象的主动陪伴状态
- token_usage_{日期} 当日 token 消耗计数
"""

import asyncio
import json
import time
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from nekro_agent.api.core import config as core_config
from nekro_agent.api.core import logger
from openai import AsyncOpenAI

from .plugin import get_config, plugin, store

# ============ 时间工具 ============


def now_ts() -> float:
    return time.time()


def today_key() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def hhmm_now() -> str:
    return datetime.now().strftime("%H:%M")


def parse_hhmm(s: str) -> Optional[Tuple[int, int]]:
    try:
        s = str(s).replace("：", ":").strip()
        h, m = s.split(":")
        return int(h) % 24, int(m) % 60
    except Exception:
        return None


def in_quiet_hours(now: Optional[datetime] = None) -> bool:
    """是否处于免打扰时段（支持跨天，如 23:30 - 07:30）"""
    cfg = get_config()
    start = parse_hhmm(cfg.QUIET_HOURS_START)
    end = parse_hhmm(cfg.QUIET_HOURS_END)
    if not start or not end:
        return False
    now = now or datetime.now()
    cur = now.hour * 60 + now.minute
    s = start[0] * 60 + start[1]
    e = end[0] * 60 + end[1]
    if s <= e:
        return s <= cur < e
    return cur >= s or cur < e


# ============ 存储 ============


async def get_json(key: str, default=None):
    raw = await store.get(chat_key="global", store_key=key)
    if not raw:
        return default
    try:
        return json.loads(raw)
    except Exception:
        return default


async def set_json(key: str, value) -> None:
    await store.set(chat_key="global", store_key=key, value=json.dumps(value, ensure_ascii=False))


async def get_bot_state() -> dict:
    state = await get_json("bot_state", None)
    if not isinstance(state, dict):
        state = {}
    state.setdefault("date", "")
    state.setdefault("plan", {})
    state.setdefault("state", {})
    state.setdefault("dream", {})
    state.setdefault("diaries", [])
    state.setdefault("day_card", {})
    return state


async def save_bot_state(state: dict) -> None:
    await set_json("bot_state", state)


def default_user_state(user_id: str) -> dict:
    return {
        "user_id": str(user_id),
        "enabled": True,
        "nickname": "",
        "remark": "",
        "quota_date": today_key(),
        "quota_used": 0,
        "last_sent_ts": 0.0,
        "last_sent_topic": "",
        "last_user_msg_ts": 0.0,
        "last_user_msg": "",
        "ignored_streak": 0,
        "relationship_score": 20,
        "total_sent": 0,
        "total_replied": 0,
        "recent_topics": [],
        "log": [],  # [{ts, topic, replied}]
    }


async def get_user_state(user_id: str) -> dict:
    state = await get_json(f"user_{user_id}", None)
    base = default_user_state(user_id)
    if isinstance(state, dict):
        base.update(state)
    # 每日配额重置
    if base.get("quota_date") != today_key():
        base["quota_date"] = today_key()
        base["quota_used"] = 0
    return base


async def save_user_state(user_id: str, state: dict) -> None:
    await set_json(f"user_{user_id}", state)


async def get_proactive_queue() -> list:
    q = await get_json("proactive_queue", [])
    return q if isinstance(q, list) else []


async def save_proactive_queue(queue: list) -> None:
    await set_json("proactive_queue", queue)


async def get_day_card_history() -> dict:
    from . import day_card as dc
    h = await get_json("day_card_history", None)
    if not isinstance(h, dict):
        return dc.empty_history()
    h.setdefault("recent_scene_ids", [])
    h.setdefault("recent_event_ids", [])
    if not isinstance(h["recent_scene_ids"], list):
        h["recent_scene_ids"] = []
    if not isinstance(h["recent_event_ids"], list):
        h["recent_event_ids"] = []
    return h


async def save_day_card_history(history: dict) -> None:
    await set_json("day_card_history", history)


def target_user_ids() -> List[str]:
    return [str(x).strip() for x in get_config().TARGET_USER_IDS if str(x).strip()]


def is_super_admin(user_id: str) -> bool:
    cfg = get_config()
    return str(user_id).strip() in {str(x).strip() for x in cfg.MANAGE_SUPER_ADMINS if str(x).strip()}


def private_chat_key(user_id: str) -> str:
    return f"onebot_v11-private_{user_id}"


# ============ 人设解析 ============


# ===== 世界观舞台 =====
# 日程/梦境/日记必须发生在角色所属世界的语境里。
# 实测问题：不加约束时 LLM 会写成现代中国都市生活
# （挤城铁去邻市找同学、地铁口碰头、快递丢了、被拉去加班、窝着刷手机）。
#
# 文本放插件数据目录（plugin_data/xiaojiu.private_companion/world_stage.txt），
# 不写死在源码里 —— 换角色只换数据文件，升级插件时可整包覆盖源码。
# 数据文件不存在时返回空串，等同上游原版行为（不加世界观约束）。
_WORLD_STAGE_REL = ("plugin_data", "xiaojiu.private_companion", "world_stage.txt")
_WORLD_STAGE_CACHE: Dict[str, object] = {"key": None, "text": ""}
WORLD_STAGE = ""  # 兼容旧取值；新代码请用 load_world_stage()


def world_stage_path() -> "Path":
    """世界观文本的数据文件路径。"""
    from pathlib import Path

    from nekro_agent.core.os_env import OsEnv

    return Path(OsEnv.DATA_DIR).joinpath(*_WORLD_STAGE_REL)


def load_world_stage() -> str:
    """读取世界观舞台文本；按 (mtime, size) 缓存；文件不存在则返回空串。"""
    global WORLD_STAGE
    try:
        fp = world_stage_path()
        if not fp.is_file():
            _WORLD_STAGE_CACHE["key"] = None
            _WORLD_STAGE_CACHE["text"] = ""
            WORLD_STAGE = ""
            return ""
        stat = fp.stat()
        key = (stat.st_mtime, stat.st_size)
        if _WORLD_STAGE_CACHE.get("key") == key:
            return str(_WORLD_STAGE_CACHE.get("text") or "")
        text = fp.read_text(encoding="utf-8").strip()
        _WORLD_STAGE_CACHE["key"] = key
        _WORLD_STAGE_CACHE["text"] = text
        WORLD_STAGE = text
        return text
    except Exception:  # noqa: BLE001
        logger.exception("[companion] 读取 world_stage.txt 失败，按空处理")
        return ""


def save_world_stage(text: str) -> str:
    """原子写入世界观舞台文本，并刷新缓存；返回落盘路径。"""
    global WORLD_STAGE
    text = str(text or "").strip()
    if not text:
        raise ValueError("世界观文本不能为空")
    fp = world_stage_path()
    fp.parent.mkdir(parents=True, exist_ok=True)
    tmp = fp.with_name(fp.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(fp)
    stat = fp.stat()
    _WORLD_STAGE_CACHE["key"] = (stat.st_mtime, stat.st_size)
    _WORLD_STAGE_CACHE["text"] = text
    WORLD_STAGE = text
    return str(fp)


def world_stage_block() -> str:
    """渲染为可追加到 system_prompt 的世界观约束段；无内容时返回空串。"""
    ws = load_world_stage()
    if not ws:
        return ""
    return "\n\n【世界观（必须严格遵守，不得出现世界外元素）】\n" + ws


# ===== 背景记忆（本地补丁 NTidal）=====
# 生成日程/梦境/日记时带上 nekro_persona 插件的 1 档背景记忆，
# 避免生成与角色设定冲突的生活线（实测曾生成"长途夜车/候车厅/小旅馆/上班族"）。
# 直接读文件、不 import 对方插件，保持解耦；按 mtime 缓存。
_BG_MEM_CACHE: Dict[str, object] = {"key": None, "text": ""}
_BG_TIER1_REL = "plugins/workdir/nekro_persona/backgrounds/tier1/core.json"


def get_background_memory() -> str:
    """读取 1 档背景记忆并渲染为紧凑文本块；读不到返回空串。"""
    try:
        from pathlib import Path

        from nekro_agent.core.os_env import OsEnv

        fp = Path(OsEnv.DATA_DIR) / _BG_TIER1_REL
        if not fp.is_file():
            return ""
        mtime = fp.stat().st_mtime
        if _BG_MEM_CACHE.get("key") == mtime:
            return str(_BG_MEM_CACHE.get("text") or "")
        data = json.loads(fp.read_text(encoding="utf-8"))
        lines = []
        for item in data.get("entries") or []:
            if not isinstance(item, dict) or not item.get("enabled", True):
                continue
            content = str(item.get("content") or "").strip()
            if content:
                lines.append(f"- {content}")
        text = ""
        if lines:
            text = (
                "\n\n## 附：她的背景事实（生成生活线时必须与这些一致，不得矛盾）\n"
                "**这些是她的过去与底色，不是今天的情绪基调。**"
                "她照常上课、写代码、排练、跟人斗嘴；"
                "生成日程时不要把这些往事当成今天的氛围，今天依然可以有轻快、无聊、好笑的段落。\n"
                + "\n".join(lines)
            )
        _BG_MEM_CACHE.update(key=mtime, text=text)
        return text
    except Exception as e:  # noqa: BLE001
        logger.debug(f"[private_companion] 读取背景记忆失败: {e!r}")
        return ""


async def get_persona_prompt() -> str:
    """陪伴人格：自定义文本 > 选中人设 > 系统默认人设 > 空；末尾追加背景记忆。"""
    cfg = get_config()
    base = ""
    if cfg.PERSONA_PROMPT.strip():
        base = cfg.PERSONA_PROMPT.strip()
    else:
        try:
            from nekro_agent.models.db_preset import DBPreset

            pid = str(cfg.PERSONA_PRESET_ID).strip()
            if pid.isdigit():
                preset = await DBPreset.get_or_none(id=int(pid))
                if preset and preset.content:
                    base = str(preset.content)
            if not base:
                default_id = getattr(core_config, "AI_CHAT_DEFAULT_PRESET_ID", None)
                if default_id:
                    preset = await DBPreset.get_or_none(id=default_id)
                    if preset and preset.content:
                        base = str(preset.content)
        except Exception as e:
            logger.warning(f"[private_companion] 读取人设失败: {e!r}")
    return base + get_background_memory()


# ============ token 预算 ============


async def get_token_usage_today() -> dict:
    usage = await get_json(f"token_usage_{today_key()}", None)
    if not isinstance(usage, dict):
        usage = {"total": 0, "calls": 0, "by_task": {}}
    return usage


async def record_token_usage(task: str, tokens: int) -> None:
    usage = await get_token_usage_today()
    usage["total"] = int(usage.get("total", 0)) + int(tokens)
    usage["calls"] = int(usage.get("calls", 0)) + 1
    by_task = usage.setdefault("by_task", {})
    by_task[task] = int(by_task.get(task, 0)) + int(tokens)
    await set_json(f"token_usage_{today_key()}", usage)


async def budget_exceeded() -> bool:
    cfg = get_config()
    if cfg.DAILY_TOKEN_LIMIT <= 0:
        return False
    usage = await get_token_usage_today()
    return int(usage.get("total", 0)) >= cfg.DAILY_TOKEN_LIMIT


# ============ LLM 调用 ============


def _resolve_model_group(name: str = ""):
    cfg = get_config()
    target = (name or "").strip() or cfg.MODEL_GROUP.strip() or core_config.USE_MODEL_GROUP
    groups = core_config.MODEL_GROUPS
    if target in groups:
        return groups[target]
    return groups[core_config.USE_MODEL_GROUP]


async def llm_call(
    prompt: str,
    system_prompt: str = "",
    task: str = "other",
    model_group: str = "",
    skip_budget: bool = False,
) -> Optional[str]:
    """插件内部 LLM 调用（日程/状态/梦境/日记生成），带预算与重试。

    返回 None 表示失败或预算超限。
    """
    cfg = get_config()
    if not skip_budget and await budget_exceeded():
        logger.warning(f"[private_companion] 今日 token 预算已超限，跳过任务 {task}")
        return None
    group_name = model_group
    if not group_name and task in ("daily_plan", "daily_state", "refine"):
        group_name = cfg.STATE_MODEL_GROUP
    mg = _resolve_model_group(group_name)
    client = AsyncOpenAI(api_key=mg.API_KEY, base_url=mg.BASE_URL, timeout=cfg.LLM_TIMEOUT)
    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": prompt})
    last_err = None
    for attempt in range(cfg.LLM_RETRIES + 1):
        try:
            resp = await client.chat.completions.create(model=mg.CHAT_MODEL, messages=messages)
            text = (resp.choices[0].message.content or "") if resp.choices else ""
            tokens = 0
            if getattr(resp, "usage", None):
                tokens = getattr(resp.usage, "total_tokens", 0) or 0
            await record_token_usage(task, tokens)
            if text.strip():
                return text
            raise RuntimeError("LLM 返回空内容")
        except Exception as e:  # noqa: PERF203
            last_err = e
            if attempt < cfg.LLM_RETRIES:
                await asyncio.sleep(2 * (attempt + 1))
    logger.error(f"[private_companion] LLM 任务 {task} 失败: {last_err!r}")
    return None


def parse_json_loose(text: str, expect: str = "object"):
    """宽松解析 LLM 返回的 JSON（剥 markdown 代码块、提取首个 JSON 块）"""
    import re

    if not text:
        return None
    clean = re.sub(r"```(?:json)?\s*", "", text.strip())
    pattern = r"\{.*\}" if expect == "object" else r"\[.*\]"
    m = re.search(pattern, clean, re.DOTALL)
    if not m:
        return None
    raw = m.group()
    try:
        return json.loads(raw)
    except Exception:
        pass
    # 常见修复
    raw2 = raw.replace("“", '"').replace("”", '"').replace("，", ",").replace("：", ":")
    raw2 = re.sub(r",\s*([}\]])", r"\1", raw2)
    try:
        return json.loads(raw2)
    except Exception:
        return None


# ============ 主动唤醒（走 nekro 原生 timer，让 agent 自己组织语言） ============


async def wake_agent_for_user(user_id: str, event_desc: str, delay_seconds: int = 3) -> bool:
    """通过 nekro timer 在指定私聊唤醒 agent，event_desc 作为触发上下文"""
    try:
        from nekro_agent.api.timer import set_temp_timer

        chat_key = private_chat_key(user_id)
        return await set_temp_timer(chat_key, int(now_ts()) + delay_seconds, event_desc)
    except Exception as e:
        logger.error(f"[private_companion] 唤醒 agent 失败 user={user_id}: {e!r}")
        return False
