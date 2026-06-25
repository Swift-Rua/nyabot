# 更新日志

## v1.2.1 (2026-06-24) - 功能稳定与异常修复
- 修复 `ai_chat` 的 `@` 提及解析分支，增加 CQ 文本兜底和 bot mention 识别，降低“@牛牛喵”不回应。
- 完善 `ai_chat` 冷却与静默状态判断，避免高优先级指令被 `force_reply` 漏过。
- 记录群成员映射迁移，`members.json` 与 `group_members.json` 的结构同步更稳定。

## v1.2.0 (2026-06-24) - 模式与响应行为增强
- 新增/修正控制命令入口：`bot start/stop`、`bot voice on/off`。
- `SUMMON_COMMANDS` / `SETU_COMMANDS` 合并到 ai_chat 与命令路由的协同处理。
- 增加 follow-up 机制与更多回复策略边界，降低重复 / 卡顿问题。

## v1.1.1 (2026-06-24) - 文档清点与数据结构整理
- 补充 `claude.md` 的功能核对说明。
- 整理 `group_members` 数据迁移逻辑，保证成员关系与 `data/members.json` 不再混写。
- 优化 `bot.py` 启停后的生命周期处理。

## v1.1 (2026-06-23) - 世界模型与上下文改造
- 引入群像能力：世界模型上下文、关系/心情/记忆摘要接入 AI 提示词。
- `ai_chat` 增加更强的消息解析与上下文压缩记录。
- 引入 `group_events` 的事件摘要思路（后续版本逐步完善）。

## v0.9 (2026-06-23) - 人设稳定与失败兜底
- 新增 `services/persona_guard.py`。
- `ai_chat.py` 增加 `_safe_ask()` 与重试/兜底输出逻辑。

## v0.8 (2026-06-23) - 回复策略与控制逻辑修复
- 调整 AI 回复判定链路：命令词、bot 提及、follow-up 与静默状态分层执行。
- 增加群级静默与概率控制命令：`bot mute 12h`、`bot reply 10%`、`bot reply default`。
- 调整 `gate.should_reply()` 与 `force_reply` 的分支关系，减少误触发。

## v0.7 (2026-06-23) - 召唤模式与主动发言联动
- `summon` 优先级上移并接入群状态判断。
- 支持 20 分钟召唤时长与 3 分钟无互动自动退出。
- `proactive.py` 增强群内主动发言条件。

## v0.6 (2026-06-23) - 画像增强
- 建立关系 / 心情 / 印象 / 记忆的基本链路。
- `relation/mood/impression/memory/world_model` 进入日常上下文构建。

## v0.5 (2026-06-23) - 画像与内容能力扩展
- 引入 `relation.py` 与 `mood.py`。
- 引入 `sticker.py`（采集与回复）。
- 增加 `tts.py` 语音输出模块。

## v0.4.5 (2026-06-23) - World Model 首次引入
- 增强 `services/world_model.py` 与 `deepseek_client.py` 的上下文组装。

## v0.4 (2026-06-23) - 关系系统升级
- 别名匹配与关系图（affinity / interaction / last_chat）结构化。
- `proactive.py` 引入关系权重和历史互动信息。

## v0.2 (2026-06-23) - 召唤与召回机制雏形
- 初步实现召唤/遣返/自动退出逻辑。
- 自动化任务循环接入群状态。

## v0.1 (2026-06-23) - 项目起步
- 引入基础 `data_store`、`persona_guard`、`world model`、`memory`、`mood` 与 `setu/impression` 等基础模块。
- 初始化 `CHANGELOG.md` 与 `claude.md`。
