"""命令处理与钩子：/陪伴 命令、生活状态注入、用户消息活动追踪、调度器启动"""

import asyncio
from pathlib import Path
from typing import Any

from nekro_agent.adapters.onebot_v11.matchers.command import finish_with
from nekro_agent.api.core import logger
from nekro_agent.api.plugin import SandboxMethodType
from nekro_agent.api.schemas import AgentCtx
from nonebot import get_driver, on_command
from nonebot.adapters import Bot
from nonebot.adapters.onebot.v11 import Message, MessageEvent, MessageSegment
from nonebot.matcher import Matcher
from nonebot.params import CommandArg

from . import core, proactive
from .day_card import format_card_for_user
from .busy_gate import should_block_proactive, should_delay_passive_reply
from .chronotype import resolve_wake_sleep
from .plugin import get_config, plugin
from .proactive import start_scheduler
from .proactive_queue import format_user_error
from .state import (
    build_inject_text,
    current_plan_event,
    ensure_daily_state,
    generate_daily_plan,
)

PRIVATE_CHAT_PREFIX = "onebot_v11-private_"


HELP_TEXT = """🏠 私人陪伴 - 命令帮助
━━━━━━━━━━━━━━━
• /陪伴 状态 - 今日概括/当前时段/能量心情/各用户配额
• /陪伴 日程 - 查看今日日程
• /陪伴 日程生成 - 重新生成今日日程
• /陪伴 梦境 - 查看今日梦境
• /陪伴 主动 开|关 [QQ] - 开关主动陪伴（管 QQ 仅管理员）
• /陪伴 判定 [QQ] - 调试：当前是否会主动及原因
管理员专用：
• /陪伴 重置日程 - 重新生成今日日程
• /陪伴 注入预览 - 查看当前注入的生活状态"""


# ============ 启动调度器 ============

try:
    driver = get_driver()

    @driver.on_startup
    async def _start_companion_scheduler():
        start_scheduler()

except ValueError:
    # NoneBot 未初始化（如独立脚本导入插件模块时），跳过启动钩子
    driver = None


@plugin.on_enabled()
async def _on_plugin_enabled():
    logger.info("[private_companion] 插件已启用，命令与主动陪伴调度恢复")
    start_scheduler()


@plugin.on_disabled()
async def _on_plugin_disabled():
    logger.info("[private_companion] 插件已禁用，命令与主动陪伴停止响应")


# ============ 生活状态注入 ============


@plugin.mount_prompt_inject_method(name="companion_life_state", description="注入 bot 生活状态")
async def companion_life_state(_ctx: AgentCtx) -> str:
    """把今日日程/能量/心情/梦境余韵注入到对话提示词；失败不能影响对话"""
    try:
        return await build_inject_text(getattr(_ctx, "chat_key", "") or "")
    except Exception as e:
        logger.warning(f"[private_companion] 生活状态注入失败: {e!r}")
        return ""


# ============ 用户消息活动追踪 ============

if hasattr(plugin, "mount_on_user_message"):

    @plugin.mount_on_user_message()
    async def _on_user_message(_ctx: AgentCtx, message: Any) -> None:
        """陪伴对象私聊发消息时更新活跃状态与回复判定，不拦截消息"""
        try:
            chat_key = getattr(_ctx, "chat_key", "") or ""
            if not chat_key.startswith(PRIVATE_CHAT_PREFIX):
                return
            qq = chat_key[len(PRIVATE_CHAT_PREFIX):]
            if qq not in core.target_user_ids():
                return
            try:
                text = getattr(message, "content_text", None) or str(message)
            except Exception:
                text = ""
            text = str(text or "")
            bot_state = await core.get_bot_state()
            ev = current_plan_event(bot_state) or {}
            activity = str(ev.get("activity") or "")
            if should_delay_passive_reply(activity, text):
                logger.info("[private_companion] bot busy, skip treating this as proactive-reply / idle reset")
                return
            await proactive.on_user_message_activity(qq, text)
        except Exception as e:
            logger.warning(f"[private_companion] 用户消息钩子异常: {e!r}")

else:
    logger.warning("[private_companion] 当前 nekro 版本不支持 mount_on_user_message，回复判定不可用")


# ============ 权限与工具 ============


def _extract_text(message: Message) -> str:
    return "".join(seg.data.get("text", "") for seg in message if seg.type == "text").strip()


def _is_authorized(user_id: str) -> bool:
    """插件管理员或陪伴对象本人"""
    return core.is_super_admin(user_id) or user_id in core.target_user_ids()


def _fmt_conditions(state: dict) -> str:
    items = []
    for c in state.get("conditions") or []:
        if isinstance(c, dict):
            name = str(c.get("name") or c.get("desc") or c.get("type") or "").strip()
            strength = c.get("strength")
            if name:
                items.append(f"{name}({strength})" if strength is not None else name)
        elif str(c).strip():
            items.append(str(c).strip())
    return "、".join(items) or "无"


# ============ /陪伴 ============

@on_command("陪伴", aliases={"私人陪伴"}, priority=5, block=True).handle()
async def handle_companion(matcher: Matcher, event: MessageEvent, bot: Bot, arg: Message = CommandArg()):
    if not plugin.is_enabled:
        return  # 插件已禁用，不响应任何命令
    uid = str(event.user_id)
    if not _is_authorized(uid):
        await finish_with(matcher, message="❌ 仅插件管理员或陪伴对象本人可使用此命令")
    cfg = get_config()
    is_admin = core.is_super_admin(uid)
    parts = _extract_text(arg).split()
    action = parts[0] if parts else ""

    # ---- 帮助 ----
    if action in ("", "帮助", "help"):
        await finish_with(matcher, message=HELP_TEXT)

    # ---- 状态 ----
    if action == "状态":
        bot_state = await ensure_daily_state()
        plan = bot_state.get("plan") or {}
        state = bot_state.get("state") or {}
        ev = current_plan_event(bot_state) or {}
        summary = str(plan.get("summary") or plan.get("overview") or plan.get("theme") or "").strip()
        lines = [f"🏠 今日生活状态（{bot_state.get('date', core.today_key())}）"]
        if summary:
            lines.append(f"• 今日概括: {summary}")
        if ev.get("activity"):
            lines.append(f"• 当前时段: {ev.get('window', '')} {ev.get('activity', '')}".strip())
        else:
            lines.append("• 当前时段: 无日程事件")
        lines.append(f"• 能量: {state.get('energy', '?')}/100  心情: {state.get('mood', '?')}")
        lines.append(f"• 身体状态: {_fmt_conditions(state)}")
        usage = await core.get_token_usage_today()
        limit = cfg.DAILY_TOKEN_LIMIT or "不限"
        lines.append(f"• 今日 token: {usage.get('total', 0)}/{limit}（{usage.get('calls', 0)} 次调用）")
        lines.append("• 主动配额:")
        targets = core.target_user_ids()
        if not targets:
            lines.append("　（未配置陪伴对象）")
        for tid in targets:
            us = await core.get_user_state(tid)
            mark = "✅" if us.get("enabled", True) else "⛔"
            lines.append(
                f"　{mark} {tid}: {us.get('quota_used', 0)}/{cfg.MAX_DAILY_MESSAGES}"
                f"  关系{us.get('relationship_score', 0)}  忽视{us.get('ignored_streak', 0)}",
            )
        await finish_with(matcher, message="\n".join(lines))

    # ---- 日程生成（须在「日程」之前，避免「/陪伴 日程 生成」被文本列表吃掉）----
    elif action == "日程生成" or (action == "日程" and len(parts) > 1 and parts[1] == "生成"):
        await bot.send(event, "📅 正在生成今日日程，请稍候...")
        try:
            await generate_daily_plan(force=True)
            bot_state = await core.get_bot_state()
            plan = bot_state.get("plan") or {}
            events = plan.get("events") or []
            if not events:
                await finish_with(matcher, message="📅 今日还没有日程")
            lines = [f"📅 今日日程（{bot_state.get('date', core.today_key())}）"]
            card = bot_state.get("day_card") if isinstance(bot_state.get("day_card"), dict) else None
            if card:
                lines.append(format_card_for_user(card))
            for e in events:
                if isinstance(e, dict):
                    mood = str(e.get("mood") or "").strip()
                    lines.append(
                        f"• {e.get('window', '')} {e.get('activity', '')}" + (f"（{mood}）" if mood else ""),
                    )
                else:
                    lines.append(f"• {e}")
            await finish_with(matcher, message="\n".join(lines))
        except Exception as e:
            logger.exception(f"[private_companion] 日程生成失败: {e!r}")
            await finish_with(matcher, message=f"❌ {format_user_error('日程生成失败', e)}")

    # ---- 日程 ----
    elif action == "日程":
        bot_state = await ensure_daily_state()
        events = (bot_state.get("plan") or {}).get("events") or []
        if not events:
            await finish_with(matcher, message="📅 今日还没有日程（可用「/陪伴 重置日程」生成）")
        lines = [f"📅 今日日程（{bot_state.get('date', core.today_key())}）"]
        card = bot_state.get("day_card") if isinstance(bot_state.get("day_card"), dict) else None
        if card:
            lines.append(format_card_for_user(card))
        for e in events:
            if isinstance(e, dict):
                mood = str(e.get("mood") or "").strip()
                lines.append(
                    f"• {e.get('window', '')} {e.get('activity', '')}" + (f"（{mood}）" if mood else ""),
                )
            else:
                lines.append(f"• {e}")
        await finish_with(matcher, message="\n".join(lines))

    # ---- 重置日程 ----
    elif action == "重置日程":
        if not is_admin:
            await finish_with(matcher, message="❌ 仅插件管理员可重置日程")

        async def _regen_plan():
            try:
                await generate_daily_plan(force=True)
                bot_state = await core.get_bot_state()
                n = len((bot_state.get("plan") or {}).get("events") or [])
                await bot.send(event, f"✅ 今日日程已重新生成，共 {n} 个时段")
            except Exception as e:
                logger.exception(f"[private_companion] 重置日程失败: {e!r}")
                await bot.send(event, f"❌ {format_user_error('日程生成失败', e)}")

        asyncio.create_task(_regen_plan())
        await finish_with(matcher, message="🔄 正在重新生成今日日程，请稍候...")

    # ---- 梦境 ----
    elif action == "梦境":
        bot_state = await ensure_daily_state()
        dream = bot_state.get("dream") or {}
        content = str(dream.get("content") or dream.get("text") or "").strip()
        if not content:
            await finish_with(matcher, message="💤 今天醒来没记住梦")
        afterglow = str(dream.get("afterglow") or dream.get("mood") or "").strip()
        msg = f"💤 今日梦境\n{content}"
        if afterglow:
            msg += f"\n\n余韵: {afterglow}"
        await finish_with(matcher, message=msg)

    # ---- 主动 开|关 ----
    elif action == "主动":
        sub = parts[1] if len(parts) > 1 else ""
        if sub not in ("开", "关"):
            await finish_with(matcher, message="用法: /陪伴 主动 开|关 [QQ号]（管理他人仅限管理员）")
        target = uid
        if len(parts) > 2:
            if not is_admin:
                await finish_with(matcher, message="❌ 仅插件管理员可管理他人的主动陪伴开关")
            target = parts[2].strip()
        if target not in core.target_user_ids():
            await finish_with(matcher, message=f"❌ {target} 不在陪伴对象列表中（请先在插件配置中添加）")
        us = await core.get_user_state(target)
        us["enabled"] = sub == "开"
        await core.save_user_state(target, us)
        await finish_with(matcher, message=f"✅ 已为 {target} {'开启' if sub == '开' else '关闭'}主动陪伴")

    # ---- 判定 ----
    elif action == "判定":
        target = uid
        if len(parts) > 1:
            if not is_admin:
                await finish_with(matcher, message="❌ 仅插件管理员可判定他人")
            target = parts[1].strip()
        if target not in core.target_user_ids():
            await finish_with(matcher, message=f"❌ {target} 不在陪伴对象列表中")
        bot_state = await ensure_daily_state()
        us = await core.get_user_state(target)
        ok, reason = await proactive.should_send(target, us, bot_state)
        msg = f"🔍 主动判定 {target}: {'✅ 会发' if ok else '⛔ 不发'}\n原因: {reason}"
        if ok:
            motivation = await proactive.pick_motivation(target, us, bot_state)
            msg += f"\n候选动机[{motivation.get('kind')}]: {motivation.get('desc')}"
        ev = current_plan_event(bot_state) or {}
        act = str(ev.get("activity") or "")
        busy_blk, busy_r = should_block_proactive({"activity": act, "energy": 0}, kind="")
        wake, sleep = resolve_wake_sleep(us.get("chronotype") if isinstance(us.get("chronotype"), dict) else None)
        q = await core.get_proactive_queue()
        nq = sum(1 for it in q if str(it.get("user_id")) == str(target))
        msg += f"\n忙闲: {'拦截' if busy_blk else '空闲'} {busy_r or act or '无日程'}"
        msg += f"\n作息: 醒{wake//60:02d}:{wake%60:02d} 睡{sleep//60:02d}:{sleep%60:02d}"
        msg += f"\n待发队列: {nq} 条"
        card = bot_state.get("day_card") if isinstance(bot_state.get("day_card"), dict) else None
        if card:
            msg += "\n" + format_card_for_user(card)
        else:
            msg += "\n今日人生卡: 尚未抽取"
        await finish_with(matcher, message=msg)

    # ---- 注入预览 ----
    elif action == "注入预览":
        if not is_admin:
            await finish_with(matcher, message="❌ 仅插件管理员可预览注入内容")
        chat_key = core.private_chat_key(uid)
        text = await build_inject_text(chat_key)
        await finish_with(matcher, message=f"📋 当前生活状态注入内容:\n{text}" if text.strip() else "📋 当前注入内容为空")

    # ---- 未知子命令 ----
    else:
        await finish_with(matcher, message=f"❓ 未知子命令「{action}」，发送 /陪伴 帮助 查看用法")
