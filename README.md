# Forward Reader - 智能聊天记录分析助手

> 像朋友一样自然地帮你读懂聊天记录，支持多模态内容分析

> 最新版本：`v2.1.0`

适配 [AstrBot](https://github.com/Soulter/AstrBot) >= 3.4.0，已在 v4.25.3 验证。

## 核心功能

- **智能感知**：自动检测用户对合并转发消息的分析意图，无需显式指令
- **多模态分析**：自动识别聊天记录中的图片，使用多模态模型进行分析
- **多层嵌套**：递归解析合并转发中嵌套的其他合并转发
- **独立上下文**：自动分析模式下不携带之前的对话上下文，避免前言不对后语
- **消息去重**：通过消息 ID 哈希缓存防止同一内容被多次分析
- **Token 预算**：超长内容自动分段处理，控制 Token 消耗

## 使用方式

### 引用分析（推荐）
引用/回复一条合并转发消息，机器人会自动感知并分析。可配置 `require_at_mention` 要求同时@机器人才触发。

### 直接发送
直接发送合并转发消息，需要带分析指令（如"分析这段""看看这个"）才会触发。

### @机器人分析
开启主动回复模式后，@机器人 + 发送合并转发可触发分析。

## 配置项

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `enable_smart_detection` | bool | true | 启用智能感知模式 |
| `enable_direct_analysis` | bool | false | 自动分析直接发送的合并转发 |
| `enable_reply_analysis` | bool | true | 分析被引用的合并转发 |
| `enable_proactive_reply` | bool | false | 主动回复模式 |
| `require_at_mention` | bool | false | 引用分析需同时@机器人（避免群友随意引用触发） |
| `sensitivity` | float | 0.7 | 智能感知灵敏度（0.0-1.0） |
| `max_messages` | int | 200 | 单次最大处理消息数 |
| `show_thinking` | bool | true | 分析前发送"让我看看..."等提示 |
| `multimodal_config` | object | - | 多模态分析配置（视觉模型、图片数量等） |
| `performance_config` | object | - | 性能配置（Token 预算、分段摘要等） |

## 更新日志

### v2.1.0
- **修复人格丢失**：手动多模态提供商不再覆盖框架 persona，改用 `_get_persona_prompt()` 从框架获取当前人格
- **新增 `require_at_mention` 配置**：引用合并转发时必须同时@机器人才触发分析，避免群友随意引用就触发（#3）
- **修复分析结果不入历史**：`create_if_not_exists` 改为 `True`，首次分析也能创建会话写入历史（#5）
- `astrbot_version` 改为 PEP 440 范围格式 `>=3.4.0,<5`

### v2.0.0
- 完全重写，支持多模态分析
- 智能感知用户意图
- 全链路结构化日志追踪
- 消息去重与性能优化

## 安装

1. 将插件文件夹放入 AstrBot 的 `data/plugins` 目录
2. 在 AstrBot WebUI 中启用插件
3. 根据需要配置参数（特别是多模态分析相关配置）

## 许可证

AGPL-3.0

## 作者

**EraAsh**