# 私人陪伴 (Private Companion)

让 nekro-agent 的 bot 拥有**连续的生活感**——每天有自己的日程、能量、心情、身体小状态，夜里做梦——并对指定的陪伴对象进行**有节制的主动陪伴**。附带 WebUI 控制面板。

> 💡 **核心理念移植自 AstrBot 插件 [astrbot_plugin_private_companion](https://github.com/menglimi/astrbot_plugin_private_companion)**（精简适配版）。
> 原版是功能极其丰富的超级插件；本插件只移植"生活状态机 + 主动陪伴决策"的精华，记忆、人设、对话全部交给 nekro-agent 原生体系，主动消息通过 nekro 原生 timer 唤醒 agent 自己组织语言（带完整人设/记忆/上下文，对话历史完整记录）。

> 🔧 **本仓是 [miuzhaii/nekro-plugin-private-companion](https://github.com/miuzhaii/nekro-plugin-private-companion) 的分支**（分叉点 `0cc193d`），在上游基础上做了精简与本地化改造，差异见 [本分支相对上游的差异](#-本分支相对上游的差异)。

## ✨ 功能

- **🌅 生活状态机**：每天自动生成贴合人设的日程（LLM）、能量/心情/身体小状态（本地规则）、昨夜梦境（LLM）
- **💭 状态注入**：把"此刻在做什么、能量心情、梦境余韵"注入对话提示词，bot 的回复自然带生活背景，不再是"随叫随到的真空 AI"
- **💌 主动陪伴**：对陪伴列表中的用户，结合日程事件/身体状态/梦境/用户上次发言挑选动机，在合适的时机主动找 TA 聊天
  - 每人独立：每日配额、最小间隔、关系分、连续被忽视自动退避
  - 全局约束：免打扰时段、问候窗口、bot 自己"睡着"时不主动
  - 关键设计：插件只决定"**何时**主动、**为什么**主动"，消息本身由被唤醒的 agent 用人设和记忆自己写
- **🖥️ WebUI 面板**：总览（状态/能量/token 消耗）、陪伴对象管理（启停/配额/立即主动）、生活记录（日程时间线/梦境）、操作（重新生成/注入预览）
- **💰 token 预算**：内部 LLM 调用（日程/梦境）有每日预算上限，超限自动暂停低优先级生成
- **🧩 角色内容外置**：世界观舞台、场景池、事件池、触发别名都在插件数据目录里，不在源码里 —— 同一份代码可以服务多个角色

## 🚀 安装

```bash
cd /root/srv/nekro_agent/plugins/packages/   # 以实际插件目录为准
git clone https://github.com/NTidal/nekro-plugin-private-companion private_companion
docker restart nekro_agent
```

> ⚠️ 目录名必须是 `private_companion`。重启后在 **WebUI → 插件管理** 中启用本插件（新插件默认禁用，启用后路由才会挂载）。
>
> 插件 key 与上游一致（`xiaojiu.private_companion`），所以从上游换过来时数据目录、配置都不用改。

无额外 Python/第三方插件依赖：只使用 nekro-agent 容器内置库。

## 🧩 角色内容与数据文件

角色专属内容**不写在源码里**，而是放在插件数据目录：

```text
{NA数据目录}/plugin_data/xiaojiu.private_companion/
  world_stage.txt        世界观舞台：活动范围 + 禁止出现的世界外元素
  day_card.json          场景池 / 事件池 / 场景冷却天数
  trigger_aliases.json   触发词别名（书面词 → 口语变体）
  config.yaml            插件配置（人格、模型组、陪伴对象等）
```

- 三个文件按 `(mtime, size)` 缓存，**改完立刻生效**，不需要重启或 reload 模块。
- 文件缺失或损坏时用内置兜底（世界观为空、场景池为 8 条通用日常），不会崩。
- WebUI「角色内容」页可直接编辑世界观与场景/事件池，每次保存自动备份到 `content_backups/`。
- 导出/导入（`/api/backup/*`）用的就是这三个文件的格式，所以**导出包 = 数据快照**，导入 = 校验后写回数据文件（失败整体回滚），全程不改 `.py`。

因此：**升级插件源码不会碰角色内容**，多实例也能共用同一份代码、各用各的数据。

## ⚙️ 配置（WebUI → 插件管理 → 私人陪伴）

| 关键配置 | 说明 |
|---|---|
| `TARGET_USER_IDS` | **陪伴对象 QQ 列表**（留空 = 只注入生活状态，不主动发消息）|
| `MANAGE_SUPER_ADMINS` | 可用全部 `/陪伴` 管理命令的管理员 QQ 列表 |
| `PERSONA_PRESET_ID` / `PERSONA_PROMPT` / `PLAN_STYLE_HINT` | 人格来源：自定义描述 > 指定人设 > 系统默认人设；`PLAN_STYLE_HINT` 用于补充生活线约束 |
| `MAX_DAILY_MESSAGES` | 每人每日主动上限（默认 3）|
| `MIN_INTERVAL_MINUTES` / `IDLE_MINUTES` | 主动最小间隔 / 用户安静阈值 |
| `QUIET_HOURS_START` / `QUIET_HOURS_END` | 免打扰时段（默认 23:30-07:30）|
| `INJECT_ENABLED` / `INJECT_SCOPE` | 状态注入开关 / 范围（所有会话 或 仅陪伴对象私聊）|
| `DAY_CARD_ENABLED` | 每日人生卡（随机场景 + 两件小事件）|
| `PLAN_MIN_SEGMENTS` / `PLAN_MAX_SEGMENTS` | 每日日程时段数范围（默认 5-8；调大 = 更精细的 1-2 小时粒度）|
| `DAILY_TOKEN_LIMIT` | 插件内部 LLM 每日 token 预算 |
| `MODEL_GROUP` / `STATE_MODEL_GROUP` | 日程与状态生成用的模型组 |

## 📝 命令（`/陪伴`，管理员或陪伴对象本人可用）

| 子命令 | 说明 |
|---|---|
| `状态` | 今日概括、当前时段、能量心情、各用户配额 |
| `日程` / `日程生成`* | 查看 / 强制重新生成今日日程 |
| `梦境` | 查看昨夜梦境 |
| `主动 开\|关 [QQ]` | 开关本人（管理员可管他人）的主动陪伴 |
| `判定 [QQ]` | 调试：显示当前能否主动及原因、候选动机 |
| `注入预览`* | 查看当前注入到对话的生活状态文本 |

（* 仅管理员）

## 🖥️ WebUI 面板

启用插件后访问：`http://<nekro地址>:8021/plugins/xiaojiu.private_companion/`

> 🔐 **鉴权**：面板 API 复用 nekro 主 WebUI 的管理员登录态（同一 JWT）。请先在**同一浏览器**登录 nekro 后台，再打开面板；未登录访问 API 一律返回 401。

## 🔧 本分支相对上游的差异

对照 [miuzhaii/nekro-plugin-private-companion](https://github.com/miuzhaii/nekro-plugin-private-companion)：

**移除**

- 日程图卡渲染：`schedule_card.py`、`visuals.py`、`napcat_share.py`
- 日程自拍出图：`image_gen.py`、`selfie_draw.py`
- 每日日记：`generate_diary` / `maybe_generate_diary_by_time` / `register_after_daily_plan` 等
- 上述功能的测试与 `img/` 截图

**新增**

- **世界观舞台**：给日程/梦境生成器加一段硬约束，防止 LLM 把角色写成现代都市生活（挤城铁、点外卖、被拉去加班）。实测不加约束时几乎必出戏。
- **好感度门控**：主动陪伴前读 [nekro_persona](https://github.com/NTidal/nekro_persona) 的好感度与背景记忆的 `min_favor`，**未解锁的话题不主动提起**——主动提起等于自己泄底，比被动被问更糟。
- **背景记忆注入**：生成日程/梦境时带上 1 档背景记忆，避免生成与角色设定冲突的生活线。
- **角色内容外置**：上游把角色内容写死在 `core.py` / `day_card.py` / `proactive.py`，导入人设包靠**正则改写源码**；本分支改为读写数据文件，源码里不含任何角色内容（见上一节）。
- **`deploy.ps1`**：多实例源码同步 + 一致性校验。

**同源未改**：`busy_gate.py`、`chronotype.py`、`plan_diversity.py`、`proactive_queue.py` 及其测试、`LICENSE`。

## 🔧 与 AstrBot 原版的差异

（这一节是上游 README 原有的，比的是 AstrBot 原版）

- 仅保留：生活状态机、主动陪伴决策、WebUI；**不含**群聊观察、QQ空间、新闻探索、创作书柜、漫画阅读等外围功能
- 用户记忆/人设/对话上下文全部使用 nekro-agent 原生能力（原版自建的记忆系统不再需要）
- 主动消息由 agent 本人生成（nekro timer 唤醒机制），不是插件代笔，风格与日常对话完全一致
- 状态机大幅精简（原版 daily_state 有 6000+ 行，本版核心字段化）

## 📦 版本

`plugin.py` 声明的版本仍是 `0.3.1`（与上游分发包 `private_companion-v0.3.2.zip` 内的版本号一致）。

暂未单独升版本号：该字符串会写进导出包的 `companion.bundle.json`，升号会让已归档的人设包与线上不一致，留到下次正式发布时一起处理。

## 🚀 多实例部署

```powershell
.\deploy.ps1                 # 同步源码到各实例 + 重启 + 校验一致
.\deploy.ps1 -NoRestart      # 只复制，不重启
.\deploy.ps1 -Targets NA3    # 只同步指定实例
```

脚本只同步源码，**不碰数据文件**（角色内容各实例独立）。跑完会打印各实例的 md5 一致性表格。

## 🙏 致谢

- 上游（nekro-agent 移植版）：[miuzhaii/nekro-plugin-private-companion](https://github.com/miuzhaii/nekro-plugin-private-companion)
- 原插件（AstrBot 版）：[menglimi/astrbot_plugin_private_companion](https://github.com/menglimi/astrbot_plugin_private_companion)
- WebUI 挂载模式参考：[wess09/nekro_plugin_prompt_injector](https://github.com/wess09/nekro_plugin_prompt_injector)
