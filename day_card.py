# -*- coding: utf-8 -*-
"""Daily life-card drawer: vary LLM daily plans with a scene + two events."""
from __future__ import annotations

import json
import random
from pathlib import Path
from typing import Any, Optional

from nekro_agent.api.core import logger

_DATA_REL = ("plugin_data", "xiaojiu.private_companion", "day_card.json")
_DEFAULT_COOLDOWN = 7
_CACHE: dict = {"key": None}

# 数据文件缺失/损坏时的兜底场景池。刻意不绑定任何具体世界观
# （不出现地名、学校、公司、交通工具），任何角色设定下都不会出戏；
# 正常运行时数据文件才是唯一来源。
_FALLBACK: dict = {
    "cooldown_days": _DEFAULT_COOLDOWN,
    "scenes": [
        {"id": "ordinary_day", "title": "平常的一天",
         "setting": "没什么特别安排的一天，按平时的作息过，事情不多不少。"},
        {"id": "busy_day", "title": "忙碌的一天",
         "setting": "事情排得比平时满，来回奔波，只有零星空档。"},
        {"id": "quiet_day", "title": "安静的一天",
         "setting": "没什么人打扰，一个人安安静静地待着。"},
        {"id": "outdoor_day", "title": "在外面的一天",
         "setting": "在外面待了大半天，走走看看，时间过得很快。"},
        {"id": "tired_day", "title": "疲惫的一天",
         "setting": "状态不太好，做什么都提不起劲，早早就歇下了。"},
        {"id": "good_day", "title": "顺遂的一天",
         "setting": "事情办得比预想顺利，心情轻快，有余力搭理别的事。"},
        {"id": "sick_day", "title": "生病躺床",
         "setting": "身体不舒服，几乎不出门，行动半径只剩床和热水。"},
        {"id": "visitor_day", "title": "有人来串门",
         "setting": "有人来待了大半天，屋里热闹，时间在聊天和煮东西里过去。"},
    ],
    "events": [
        {"id": "power_cut", "blurb": "停了一次电"},
        {"id": "friend_cancel", "blurb": "约好的人临时来不了"},
        {"id": "old_photo", "blurb": "翻出一张很久以前的旧照片"},
        {"id": "stray_cat", "blurb": "附近出现一只不知从哪来的猫"},
        {"id": "good_news", "blurb": "收到一个好消息"},
        {"id": "small_gift", "blurb": "收到一份没预料到的小礼物"},
        {"id": "found_lost", "blurb": "丢了很久的东西找回来了"},
        {"id": "broken_thing", "blurb": "正在用的东西突然坏了，得从头查"},
    ],
}

# 当前生效的角色内容，由 load_card() 从数据文件刷新。
# 保留模块级名字，本文件与 router、测试的既有调用方都不用改。
COOLDOWN_DAYS: int = _DEFAULT_COOLDOWN
SCENES: list[dict[str, str]] = []
EVENTS: list[dict[str, str]] = []
_SCENE_BY_ID: dict[str, dict[str, str]] = {}
_EVENT_BY_ID: dict[str, dict[str, str]] = {}


def card_path() -> Path:
    """场景/事件池的数据文件路径（格式与导出包里的 companion/day_card.json 一致）。"""
    from nekro_agent.core.os_env import OsEnv

    return Path(OsEnv.DATA_DIR).joinpath(*_DATA_REL)


def _apply(card: dict) -> None:
    global COOLDOWN_DAYS, SCENES, EVENTS, _SCENE_BY_ID, _EVENT_BY_ID
    COOLDOWN_DAYS = max(1, int(card.get("cooldown_days") or _DEFAULT_COOLDOWN))
    SCENES = [dict(x) for x in (card.get("scenes") or [])]
    EVENTS = [dict(x) for x in (card.get("events") or [])]
    _SCENE_BY_ID = {s["id"]: s for s in SCENES}
    _EVENT_BY_ID = {e["id"]: e for e in EVENTS}


def load_card(force: bool = False) -> bool:
    """从数据文件刷新角色内容。返回 True=用的是数据文件，False=兜底。"""
    try:
        path = card_path()
        if path.is_file():
            stat = path.stat()
            key = (stat.st_mtime, stat.st_size)
            if not force and _CACHE.get("key") == key:
                return True
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data.get("scenes"), list) and data["scenes"] \
                    and isinstance(data.get("events"), list) and data["events"]:
                _apply(data)
                _CACHE["key"] = key
                return True
            logger.warning("[companion] day_card.json 内容不完整，改用兜底场景池")
    except Exception:  # noqa: BLE001
        logger.exception("[companion] 读取 day_card.json 失败，改用兜底场景池")
    if _CACHE.get("key") != "fallback":
        _apply(_FALLBACK)
        _CACHE["key"] = "fallback"
    return False


def current_card() -> dict:
    """当前生效的角色内容（导出用，格式与 day_card.json 一致）。"""
    load_card()
    return {
        "cooldown_days": COOLDOWN_DAYS,
        "scenes": [dict(x) for x in SCENES],
        "events": [dict(x) for x in EVENTS],
    }


def save_card(card: dict) -> str:
    """原子写入角色内容并立即生效；返回落盘路径。"""
    scenes = card.get("scenes")
    events = card.get("events")
    if not isinstance(scenes, list) or not scenes:
        raise ValueError("场景池不能为空")
    if not isinstance(events, list) or not events:
        raise ValueError("事件池不能为空")
    payload = {
        "cooldown_days": max(1, int(card.get("cooldown_days") or _DEFAULT_COOLDOWN)),
        "scenes": [dict(x) for x in scenes],
        "events": [dict(x) for x in events],
    }
    path = card_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=4),
                   encoding="utf-8", newline="\n")
    tmp.replace(path)
    stat = path.stat()
    _CACHE["key"] = (stat.st_mtime, stat.st_size)
    _apply(payload)
    return str(path)


def empty_history() -> dict[str, list[str]]:
    return {"recent_scene_ids": [], "recent_event_ids": []}


def _rng_for(date_key: str, rng: Optional[random.Random]) -> random.Random:
    if rng is None:
        return random.Random(date_key)
    return rng


def _pick_scene(history: dict, rng: random.Random) -> dict[str, str]:
    load_card()
    recent = list(history.get("recent_scene_ids") or [])
    cooled = set(recent[-COOLDOWN_DAYS:])
    if not SCENES:
        return {"id": "unknown", "title": "平常的一天", "setting": "按平时的作息过一天。"}
    available = [s for s in SCENES if s["id"] not in cooled]
    if available:
        return rng.choice(available)
    # All on cooldown: pick the oldest-cooldown (first in recent window), never fail.
    if recent:
        oldest = recent[-COOLDOWN_DAYS:][0] if len(recent) >= COOLDOWN_DAYS else recent[0]
        scene = _SCENE_BY_ID.get(oldest)
        if scene is not None:
            return scene
    return SCENES[0]


def _pick_events(history: dict, rng: random.Random) -> list[dict[str, str]]:
    load_card()
    recent = list(history.get("recent_event_ids") or [])
    cooled = set(recent[-14:])
    available = [e for e in EVENTS if e["id"] not in cooled]
    picked: list[dict[str, str]] = []
    if not EVENTS:
        return []
    pool = list(available)
    rng.shuffle(pool)
    for ev in pool:
        if ev["id"] not in {p["id"] for p in picked}:
            picked.append(ev)
        if len(picked) == 2:
            return picked
    # Pool exhausted: allow reuse, still return 2 distinct ids if possible.
    leftovers = [e for e in EVENTS if e["id"] not in {p["id"] for p in picked}]
    rng.shuffle(leftovers)
    for ev in leftovers:
        picked.append(ev)
        if len(picked) == 2:
            break
    while len(picked) < 2:
        picked.append(EVENTS[len(picked) % len(EVENTS)])
    return picked[:2]


def draw_day_card(
    history: dict,
    *,
    date_key: str,
    rng: Optional[random.Random] = None,
) -> dict[str, Any]:
    r = _rng_for(date_key, rng)
    scene = _pick_scene(history, r)
    events = _pick_events(history, r)
    return {
        "date": date_key,
        "scene": {"id": scene["id"], "title": scene["title"], "setting": scene["setting"]},
        "events": [{"id": e["id"], "blurb": e["blurb"]} for e in events],
    }


def remember_card(history: dict, card: dict) -> dict:
    scenes = list(history.get("recent_scene_ids") or [])
    events = list(history.get("recent_event_ids") or [])
    scenes.append(card["scene"]["id"])
    events.append(card["events"][0]["id"])
    events.append(card["events"][1]["id"])
    history["recent_scene_ids"] = scenes[-14:]
    history["recent_event_ids"] = events[-28:]
    return history


def format_card_for_prompt(card: dict) -> str:
    title = card["scene"]["title"]
    setting = card["scene"]["setting"]
    b1 = card["events"][0]["blurb"]
    b2 = card["events"][1]["blurb"]
    return (
        f"今日场景：{title}。场景约束：{setting}。\n"
        f"今日事件：1) {b1} 2) {b2}\n"
        "要求：日程必须围绕这个场景来写，并把两个事件嵌进不同时段；"
        "不要写成与近几天相同的上课/写代码模板日。"
    )


def format_card_for_user(card: dict) -> str:
    title = card["scene"]["title"]
    b1 = card["events"][0]["blurb"]
    b2 = card["events"][1]["blurb"]
    return f"今日人生卡：{title}｜{b1}；{b2}"
