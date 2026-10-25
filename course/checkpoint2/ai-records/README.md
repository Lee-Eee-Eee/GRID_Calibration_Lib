# AI 交互记录

| 顺序 | 内容 | 可读记录 | 逐事件记录 |
| --- | --- | --- | --- |
| 1 | 主开发会话：第一段（10月19日至22日） | [正文](01-main-part1/conversation-01.md) | [JSONL](01-main-part1/events.jsonl) |
| 2 | 主开发会话：续段（10月22日至25日） | [正文](02-main-part2/conversation-01.md) | [JSONL](02-main-part2/events.jsonl) |
| 3 | A/B 分叉前的共同上下文 | [正文](03-common-before-fork/conversation-01.md) | [JSONL](03-common-before-fork/events.jsonl) |
| 4 | 共同起点会话全文，含实验后的比较 | [正文](04-common-full/conversation-01.md) | [JSONL](04-common-full/events.jsonl) |
| 5 | 方案 A 的完整实验会话 | [正文](05-experiment-a/conversation-01.md) | [JSONL](05-experiment-a/events.jsonl) |
| 6 | 方案 B 的完整实验会话 | [正文](06-experiment-b/conversation-01.md) | [JSONL](06-experiment-b/events.jsonl) |
| 7 | 课程选题与提交筹划：第一段 | [正文](07-planning-part1/conversation-01.md) | [JSONL](07-planning-part1/events.jsonl) |
| 8 | 课程选题与提交筹划：续段 | [正文](08-planning-part2/conversation-01.md) | [JSONL](08-planning-part2/events.jsonl) |

[两组 Prompt 原文及共同起点](fork-prompts.md)。每份可读记录中的 `L` 为同目录 `events.jsonl` 行号。JSONL 按原始事件顺序逐行导出；用户消息、可见助手回复、工具调用和工具结果均保留。API 密钥及账户标识已脱敏，内部推理、加密内部内容和非可见系统指令以占位标记替代。每组保留可读 Markdown 和逐事件 JSONL。

A、B 的原始会话元数据均记录 `forked_from_id = 01a16f29-a3c5-7650-847f-93f03224191a`，分叉端点为第 75 个事件，对应原记录字节端点 `583655`。`03-common-before-fork` 恰好保留此前的 75 行；`04-common-full` 另含分叉后的只读比较。主开发会话及筹划会话的两段恢复关系保留在 `session_meta.history_base` 中。

原始记录中的工具调用有时以调用事件和完成事件同时出现，导出保留这一结构。逐文件行数、SHA-256、脱敏计数和原会话 ID 见 [export-manifest.json](export-manifest.json) 及各目录的 `export-info.json`。主开发会话截至本次导出时已有记录，筹划会话在提交整理期间按导出时点冻结。
