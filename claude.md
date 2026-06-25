# nyabot 功能总览（可读版）

> 目标：用当前代码对齐你最近提到的功能清单，并避免文档再次出现乱码。

## 0. 先说结论
- 已校验：**现有 v1.2.1 代码链路中，主功能未缺失**。
- 主要修复点：`@牛牛喵` 命中、控制命令、follow-up 及静默/概率策略。
- 目前文档以 `CHANGELOG.md` 为版本来源，`claude.md` 记录当前架构与功能分工。

---

## 1. 项目概览
- 运行框架：QQ 群聊 Bot（NoneBot2 + OneBot v11）
- 协作模块：聊天处理 `plugins/ai_chat.py`、召唤控制 `plugins/summon.py`、涩图 `plugins/setu.py`
- 核心服务：`services/` 下的 `deepseek_client`、`world_model`、`data_store`、`group_reply_policy`、`ai_gate` 等

---

## 2. 启动与生命周期
- `bot.py`
  - 读取 `.env`，完成 NoneBot + OneBot v11 初始化
  - 加载 `plugins/` 全量插件
  - 连接回调：`on_bot_connect`（避免频繁启动/关闭提醒）
  - `on_startup`：异步修复关系/昵称索引，启动 `proactive/impression/event` 周期任务
  - `on_shutdown`：可选清理

---

## 3. 插件功能（plugins）

### 3.1 `plugins/ai_chat.py`
- 响应优先级：`priority=10`
- 核心职责：
  - 群消息解析（含 `@` 提及、文本提取）
  - 记录上下文压缩日志
  - 人设守护后再决定输出
  - AI 调用失败 fallback
  - 表情贴纸/语音输出整合
  - gate 冷却 + 回复概率控制
- 控制命令分支：
  - `bot mute 12h`：全局 12 小时静默（只保留被动提及回复）
  - `牛牛喵闭嘴！`：同上（同名静默）
  - `牛牛喵归来！`：解除静默
  - `bot reply 10%`：临时设置群回复概率
  - `bot reply default`：恢复默认
  - `bot start / bot stop / bot voice on / bot voice off / image / photo / sticker`

### 3.2 `plugins/summon.py`
- 响应优先级：`priority=1`（高于 AI）
- `group_state` 控制是否在当前群开启召唤状态
- 召唤命令：
  - `牛牛喵召唤术！`：开启聊天状态
  - `牛牛喵遣返术！`：关闭召唤状态
  - `牛牛喵 语音模式` / `牛牛喵 文字模式`：切换语音输出
- 自动退出：超过时长或长时间不活跃自动退下

### 3.3 `plugins/setu.py`
- 响应优先级：`priority=5`
- 指令集合包括：`牛牛喵快逃`、`牛牛喵退后`、`牛牛喵撤回`
- 支持图片随机返回、部分撤回保护逻辑，非命中命令时放行给 `ai_chat`

---

## 4. 服务层（services）

### 4.1 AI 与对话
- `services/deepseek_client.py`
  - 调用主模型
  - 失败重试与安全 fallback
- `services/world_model.py`
  - 聚合群体上下文、用户关系、情绪、记忆给提示词
- `services/ai_gate.py`
  - 冷却、概率、群级/全局开关判断

### 4.2 数据与记忆
- `services/data_store.py`
  - 用户数据 `data/members.json`
  - 群成员映射 `data/group_members.json`
  - 群元信息与用户画像读写接口
- `services/context_compressor.py`
  - 消息上下文压缩，保留关键对话线索
- `services/memory.py`
  - 记忆片段持久化与查询
- `services/relation.py`
  - 关系图谱（交互、亲密度）
- `services/persona_guard.py`
  - 人设防线：检测不合适输出，必要时重试/兜底
- `services/nya_personality.py`
  - 牛牛喵口头禅、观点、个性描述
- `services/mood.py`
  - 群聊情绪状态（快慢衰减 + 互动加减）

### 4.3 自动化与增强
- `services/proactive.py`
  - 群内主动发言调度（受 `summon` 与状态限制）
- `services/impression.py`
  - 群印象更新
- `services/group_events.py`
  - 群事件记录与摘要骨架
- `services/group_reply_policy.py`
  - 群级回复概率参数持久化
- `services/mention_resolver.py`
  - `@` 提及与别名解析（新增 CQ raw 兜底）
- `services/sticker.py`
  - 表情抓取/生成/回复
- `services/tts.py`
  - 文本转语音（Edge TTS）
- `services/utils.py`
  - 噪声检测（`is_noise`）

---

## 5. 数据文件
- `data/members.json`：用户资料、别名、关系元信息等
- `data/group_members.json`：群内成员映射（建议优先来源）
- `data/group_reply_policy.json`：群内回复概率策略
- `data/group_events.json`：群事件记录
- `data/mood.json`：情绪状态
- `data/nya_memory.json`：牛牛喵长期记忆与偏好

---

## 6. 近期版本核对（最近 + 关键历史）

### 版本线
- `v1.2.1`：修复 `@` 提及漏检与静默/冷却判断，补强 `ai_chat` 可读性
- `v1.2.0`：控制命令增强、follow-up 及召唤相关判定收敛
- `v1.1.1`：`claude.md` 与 `group_members` 迁移核对
- `v1.1`：世界模型、上下文与会话关系增强
- `v0.9`：人设守护与 AI fallback 重试
- `v0.8`：回复策略修复（命令词/提及强制/静默）
- `v0.7`：召唤状态联动与主动发言策略
- `v0.6`：关系/心情/印象/记忆链路落地
- `v0.5`：sticker、tts、setu 相关能力扩展
- `v0.4`：关系系统升级与主动发言基础
- `v0.2`：召唤机制雏形
- `v0.1`：项目起步

---

## 7. 常见问题回退
- 若 `@牛牛喵` 常不响应：确认事件解析是否成功命中（群@、文本、调用词）
- 若出现重复回复：检查是否同时命中 `summon` + `ai_chat` 的响应链路
- 若出现乱码：清理 `CHANGELOG.md` / `claude.md` 本地编码；当前文件使用 UTF-8

---

## 8. 运行入口
```bash
cd nyabot
nb run
```

---
