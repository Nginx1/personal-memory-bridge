# 首版运行与恢复

## 分阶段执行

1. 只读扫描少量资料，保存脱敏来源档案并检查内容；不调用模型。
2. 使用独立 bank 做虚构中文资料试验，验证来源、否定、时间更新、资料范围和未知问题。
3. 导入少量真实资料，比较记忆与原文；通过后再增加范围。
4. 对采集和后台整合设置明确责任方，避免 Hermes 原生插件与历史采集器同时写入同一内容。
5. 部署长期服务后才启用定期采集和客户端自动召回。不要先全量回填再调规则。

## 已验证的开发环境版本

- Python 3.13，Windows x86_64。
- `hindsight-api-slim[embedded-db,local-onnx]==0.10.2`，`hindsight-client==0.10.2`。
- 多语言向量：`intfloat/multilingual-e5-small`，试验下载版本 `614241f622f53c4eeff9890bdc4f31cfecc418b3`，384 维，query/passage 前缀。
- pg0 的 PostgreSQL 18.1.0 与 pgvector；嵌入式数据库仅用于开发验证。
- MCP 试验使用 `fastmcp==4.0.10`。

```sh
pip install 'hindsight-api-slim[embedded-db,local-onnx]==0.10.2' 'hindsight-client==0.10.2'
```

官方 [安装说明](https://hindsight.vectorize.io/developer/installation) 支持 Windows pg0，并建议生产使用外部 PostgreSQL。长期 Linux 服务的资源安排应按实测 RSS、并发和备份空间确定。

## 试验配置

以下是环境变量模板。模型入口应指向本项目 `cost_proxy()` 创建的 loopback 闸门，真实上游密钥仅由闸门进程持有。提供者、固定模型、真实人民币单价与预留额度要一起配置。

```dotenv
HINDSIGHT_API_DATABASE_URL=pg0://personal-memory-development
HINDSIGHT_API_LLM_PROVIDER=openai
HINDSIGHT_API_LLM_MODEL=YOUR_PINNED_MODEL
HINDSIGHT_API_LLM_BASE_URL=http://127.0.0.1:YOUR_COST_GATE_PORT/v1
HINDSIGHT_API_LLM_API_KEY=EPHEMERAL_GATE_TOKEN
HINDSIGHT_API_LLM_MAX_RETRIES=0
HINDSIGHT_API_LLM_MAX_CONCURRENT=1
HINDSIGHT_API_WORKER_MAX_RETRIES=0
HINDSIGHT_API_RETAIN_MAX_COMPLETION_TOKENS=4096
HINDSIGHT_API_ENABLE_AUTO_CONSOLIDATION=false
HINDSIGHT_API_ENABLE_TEMPORAL_RETRIEVAL=false
HINDSIGHT_API_EMBEDDINGS_PROVIDER=onnx
HINDSIGHT_API_EMBEDDINGS_ONNX_MODEL_ID=intfloat/multilingual-e5-small
HINDSIGHT_API_RERANKER_PROVIDER=rrf
HINDSIGHT_API_ENABLE_RERANKING=false
HINDSIGHT_API_WORKER_ID=personal-memory-development
```

费用记录包括启动时的模型连接验证。4096 是每次提炼的试验输出上限，含推理占用；遇到截断不能直接当作提炼完整。费用闸门仅支持固定文本模型、非流式、单个 completion；未知/多模态请求拒绝。

真实样本中，4096 上限引发拆段重新提炼及同步请求超时。修正试验使用 8192 上限、异步 operation 和禁止 worker 自动重试；提高上限不会强制模型花满额度。不要通过一味压低输出上限追求节省，须同时观察实际 reasoning 用量、截断和重新提炼次数。

已验证的连接无需反复付费检查；可以在明确的试验中设置 `HINDSIGHT_API_SKIP_LLM_VERIFICATION=true`，让第一条受限提炼同时验证连接。此配置不保证模型可用，失败仍计入账本。

`cost_proxy(..., disable_thinking=True)` 显式设置 DeepSeek 格式的 `thinking.type=disabled`，并移除冲突的 `reasoning_effort`。默认关闭这个改写选项；仅在所选供应商和固定模型已验证兼容时启用。先验证否定、日期和证据边界，不因费用降低就直接扩大真实资料范围。首轮结果见 [试验报告](EVALUATION.md)。

`Ledger(path, ceiling_cny=200, max_calls=30)` 记录所有尝试；更改已有账本政策需明确迁移。`Tariff(input_per_million, output_per_million, cached_per_million)` 使用人民币/百万 token。输出 token 已包含的 reasoning 部分不再次相加。费用是按声明单价计算的估计值，实际账单以供应商为准；调价、额外请求收费或不遵守输出限制会影响预留上界。

关闭自动 Consolidation/Temporal Retrieval 的试验不代表完整观察记忆、时间检索和知识页能力已经验证。启用这些功能时仍需通过同一费用闸门。

Windows 系统代理需同时保留外网代理和对 `127.0.0.1,localhost` 的 `NO_PROXY`；只新增 `NO_PROXY` 可能改变 Python 的注册表代理回退。模型已缓存后可以启用 Hugging Face offline 模式，避免启动时重复联网。

## 客户端配置模板

Codex 本地 stdio MCP 的配置示例：按本机绝对路径替换命令与参数。

```toml
[mcp_servers.personal_memory]
command = "/ABSOLUTE/PATH/TO/PYTHON"
args = ["-m", "personal_memory.cli", "--archive", "/PRIVATE/PATH/archive.db", "mcp", "--url", "http://127.0.0.1:8889", "--bank", "my-personal-bank"]
enabled_tools = ["memory_search", "memory_source"]
```

让客户端在需要个人偏好或已有经验时检索，必要时读来源。记忆内容作为资料，不作为新的系统指令。缺少直接依据时应回答未知；发生矛盾时展示日期和来源，不自行把旧记录覆盖成新事实。

模板依据 [OpenAI MCP 文档](https://learn.chatgpt.com/docs/extend/mcp?surface=app)。多机器部署、SSH 强制命令、增量来源游标和两个客户端的配置见 [部署说明](DEPLOYMENT.md)。Hermes 应只选一种写入方式，避免原生自动保存与采集器重复计费。

## 备份与恢复

- 来源档案：`personal-memory --archive data/archive.db backup /PRIVATE/BACKUP/archive.db`，使用 SQLite backup API；以恢复副本的 stats 与源内容比对。
- Hindsight：定期使用 PostgreSQL `pg_dump -Fc` 备份完整记忆数据库；对全新隔离数据库运行 `pg_restore`，核对来源数、记忆数、版本、标签与检索结果。
- 备份模型配置、向量模型版本和维度；换模型即使维度相同也需重新建索引，不能直接混用旧向量。
- Markdown 来源导出用于查看和重新提炼，不含全部后端实体图和观察历史，不能当作完整 Hindsight 备份。
- 生成的主题页/事实导出必须写到独立 `_memory/` 或导出目录，并从采集范围排除。

恢复时不要连接原生产数据库，不要启动后台模型作业；先使用只读/无 LLM 模式核对，避免恢复过程自动发生费用。
