# Python 直连 MCP

`scripts/mcp_client.py` 用 Python 标准库直接访问两个远程 MCP。它不依赖宿主预先安装 MCP，也不代替宿主的浏览器或侧边栏打开能力。Python 3.9+，无需安装第三方包；本版在 Python 3.9.6 上验证。

## 使用

在解压后的 Skill 根目录运行：

```sh
python3 scripts/mcp_client.py list-tools --server query
python3 scripts/mcp_client.py call --server query --tool convert_gpa --args-json '{"score":85,"from_scale":"percent","to_scale":"4.0"}'
python3 scripts/mcp_client.py call --server query --tool search_programs --args-json '{"country":"SG","keyword":"computer","limit":3}'
python3 scripts/mcp_client.py doctor --server query
python3 scripts/mcp_client.py doctor --server query --probe
```

Windows 可将 `python3` 换成已安装的 `python`。`--args-file 文件名.json` 与 `--args-json` 二选一；中文参数文件使用 UTF-8，内容须为 JSON 对象，最多 48 KiB。文件方式可减少命令历史中留下背景信息，但仍应保护和及时清理本地参数文件。

- `query` 固定访问 `https://hi.xwoork.cn/info/mcp`，保持匿名，不读取或发送助手密钥。
- `assist` 固定访问 `https://hi.xwoork.cn/assist/mcp`，需要运行环境中的 `LIUXUE_API_KEY`，或显式传入 `--key-file 私密文件`。不支持命令行直接写出密钥。POSIX 下文件必须属于本人、是普通文件且无组/其他用户权限（建议 `600`），拒绝符号链接；Windows 下应自行使用私密用户目录及文件权限。
- 输出为 JSON；成功退出码 `0`，服务错误、参数错误或调用失败为非零。`ok` 表示本次协议/工具调用成功，不代表录取、材料登记或学校递交成功。
- `doctor` 默认只初始化、发现工具并确认原有核心只读工具齐全；缺少核心工具就报告失败。`features.knowledge` 仅在目录同时包含 `search_knowledge`、`get_knowledge` 时为真；旧服务缺少知识扩展不影响核心查询诊断通过。`--probe` 仅用于查询服务，另做合成分数 `85` 的绩点换算及 `limit=1` 的专业查询，校验换算方法/数值、结果条数与项目字段，空对象或工具错误不算通过。专业查询可正常返回零条结果；不查询学生资料、不调用递交工具。它不验证顾问网页交互；匿名查询通过不代表助手已授权。

助手示例（先通过自己的安全方式提供个人密钥）：

```sh
python3 scripts/mcp_client.py list-tools --server assist --key-file /path/to/private-key
python3 scripts/mcp_client.py call --server assist --key-file /path/to/private-key --tool open_advisor --args-json '{}'
```

`open_advisor` 仅取得入口。按主 Skill 的顾问触发规则完成解答和相应免费评估／在线免费申请引导后，由宿主打开或恢复显示咨询页；已有页面且可见时直接复用，不重复创建。没有助手授权也可打开固定公共入口，取得网址或返回 queued / pending 均不代表页面已经显示。不会自动发送学生背景、方案或材料。

这里的私密文件路径只是示例；不要把真实密钥、学生材料或背景参数放进公共仓库。客户端拒绝所有 HTTP 重定向，避免将 Authorization、会话或业务参数转发到其他地址；错误信息不打印 HTTP 响应正文或密钥，正常结果也会遮蔽当前密钥的原文回显。

## 递交入口的门槛

`submit_to_hri` 仅允许 `assist`。即使只是获取摘要预览，也须显式 `--allow-write` 且参数包含 `confirmed:false`。确认阶段须另加 `--confirm-write`，参数包含 `confirmed:true`、原 `summary` 对象和服务返回的 `confirm_code`；只有学生明确确认后才可使用。

```sh
python3 scripts/mcp_client.py call --server assist --tool submit_to_hri --args-file preview.json --allow-write
```

CLI 不替用户确认，也不把返回链接说成已递交。所有该工具响应带有 `submission_status: "not_verified_by_client"`。当前服务返回的是预览/申请工作台入口，应按服务实际响应继续，不宣称学校已收到。确认调用发生超时或连接中断时会标记 `outcome_unknown:true`，不自动重发；应先核实结果。

## 协议与边界

按官方 [Streamable HTTP 传输](https://modelcontextprotocol.io/specification/2025-06-18/basic/transports)、[初始化生命周期](https://modelcontextprotocol.io/specification/2025-06-18/basic/lifecycle) 和 [工具协议](https://modelcontextprotocol.io/specification/2025-06-18/server/tools) 实现最小客户端：

1. 发送 `initialize`，优先协商 `2025-06-18`，也接受服务返回的 `2025-03-26`；随后发送 `notifications/initialized`。后续请求附协议版本及服务提供的会话 ID。
2. 通过 POST 调用 `tools/list` / `tools/call`，同时接受 JSON 与 SSE 响应。SSE 按事件解析，允许结果前出现通知，并校验对应请求 ID。
3. 优先取 `structuredContent`，兼容 `content` 中文本形式的 JSON。保留工具错误，不将错误内容当成功。

本客户端面向当前 `stateless_http=True, json_response=True` 双 MCP；它不是通用完整 MCP SDK。不实现 GET 长连接、断线恢复、服务端主动请求、OAuth 或多进程持久会话。工具名称使用本版已核实的服务名单，目录变更需更新客户端。

默认单命令最多 8 次 HTTP 请求（含初始化与通知），目录最多 4 页；响应最多 2 MiB，JSON 最多嵌套 64 层。单次网络等待默认 10 秒，`--timeout` 可设为大于 0 且不超过 30 秒。接收过程中也检查已用时间；阻塞读取受 socket 超时限制。HTTP 401、429、连接错误、超时均不自动重试；429 的数字 `Retry-After` 可返回给调用者，但客户端不循环等待。

复用根目录 `pow_client.py`：`pow_required` / `pow_invalid` 可触发一次求解、一次原业务请求重试；第二次仍失败就返回错误。只接受 `sha256-leading-zero-bits`、难度 1–24、当前未过期且剩余不超过 300 秒的数字 Unix 秒时间戳，每次求解上限 500 万个 nonce。不接受调用者自填 `pow_` 参数。成功结果的下一轮挑战会后台预计算；若该挑战无效，保留已成功的业务结果并返回 `warnings`，不计算。

预计算只在同一个 `MCPClient` 对象/进程内复用。每次独立 CLI 命令都会建立新客户端，没有把 proof 或密钥写到磁盘，也没有常驻后台。`doctor --probe` 在同一客户端内连续验证；应用集成时也可复用对象。

## 离线测试

```sh
python3 -m unittest discover -s tests -p 'test_mcp_client.py' -v
```

测试使用合成 HTTP/JSON/SSE、合成密钥和小难度挑战，并阻止真实网络请求；覆盖握手、结果解析、分页/限额、401/429/超时、一次重试、预计算、私密文件和递交门槛。
