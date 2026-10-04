# Personal Memory Bridge

从 Markdown、Hermes 和 Codex 会话中收集有用资料，通过同一记忆后端供多个 AI 查询。减少手工写笔记，保留来源、版本和证据边界。

首版为可运行试验原型。记忆提炼和向量检索复用 [Hindsight](https://github.com/vectorize-io/hindsight)，本项目负责采集、去重、来源档案、费用控制和精简 MCP 接口。已在 Linux 部署并连接真实 Hermes 与 Windows Codex 客户端；长期稳定性、全部历史回填和官方网页聊天自动采集仍待验证。

## 已实现

- Markdown 文件、Codex JSONL、Hermes SQLite 的只读适配器。
- 固定来源 ID、内容哈希、增量写入、修订记录；同一逻辑库的不同挂载不重复导入。
- 跳过 Codex 内部推理、重复事件、子代理上下文和大体积工具输出；保留小型结构化执行回执，标记为观察结果。
- 原文角色、时间、否定和“助手宣称”的标记；不根据命令成功就认定任务完成。
- 独立的脱敏来源档案、Markdown 导出、一致性 SQLite 备份。
- Hindsight Retain/Recall 接入。重复推送已同步版本不再触发提炼；来源变化后旧摘要不作为当前证据返回。
- 两个只读 MCP 工具：`memory_search`、`memory_source`。范围由服务配置固定，客户端不能随意扩大。
- 模型调用前原子预留预算，限制请求数和输出上限；失败或未知用量保留保守费用估计，避免当作免费。
- 错误派生记忆可按来源版本记录排除原因；MCP 和 CLI 查询应用同一纠错记录。
- 启用日期筛选、完成会话筛选；远程来源通过受限 SSH 命令传输，写入与检索使用不同密钥。
- 来源变更日志与确认游标。单批默认 20 个变更，确认丢失可重放；源日志文件未变化时跳过解析，不把整个累计历史传给模型。
- 受费用闸门约束的独立提炼 worker、服务就绪等待与模型缓存离线启动。

自动提炼所得内容仍需检查。来源笔记可能过时，助手答复可能有误；来源链接用于核对原文，不能替代实际工作证据。

首轮已完成 6 条真实来源的小样本提炼、MCP 调用和完整备份恢复。效果、费用、失败案例和未完成项见 [试验报告](docs/EVALUATION.md)。

## 本地运行

Python 3.11+；采集核心使用标准库。以下命令在项目目录执行：

```sh
python -m venv .venv
# Windows: .venv\Scripts\activate
# Linux/macOS: source .venv/bin/activate
pip install -e .
python -m unittest discover -s tests -v
```

先在本地采集并查看，不会调用模型：

```sh
personal-memory --archive data/archive.db markdown /path/to/vault --source my-vault --include 'notes/**'
personal-memory --archive data/archive.db codex /path/to/one-completed-session.jsonl
personal-memory --archive data/archive.db hermes /path/to/state.db --session SELECTED_SESSION_ID
personal-memory --archive data/archive.db stats
personal-memory --archive data/archive.db export data/source-export
personal-memory --archive data/archive.db backup data/archive-backup.db
```

持续采集使用 [来源配置模板](examples/sources.example.json)，把私有路径配置保存到仓库外：

```sh
personal-memory --archive data/archive.db collect --config /PRIVATE/PATH/sources.json --interval 300
```

采集轮询只更新本地档案，模型调用为零；提炼另由受预算约束的 worker 执行。`collect` 为直接采集；多机器部署使用 `personal_memory.sync` 的变更日志与确认游标，见运行说明。未变化的 Codex 文件和 Hermes 数据库可跳过解析；变化的大文件仍需重新解析，尚未实现文件内的按字节续读。

对同一个库始终使用相同的 `--source`，即使物理路径发生变化。生成的 `_memory/` 默认排除，避免整理结果循环导入。使用明确的目录和会话白名单；首轮只选少量资料。Hermes 读取使用只读事务；远程活动数据库应先做一致性快照，不能只复制主库文件。

连接已配置好的 Hindsight 后端：

```sh
personal-memory --archive data/archive.db push --url http://127.0.0.1:8889 --bank my-personal-bank --limit 10 --async
personal-memory --archive data/archive.db recall --url http://127.0.0.1:8889 --bank my-personal-bank '我最近决定怎么备份资料？' --max-tokens 1024
pip install -e '.[mcp]'
personal-memory --archive data/archive.db mcp --url http://127.0.0.1:8889 --bank my-personal-bank
```

`push` 会调用后端模型；生产服务应通过费用闸门使用模型，不能只限制客户端请求而放任后台整合、重试或页面刷新。后端试验配置见 [运行说明](docs/OPERATIONS.md)。

发现错误提炼时，在私有档案中排除对应记忆，保留原文与原因；该操作不调用模型：

```sh
personal-memory --archive data/archive.db exclude-memory DOCUMENT_ID MEMORY_ID --reason '原文不支持这条推断'
```

排除仅对当时的来源版本生效，不删除后端审计资料；来源更新后重新核对。绕过本项目直连 Hindsight 的工具不会应用此排除记录。

## 设计边界

原始资料继续由 Obsidian 和各 AI 的历史系统保存。本项目的 SQLite 是脱敏来源档案与同步状态，不代替 Hindsight 的记忆数据库。Hindsight 更新使用固定 document ID；`push --async` 以 bank、来源 ID 和内容版本生成固定 operation ID，在后端操作记录的保留窗口内恢复同一任务，避免确认消息丢失后重复排队。失败的操作不自动重排；恢复或重新提炼须检查失败原因。同步模式网络超时后的人工重试仍可能重做提炼，尚未宣称跨系统 exactly-once。

服务默认返回最多四条当前来源的记忆，并在需要时展开原文。当前试验按多语言语义分数排序；分数不是置信概率，不能把检索结果直接当作问题已回答。日期条件、未知问题拒答和更大语料的质量还需进一步评估。

不同资料范围使用独立 bank 与服务配置。标签过滤是查询边界，公开远程服务仍需身份验证和访问控制。首版 MCP 采用 stdio，跨机器通过 SSH 公钥与强制命令接入；不要直接把未经鉴权的试验 HTTP 端口暴露到公网。

目前一个来源档案固定连接一个后端 bank；不要更换 `--bank` 后沿用原档案的同步状态。迁移模型、提炼策略或 bank 时须安排明确的重建与验证流程。

现阶段不自动删除源文件消失后的远端记忆，也不自动全量回填、不启用对整个历史库的后台提炼。源记录消失、修改已有采集窗口或重建来源变更日志时，须显式检查游标与来源版本。自动采集已接通，运行期限与更大数据量仍需持续验证。

## 开源与资料

MIT 许可证。仅代码、文档和虚构样例准备公开；真实记录、机器地址、凭据与试验数据库放在仓库之外。凭据规则过滤是辅助措施，发布前仍须检查。模型权重不随项目分发，各依赖及模型遵循各自许可证。

- [虚构中文验收样例](examples/fictional.json)
- [运行与备份说明](docs/OPERATIONS.md)
- [多机器部署与自动采集](docs/DEPLOYMENT.md)
- [Hindsight Hermes 集成](https://hindsight.vectorize.io/sdks/integrations/hermes)
- [Hindsight Obsidian 集成](https://hindsight.vectorize.io/sdks/integrations/obsidian)
- [Hindsight Codex CLI 集成](https://hindsight.vectorize.io/sdks/integrations/coding-agents)

官方 Obsidian 集成仍为 Beta；Codex CLI hooks 支持不自动等同于桌面端支持。网页版 AI 需逐个平台验证采集和调用能力。
