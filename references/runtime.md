# 运行环境、连接方式与联合招生官窗口

安装、工具缺失或切换宿主时阅读本页；日常咨询只向学生说明结果和实际缺项。

## 两种调用路径

1. **宿主 MCP**：首次安装优先配置并验证；每次使用先检查当前工具列表，复用已连接的服务，不重复安装同名连接。
2. **包内 Python 直连**：原生 MCP 无法安装、未加载，或本次只读调用发生连接 / 协议故障时，使用 `scripts/mcp_client.py`。先复用已有 Python 3.9+，缺少运行环境才按安装指南处理。它直接实现 MCP 的初始化、工具发现和调用，不依赖先在宿主登记连接，也不需要安装 MCP SDK。

两条路径访问同一远程服务，并非把服务数据库搬到本机。详情与错误处理见 [客户端说明](mcp-client.md)。

| 服务 | 地址 | 授权 |
| --- | --- | --- |
| 查询 | `https://hi.xwoork.cn/info/mcp` | 按当前匿名查询规则 |
| 申请助手 | `https://hi.xwoork.cn/assist/mcp` | 学生工具无需 Key（按 IP 限流、建票需 PoW）；`submit_to_hri` 仍需运维发的 Key |

学生不需要配置任何 Key；运维或合作方的 Key 才通过 `LIUXUE_API_KEY` 或指定密钥文件提供，原生连接凭据不会自动交给 Python。旧服务若仍返回需要 Key，记为“申请助手待开放”，照常打开公共咨询页，不向学生索要 Key。不能扫描其他应用的凭据，不向聊天、报告、URL 或公共仓库写密钥。查询可用与助手授权完成是两项独立结果。

只读连接故障最多切换一次直连，保持原业务参数；失败不循环。`401` / `403` 停止同类重试；`429`、配额与过宽查询按返回说明收窄或稍后处理，不属于连接降级。正常 PoW 先处理挑战；超时不自动重发有副作用的操作。不可通过换客户端规避限制。

## 原生 MCP 的依赖声明

- `agents/openai.yaml` 为支持该元数据的 OpenAI 宿主声明依赖；实际安装和授权由宿主控制。
- Codex CLI 配置方式见 [INSTALL.md](../INSTALL.md)。
- `mcp.example.json` 是通用示例，字段与变量展开须按目标应用确认。
- WorkBuddy **专家 / 插件包**可在 `.codebuddy-plugin/plugin.json` 的 `dependencies.mcpServers` 指向 `.mcp.json`；本仓库提供 `references/workbuddy-mcp.example.json` 接入示例。普通 Skill 文件本身不等于官方专家，也不能保证弹出授权卡片。

参考：[OpenAI Skill 依赖](https://developers.openai.com/plugins/build/skills)、[Codex MCP](https://developers.openai.com/codex/mcp/)、[WorkBuddy 专家依赖](https://open.workbuddy.cn/docs/expert)。

## 启动与工具选择

1. 每次触发从原安装目录运行 `python3 <安装目录>/scripts/check_update.py --background`（路径有空格加引号；Windows 用已确认的 Python 3 命令）。它立即返回 `runtime_path`、`runtime_skill_path`、`effective_version`；版本与已加载说明不同才读运行版 `SKILL.md`，且不再递归执行启动步骤。没有执行能力就用当前说明。
2. 首次安装按 [INSTALL.md](../INSTALL.md) 优先配置原生 MCP；之后每次先用当前已连接的查询 / 申请助手，不因名称相似就假定存在。
3. 原生 MCP 未加载或只读请求遇到连接 / 协议故障时，用 `scripts/mcp_client.py`，参数与查询范围不变，先发现 schema 再构造参数。
4. 两条路径都不可用时给明确“未查库”的一般方法，列出待核项；不为一次咨询反复要求安装。

原生工具遇到 `pow_required` / `pow_invalid`：用根目录 `pow_client.py` 求解服务返回的挑战，同一业务参数带 `pow_session / pow_challenge_id / pow_nonce` 最多重试一次；成功响应的下一挑战可用 `PowPipeline` 预计算。Python 客户端已封装此流程。

## 打开联合招生官

学生识别码：`python3 <skill>/scripts/student_code.py link`（上传页加 `--upload`）返回本次要打开的地址。第一次会向平台申请 8 位 ID 和 6 位授权码，存到本机私有文件 `~/.liuxue/id`（Windows 在 `%APPDATA%\liuxue\id`），之后复用；地址形如 `https://hi.xwoork.cn/s/<ID>#<授权码>`，授权码在 `#` 后不进服务器日志，页面读取后会从地址栏去掉。平台未开放、限流或离线时返回普通 `/student`，并在本机记下等待时间，不反复请求。`student_code.py ensure` 只返回 ID（不含授权码），供 `start_intake(student_code=…)` 关联交接票。授权码不写进普通回复；没有打开能力需要给学生链接时，可以给完整地址。

触发时机、一句话结尾和拒绝规则以 `SKILL.md`「联合招生官」为准；地址、宿主差异和材料交接见 [联合招生官说明](joint-officer.md)。Skill 能说明何时打开页面，永久侧栏按钮仍取决于宿主界面；Python 客户端不会自行增加宿主按钮。

## 自动更新

普通 GitHub 安装默认用 `scripts/install_latest.py` 从公开更新目录比较 stable / pilot 并选择最新可用版本；只有用户明确要求时使用 `--stable-only`。已收到且明确指定的完整交付包按安装指南的指定包流程处理。首次选择安装版本与下述已安装版本的后台跟随通道是两种操作；安装或更新后都记录实际 `effective_version` 与 `runtime_path`。

每次触发从原安装目录执行 `python3 <安装目录>/scripts/check_update.py --background`，读取返回的 `runtime_path`、`runtime_skill_path`、`effective_version`。读取该运行版说明后跳过已完成的启动步骤，本次咨询一直使用同一运行目录，不能中途再跑启动器重选版本。宿主没有脚本执行能力时直接用已加载的说明，不因更新阻断咨询。此触发由 Agent 遵守 Skill 指令执行，不是所有宿主都有的强制事件钩子。

后台读取官方仓库的 `updates.json`（固定 GitHub raw 地址），下载固定提交对应的包。只接受更高版本；稳定安装只跟随 stable，试用安装可选择更高的 pilot 或 stable。正常 6 小时缓存；网络失败从 5 分钟退避至最多 1 小时，403/429 按服务器要求等待，`--check` 也不绕过等待。缓存支持 ETag，避免重复传输。不读用户 GitHub Token，不修改 MCP 密钥或配置。

通过来源、清单、逐文件大小与 SHA-256、兼容能力、已有文件和知识卡保护后，新版保存到安装目录之外的独立版本目录，最后原子切换本机指针。后台不覆盖正在使用的文件、不执行下载包中的安装器或测试，也不重启应用。旧运行版一直保留，下载/校验/切换失败继续原版，不按时间猜测当前咨询已结束。检查能力声明不等于证明所有业务行为；发布者仍须完成原功能回归。

本地编辑、额外文件、知识条目变更或丢失、功能声明缺失会阻止自动切换，状态中给出原因；不要为了更新删除这些保护。知识卡目前允许保留全部原条目后追加新卡，已有卡内容修订需维护者明确迁移并重新验收，不能悄悄覆盖用户试用内容。安装目录根的 `VERSION` 是入口版本，实际本次使用版本以 `effective_version` 为准；运行目录和原目录分别保留。旧会话若仍缓存以前的启动说明，开新会话载入新入口，不强制重启应用。

`--status` 不联网；`--check` 由维护者显式执行一次前台更新；正常咨询只用 `--background`。所有更新状态以 JSON 返回，不在学生回答中堆叠技术信息或每轮提醒。更新不采集学生资料、不进入咨询、不请求音频权限。
