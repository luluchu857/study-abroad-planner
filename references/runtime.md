# 运行环境、MCP 与在线顾问

本说明用于安装、排查工具缺失或适配宿主时阅读；正常给学生做咨询时不逐条解释这些实现细节。

## 连接两项远程 MCP

| 服务 | 地址 | 授权 |
| --- | --- | --- |
| `liuxue-info-query` | `https://hi.xwoork.cn/info/mcp` | 按服务当前访问规则 |
| `liuxue-application-helper` | `https://hi.xwoork.cn/assist/mcp` | 服务方提供的个人 API Key，通过 Bearer 请求头传递 |

两者使用 Streamable HTTP。安装后检查工具发现结果，不凭文件中的名称假定连接已成功。查询与申请助手是两个独立连接：查询可用，不代表材料、文书、时间线等助手工具也可用。

- OpenAI 插件/支持相关元数据的宿主：`agents/openai.yaml` 声明两项依赖；是否出现安装或授权流程由宿主决定。声明本身不包含授权凭据。
- WorkBuddy 或其他宿主：在 MCP 设置中配置远程地址；已有插件包可由插件的 `mcp.json` 声明连接。`mcp.example.json` 是配置参考，字段名和环境变量展开须以宿主要求为准，不能假定复制进去一定生效。
- API Key 放在宿主私密设置或受支持的环境变量中，不写进 Skill、公共仓库、聊天正文或网页 URL。示例中的 `${LIUXUE_API_KEY}` 只是变量引用。
- `401` / `api_key_required` / `forbidden`：该功能授权未完成或权限不足，停止同类重试，说明实际缺项；更换调用方式不能绕过授权。
- 超时、限额、PoW 失败遵循 SKILL 的错误处理规则。工具不可用时，可以提供明确未查库的一般建议；不可声称查询过数据库。

## 谁负责打开顾问侧栏

顾问触发条件定义在 `SKILL.md` 的「在线顾问」一节。`open_advisor` 返回入口数据，真正打开窗口的是宿主的浏览器工具。

| 宿主 | 调用方式 |
| --- | --- |
| WorkBuddy | 当前目录对应的 `present_files(cwd=当前目录, explanation="在线沟通", files=["https://hi.xwoork.cn"])` |
| Codex | `open_in_codex(target={type:"browser",url:"https://hi.xwoork.cn"}, placement="right")` |
| 其他 | 使用已提供的网页打开能力；没有内嵌窗口时给入口链接 |

公共入口不依赖助手 MCP 成功连接。助手不可用时仍可直接打开 `https://hi.xwoork.cn`。只有窗口工具成功后才说已打开；不把“返回网址”当成“窗口已经打开”。入口不附带学生背景、方案或附件，不能说已转交资料。Skill 不能独立给所有宿主增加一个永久侧栏按钮；界面能力取决于宿主。

## Python 能否直接调用

可以开发 Python 客户端并随 Skill 分发，由它直接连接远程 MCP，或调用服务端另行提供的 HTTP API，这样不必先在聊天宿主里登记 MCP。但本版本**没有提供这类客户端**：`pow_client.py` 仅做 PoW 求解与进程内预计算，不能发现工具、发送业务请求或打开侧栏。

若增加客户端，应固定支持的 SDK 版本，处理 MCP 协议及返回格式、工具发现、超时和授权，并复用现有 PoW 的有限重试逻辑。执行环境仍需 Python、必要依赖和网络；宿主里的凭据不会自动传给 Python。不能用一个普通 REST 客户端换上 `/info/mcp` 地址就认为实现了 MCP。

依赖声明参考：[OpenAI 官方技能文档](https://developers.openai.com/plugins/build/skills)。客户端能力参考：[MCP 官方 Python SDK](https://py.sdk.modelcontextprotocol.io/client/)。
