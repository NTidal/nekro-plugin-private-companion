"""私人陪伴插件 - 插件定义与配置

核心理念移植自 astrbot_plugin_private_companion（精简适配版）：
让 bot 拥有连续的"生活感"（每日日程/能量/心情/梦境），
并对陪伴列表中的用户进行有节制的主动陪伴。

本地精简（NTidal）：已移除日程图卡、每日日记、视觉资产与日程自拍，
只保留日程生成、生活状态注入、梦境、主动陪伴。

与原版的关键差异：主动消息通过 nekro 原生 timer 唤醒 agent 自己组织语言，
人设/记忆/对话历史全部走 nekro 原生体系。
"""

from typing import List, Literal, Optional

from nekro_agent.api.plugin import ConfigBase, NekroPlugin
from nekro_agent.core.core_utils import ExtraField
from pydantic import Field

MODEL_GROUP_REF = ExtraField(ref_model_groups=True, model_type="chat").model_dump()

plugin = NekroPlugin(
    name="私人陪伴",
    module_name="private_companion",
    description="让 bot 拥有连续的生活感（日程/状态/梦境）并主动陪伴指定用户，附 WebUI 面板",
    version="0.3.1",
    author="xiaojiu",
    url="/plugins/xiaojiu.private_companion/",
)


@plugin.mount_config()
class CompanionConfig(ConfigBase):
    """私人陪伴配置。所有项改完即时生效，无需重启。"""

    # ==================== 陪伴对象 ====================
    # 只有列表里的用户会收到主动消息；不填则只生成生活状态、不主动打扰任何人。

    TARGET_USER_IDS: List[str] = Field(
        default=[],
        title="陪伴对象 QQ 列表",
        description=(
            "会收到主动陪伴消息的 QQ 号，每人独立配额与关系状态。"
            "留空 = 只生成生活状态并注入对话，不主动给任何人发消息。"
        ),
    )
    MANAGE_SUPER_ADMINS: List[str] = Field(
        default=[],
        title="插件管理员 QQ 列表",
        description="可使用全部「陪伴」管理命令；陪伴对象本人也能管理自己的开关。",
    )

    # ==================== 人格来源 ====================
    # 生成日程/状态/梦境时用谁的人格。优先级：自定义描述 > 指定人设 > 系统默认人设。

    PERSONA_PROMPT: str = Field(
        default="",
        title="自定义人格描述（最高优先级）",
        description=(
            "直接写一段人格描述，生成生活内容时以此为准。"
            "留空则改用下面指定的人设。"
        ),
        json_schema_extra=ExtraField(is_textarea=True).model_dump(),
    )
    PERSONA_PRESET_ID: str = Field(
        default="",
        title="陪伴人格（人设 ID）",
        description=(
            "生成日程/状态/梦境时使用的人设 ID。"
            "上方的自定义描述留空时才会用到；此处也为空则退回系统默认人设。"
        ),
        json_schema_extra=ExtraField(ref_presets=True, ref_presets_no_default=True).model_dump(),
    )
    PLAN_STYLE_HINT: str = Field(
        default="",
        title="日程风格补充（可选）",
        description=(
            "只影响每日日程的细碎偏好，例如「大学生作息，常熬夜」「自由职业画师」；"
            "不需要时留空。"
        ),
        json_schema_extra=ExtraField(is_textarea=True).model_dump(),
    )

    # ==================== 模型 ====================

    MODEL_GROUP: str = Field(
        default="",
        title="主模型组",
        description="日程 / 状态 / 梦境等内容生成使用的模型组，留空用 nekro 主模型组。",
        json_schema_extra=MODEL_GROUP_REF,
    )
    STATE_MODEL_GROUP: str = Field(
        default="",
        title="轻量模型组（高频任务）",
        description=(
            "日程、状态这类每天都要跑的高频轻量生成可指定更便宜的模型组；"
            "留空则与主模型组相同。"
        ),
        json_schema_extra=MODEL_GROUP_REF,
    )
    LLM_TIMEOUT: int = Field(
        default=120,
        title="LLM 请求超时（秒）",
        description="单次内容生成请求的最长等待时间，超时按失败处理。",
        ge=30,
        le=600,
    )
    LLM_RETRIES: int = Field(
        default=2,
        title="LLM 重试次数",
        description="生成失败后的自动重试次数，0 表示不重试。",
        ge=0,
        le=5,
    )

    # ==================== 生活状态注入 ====================
    # 把「今天在做什么 / 心情如何」注入对话上下文，让 bot 有连续的生活感。

    INJECT_ENABLED: bool = Field(
        default=True,
        title="启用生活状态注入",
        description="把今日日程、能量、心情与梦境余韵注入对话提示词；关闭后 bot 不再有连续生活感。",
    )
    INJECT_SCOPE: Literal["all", "target_private"] = Field(
        default="all",
        title="状态注入范围",
        description="all = 所有会话都注入生活状态；target_private = 只注入陪伴对象的私聊。",
    )

    # ==================== 每日人生卡 ====================
    # 用「一个场景 + 两件小事」做当天日程的种子，避免每天重复同样的日常。

    DAY_CARD_ENABLED: bool = Field(
        default=True,
        title="启用每日人生卡",
        description="每天从场景池 / 事件池各抽一些再生成日程，大幅降低日常内容的重复感。",
    )
    PLAN_MIN_SEGMENTS: int = Field(
        default=14,
        title="日程时段数下限",
        description="每日日程生成的最少时间段数量，数值越大日程越细。范围 3-16。",
        ge=3,
        le=16,
    )
    PLAN_MAX_SEGMENTS: int = Field(
        default=18,
        title="日程时段数上限",
        description="每日日程生成的最多时间段数量；小于下限时自动按下限处理。范围 3-20。",
        ge=3,
        le=20,
    )

    # ==================== 主动陪伴 ====================
    # Bot 主动找陪伴对象说话的节奏控制。留空陪伴对象列表时本组全部不生效。

    PROACTIVE_ENABLED: bool = Field(
        default=True,
        title="启用主动陪伴",
        description="总开关。关闭后不再主动给任何陪伴对象发消息（生活状态注入不受影响）。",
    )
    MAX_DAILY_MESSAGES: int = Field(
        default=3,
        title="每人每日主动消息上限",
        description="单个用户每天最多收到多少条主动消息。",
        ge=1,
        le=12,
    )
    MIN_INTERVAL_MINUTES: int = Field(
        default=90,
        title="同一用户两次主动的最小间隔（分钟）",
        description="防止主动消息刷屏的最小冷却时间。",
        ge=15,
        le=720,
    )
    IDLE_MINUTES: int = Field(
        default=120,
        title="用户安静阈值（分钟）",
        description="用户多久没说话才算「安静」；只有安静超过该时长才会考虑主动搭话。",
    )
    ENABLE_GREETINGS: bool = Field(
        default=True,
        title="启用早晚问候窗口",
        description="早晨 07:30-09:30、晚间 21:30-23:00 问候窗口内主动意愿会提高。",
    )
    IGNORE_BACKOFF: bool = Field(
        default=True,
        title="被忽视自动退避",
        description="用户连续不回复主动消息时自动拉长间隔（最高 3 倍），避免单方面打扰。",
    )

    # ==================== 预算与调度 ====================

    DAILY_TOKEN_LIMIT: int = Field(
        default=200000,
        title="每日 token 预算（0 = 不限）",
        description="插件内部 LLM 调用（日程/状态/梦境）的每日总 token 上限，超限后暂停低优先级生成。",
    )
    QUIET_HOURS_START: str = Field(
        default="23:30",
        title="免打扰开始（HH:MM）",
        description="该时刻之后不再主动发消息，直到免打扰结束。",
    )
    QUIET_HOURS_END: str = Field(
        default="07:30",
        title="免打扰结束（HH:MM）",
        description="该时刻之后恢复主动陪伴。",
    )
    SCHEDULER_TICK_SECONDS: int = Field(
        default=45,
        title="调度器轮询间隔（秒）",
        description="主动陪伴调度器多久检查一次是否需要发消息；调小更及时但更耗资源。",
        ge=15,
        le=300,
    )

def get_config() -> CompanionConfig:
    return plugin.get_config(CompanionConfig)


config: CompanionConfig = get_config()
store = plugin.store
