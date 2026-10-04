# 多机器部署

已验证的组合为 Ubuntu 24.04、Python 3.12、PostgreSQL 18 + pgvector、Hindsight 0.10.2，以及本项目的 SSH stdio 接入。参考宿主 2 vCPU / 4 GB RAM；当前样本的 API 进程组约占 1.3 GB。这个数字不能代表大规模资料和高并发的需求。

## 中央服务

安装 PostgreSQL/pgvector，并为应用建立独立数据库与非超级用户。安装 Python 环境：

```sh
python3 -m venv /opt/personal-memory/venv
/opt/personal-memory/venv/bin/pip install 'hindsight-api-slim[local-onnx]==0.10.2' 'fastmcp==4.0.10'
/opt/personal-memory/venv/bin/pip install /PATH/TO/personal_memory_bridge-0.1.0-py3-none-any.whl
```

把 [运行配置模板](../examples/runtime.example.json) 复制到仓库外，填写数据库、固定模型、真实报价和密钥文件路径。三个价格占位符必须全部替换为人民币/百万 token；模板不是供应商报价。密钥单独保存在应用用户可读的 0600 文件中。模型权重准备完毕后启用 offline 配置；维度和模型版本必须与已有向量一致。

```sh
python -m personal_memory.runtime --config /PRIVATE/PATH/runtime.json
python -m personal_memory.wait_ready --url http://127.0.0.1:8889 --timeout 120
personal-memory --archive /PRIVATE/PATH/archive.db worker --url http://127.0.0.1:8889 --bank my-personal-bank --scope personal --interval 1800 --limit 1
```

`runtime` 把后端全部 LLM 请求转到同一费用闸门。重复启动不重置账本；已验证连接跳过付费启动探测。worker 默认异步提交固定 operation ID，遇到失败即退出，不自动重排失败操作。systemd 应先等待 health 就绪再启动 worker，避免重启机器时因模型尚未加载而永久停止。API 可配置失败重启；提炼 worker 使用 `Restart=no`。

一次阶段扩充使用 `Ledger.extend_attempt_limit(path, new_limit, reason)`，保留原预算和每次费用。它不增加人民币上限。对遗留 `reserved` 项，只有确认原进程和后台任务已停止后，才按未知费用结算；不能释放这笔预留当作未发生。

## 来源端与增量传输

来源端只需要本项目标准库模块，读取原日志/数据库，不修改原资料。每个来源端配置自己的 `state` 文件、固定启用时间和来源白名单：

```json
{
  "state": "/PRIVATE/PATH/source-feed.db",
  "sources": [
    {"kind": "hermes", "name": "my-hermes", "path": "/PRIVATE/PATH/state.db", "scope": "personal", "since": "2026-10-04T00:00:00+00:00", "sessions": [], "complete_only": true}
  ]
}
```

只有设置了带时区的 `since`，才允许 Hermes 自动发现新会话；未设置时必须明确列出 `sessions`。Codex 同样可设置 `since` 和 `complete_only`。Markdown 的 `since` 根据文件修改时间筛选，仍须 `include` 白名单。没有变化的 Codex 文件及 Hermes 主库/WAL 可复用扫描记录；变化的大文件仍整体解析。变更日志保存脱敏来源，是私有资料，不能随代码公开。

使用 SSH 公钥连接，固定 known_hosts，给采集密钥设置 `restrict,command="/ABSOLUTE/EXPORT_LAUNCHER"`。该启动器只运行：

```sh
python -m personal_memory.sync export --config /PRIVATE/PATH/source-config.json
```

中央 `pull` 配置形如：

```json
{
  "archive": "/PRIVATE/PATH/archive.db",
  "scope": "personal",
  "allowed_sources": ["my-hermes"],
  "checkpoint": "/PRIVATE/PATH/source-cursor.json",
  "command": ["ssh", "-T", "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes", "-o", "StrictHostKeyChecking=yes", "-i", "/PRIVATE/KEY/source-reader", "source-user@SOURCE_HOST"]
}
```

```sh
python -m personal_memory.sync pull --config /PRIVATE/PATH/pull.json
```

接收端成功写入才推进游标。一次默认传输最多 20 个变更，断线后可以重放；内容哈希跳过已收到的版本。来源变更日志身份发生变化时停止，不能拿旧游标跳过新库。后台每五分钟轮询即可，不需要定期唤醒语言模型来搬运数据。

Windows Codex 使用主动 `push`：来源配置再加入 `checkpoint` 和 `command`，SSH 写入密钥的强制命令固定为中央 `sync ingest --config ...`。接收端的 `allowed_sources` 和 `scope` 限制该密钥的写入范围。持续运行：

```sh
python -m personal_memory.sync_watch --config /PRIVATE/PATH/push.json --status /PRIVATE/PATH/status.json --interval 300
```

在 Windows 任务计划中使用 `pythonw.exe`，当前用户登录后运行。进程生成的传输子进程使用隐藏窗口；日志只写采集状态，不输出原文。

## 两个客户端共享检索

检索使用另一把密钥，强制命令固定运行本项目 `mcp`，只开放 `memory_search` 和 `memory_source`。API HTTP 只监听 loopback。Codex 示例：

```toml
[mcp_servers.personal_memory]
command = "/ABSOLUTE/PATH/TO/ssh"
args = ["-T", "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes", "-o", "StrictHostKeyChecking=yes", "-i", "/PRIVATE/KEY/memory-read", "memory-user@MEMORY_HOST"]
enabled_tools = ["memory_search", "memory_source"]
startup_timeout_sec = 30
tool_timeout_sec = 60
```

配置方式依据 [OpenAI 官方 MCP 文档](https://learn.chatgpt.com/docs/extend/mcp?surface=cli)。新配置需要被客户端重新加载；现有对话是否立即识别工具应实际检查。Windows 已通过普通匿名管道完成 MCP JSON-RPC 查询与回源；另用本机安装的 Codex 二进制启动独立 stdio app-server，通过 `mcpServerStatus/list` 连接并发现两个工具，没有创建聊天或启动模型请求。该检查验证配置和连接，真实聊天模型的主动选择仍待观察。另一个 FastMCP Python 异步客户端返回 `Connection closed`，其原因尚未定位，不能据此推断原生 Codex 连接失败。

Hermes 配置：

```yaml
mcp_servers:
  personal_memory:
    command: /usr/bin/ssh
    args: ["-T", "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes", "-o", "StrictHostKeyChecking=yes", "-i", "/PRIVATE/KEY/memory-read", "memory-user@MEMORY_HOST"]
    connect_timeout: 30
    timeout: 60
    tools:
      include: [memory_search, memory_source]
      resources: false
      prompts: false
```

Hermes 实测能把工具注册为 `mcp__personal_memory__memory_search` 和 `mcp__personal_memory__memory_source`。如果已有 `toolsets: [hermes-cli]` 等显式名单，保留原名单并加入 `mcp-personal_memory`；仅注册 MCP 不代表它已经进入模型的工具集合。部署验证曾发现此差异，补充后在实际工具构建器中确认两工具可见。载入配置后验证一次读取；不要重复启用原生 Hindsight 自动写入，绕过本项目纠错记录或重复提炼同一内容。

## 备份与恢复

```sh
python -m personal_memory.backup --config /PRIVATE/PATH/runtime.json --archive /PRIVATE/PATH/archive.db --directory /PRIVATE/BACKUPS
```

备份包含 PostgreSQL dump、来源 SQLite、费用 SQLite 及 SHA-256 清单。调用前暂停新的提炼并等待已有异步任务和费用预留结算，避免来源同步状态先于 PostgreSQL 快照。备份副本与配置分别保存在私有位置；采集 feed 和游标也要保留，缺失时按显式重放流程处理。

恢复先使用隔离数据库与禁用 LLM 的后端，比较表行数、内容指纹、来源版本和纠错记录，再测试检索；不要直接重跑全部提炼。
