# nyabot

nyabot 是基于 NoneBot2 + OneBot v11 的 QQ 群聊机器人，主打 AI 对话、群内被动/主动响应、人物画像与风格模型。

## 一、已核验功能（按 `CLAUDE.md` + `CHANGELOG.md` 校对）

按当前代码已逐项核验如下：

- `summon`（召唤模式）
  - `plugins/summon.py`
  - 文档声明：支持 `开始召唤` / `解除召唤` / `开启语音模式` / `开启文字模式`
  - 代码实现：
    - `group_state`、`group_start_time`、`group_last_active`、`group_voice_mode` 四类状态存在且有默认值
    - `activate / deactivate / check_auto / on_message` 处理上述 4 个关键词
  - 结论：**一致**。

- AI 对话主链路
  - `plugins/ai_chat.py`
  - 文档声明：消息处理、`@机器人` 强制触发、冷却 + 噪声过滤 + 触发概率
  - 代码实现：
    - `parse_at_from_event` / `resolve_mentions`
    - `group_state` 自动模式检查
    - `AIGate.should_reply(cooldown/reply_prob/noise)` 与 `force_reply`
    - `ask_ai` 与世界模型构建（`world_model.build`）
  - 结论：**一致**（与文档预期对齐）。

- 被动回复风格与表情
  - `services/sticker.py`、`services/tts.py`
  - 文档声明：QQ 表情/图片回复、文字优先、支持图片与语音模式
  - 代码实现：`reply_with_sticker()` 包含随机表情/图片，`_send_reply()` 按 `group_voice_mode` 发送 voice/text
  - 结论：**一致**。

- 人设与人格约束
  - `services/world_model.py`、`services/deepseek_client.py`、`services/persona_guard.py`
  - 文档声明：深度提示词 + 人设 + 自动反向校验
  - 代码实现：`SYSTEM_PROMPT` + `world_model.build` + `persona_guard.check`，`_safe_ask` 有重试与回退
  - 结论：**一致**。

- 主动推送与互动
  - `services/proactive.py`
  - 文档声明：按群状态/热度/目标用户进行主动发言
  - 代码实现：`proactive_loop()` 60s 循环、`check_auto()`、`pick_target()`、`build_proactive_prompt()`、`ask_ai()`、支持语音/文字
  - 结论：**一致**。

- 历史上下文与世界模型输入
  - `services/context_compressor.py`、`services/memory.py`
  - 文档声明：保留群内上下文、事件与长期记忆
  - 代码实现：`record_message()`、`compress()`、`group_events.build_context()`（间接接入）、`memory.build_context()`
  - 结论：**一致**。

- 核心群员重点记忆（新增）
  - `data/core_members.json`（新增）
  - `services/data_store.py`、`services/world_model.py`、`services/memory.py`
  - 说明：将核心群员写入 `core_members.json` 后，对应成员关联的长期记忆会写入 `importance=2` 并优先参与 World Model 构建；群上下文筛选也会优先带入核心群员。

- 用户画像与关系
  - `services/data_store.py`、`services/profile_updater.py`、`services/relation.py`
  - 文档声明：`members.json` 用户画像、relations/affinity/interaction 与自动标签
  - 代码实现：`ensure_user/update_user/update_tags/update_affinity/record_interaction`
  - 结论：**一致**。

- 日常异步任务与事件摘要
  - `services/impression.py`、`services/group_events.py`、`bot.py`
  - 文档声明：15min 印象更新、30min/日内事件归档（22~23点）
  - 代码实现：启动时注册 `impression_loop` + `event_loop`
  - 结论：**大体一致**（见下方问题清单中的实现边界）。

- 目录结构与运行方式
  - `CLAUDE.md` 与 `CHANGELOG.md` 中列出的目录结构/命令在仓库中均能匹配到
  - 结论：**一致**。

## 二、已记录问题（README 风险清单）

> 按优先级排列，便于你按顺序处理。

1. **[高] 密钥明文**
   - `nyabot/.env` 中包含 `ONEBOT_ACCESS_TOKEN`、`DEEPSEEK_API_KEY`。
   - 建议：改为外部注入（CI/运行时环境变量），并立即轮换已泄露风险密钥。

2. **[高] 图片收集存在安全面（已修复）**
  - `services/sticker.py` 已添加图片来源白名单（`https` + QQ 图片域名）、`Content-Type` 与 `Content-Length` 校验、每条消息最多采集 2 张图片、大小范围限制（1KB~3MB），并保留旧有失败兜底。

3. **[中] 权限未隔离的高影响指令**
   - `plugins/summon.py` 与 `plugins/setu.py` 的关键命令可被任意群成员触发。
   - 建议：增加管理员/白名单权限控制。


## 三、项目建议配置（安全与运行）

- 运行前先确认环境变量：
  - `DEEPSEEK_API_KEY`
  - `DEEPSEEK_BASE_URL`
  - `MODEL`
  - `GROUP_ID`（可选，影响启动/退出通知）
- 生产环境建议不要让 `.env` 长期存放真实密钥。
- `nb run` 为启动入口（依赖 OneBot/NapCat 连接地址：`ws://127.0.0.1:8080/onebot/v11/ws`）。

## 四、文档核验记录

- 已核验文档：
  - [CLAUDE.md](/C:/QQBot/nyabot/CLAUDE.md)
  - [CHANGELOG.md](/C:/QQBot/nyabot/CHANGELOG.md)
- 结论：当前实现总体与文档一致，但存在上述风险点（见“已记录问题”）。
