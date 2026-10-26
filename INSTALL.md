# 给 AI 的安装与验收指南

目标：把这份 Skill 装到当前宿主，验证实际可用的功能，并向用户报告结果。不能把“文件已复制”“MCP 已登记”“已授权”“业务调用成功”混为一谈。

默认安装用户指定版本；没有指定时选择本仓库的最新稳定 Release。不要从第三方镜像下载，不要执行网上搜索结果中的未知安装命令。

## 1. 确认环境与取包

- 官方仓库：https://github.com/luluchu857/study-abroad-planner
- 稳定版本：https://github.com/luluchu857/study-abroad-planner/releases/latest
- Skill 文本与原生 MCP 本身**不要求先安装 Python**。先配置原生 MCP；环境已有 Python 可直接复用，只有需要包内安装器、直连、报告导出或版本检测时才使用 Python **3.9+**。这些核心脚本均不需要额外 Python 依赖。
- 识别当前是 Codex、WorkBuddy 还是其他宿主；复用已有授权，不为安装无关插件或开启额外权限。
- 下载 Release 附件的 Skill ZIP，解压到新的临时目录；也可读取选定版本的 Git 仓库。示例安装当前版本：

```sh
git clone --branch v0.8.0 --depth 1 https://github.com/luluchu857/study-abroad-planner.git study-abroad-planner-v0.8.0
cd study-abroad-planner-v0.8.0
python3 --version
```

Windows 若使用 `py -3`，后续 `python3` 替换为同一 Python 解释器。优先检查 Release 上的 ZIP SHA-256 与下载文件一致。包内 `PACKAGE-MANIFEST.json` 列出文件大小与 SHA-256，安装器会逐项验证；清单证明内容一致，不代替对下载来源的核对。

先阅读 `SKILL.md`、本指南和要执行的脚本。目录应有 `VERSION`、`PACKAGE-MANIFEST.json`、`scripts/`、`references/`、`templates/`、`tests/`。

## 2. 安装 Skill 文件

先按第 3 节确认原生 MCP 的连接方案。环境已有 Python 时，以下命令在**取到的包根目录**运行，选择一条适用的路径。之后引用脚本时使用实际安装目录，不假设咨询工作目录就是 Skill。

**没有 Python、原生 MCP 可用时**：优先用宿主支持的 Skill ZIP 导入，或用本地文件操作将解压后的 `study-abroad-planner` 文件夹装到下列宿主目录。替换前把旧文件夹完整移到活动 `skills` 目录之外，按清单核对来源文件与目标文件的大小及 SHA-256；校验能力不足则不要声称校验通过。无需仅为查询再装 Python。之后需要直连或文件导出时，再安装运行环境；未具备脚本环境的验收项明确列为未执行。

Codex：

```sh
python3 scripts/install.py --host codex --dry-run
python3 scripts/install.py --host codex
```

默认目标是 `$CODEX_HOME/skills/study-abroad-planner`，未设置时为 `~/.codex/skills/study-abroad-planner`。

WorkBuddy：

```sh
python3 scripts/install.py --host workbuddy --dry-run
python3 scripts/install.py --host workbuddy
```

默认目标为 `~/.workbuddy/skills/study-abroad-planner`。这是本人的 Skill 包，不覆盖 WorkBuddy 官方“留学顾问｜留洋洋”专家，也不改动其他名称的 Skill。

其他宿主 / 指定目录：

```sh
python3 scripts/install.py --host custom --destination "<宿主要求的最终Skill目录>" --dry-run
python3 scripts/install.py --host custom --destination "<宿主要求的最终Skill目录>"
```

`--destination` 指**最终 Skill 目录**，不是其父目录。Codex / WorkBuddy 覆盖路径时最后一级仍须为 `study-abroad-planner`。

安装器只复制清单内容与清单本身。相同内容不重复改动；更新时先验证暂存包，将旧目录完整保留到独立备份目录，再替换目标。Codex / WorkBuddy 的默认备份位于各自的 `skill-backups` 下，不放在活动 `skills` 目录中，避免发现多个旧 Skill。记录输出中的目标、版本和备份路径。失败停止，不手工删除旧包。不会修改个人 MCP 配置、密钥或其他 Skill。

按宿主的正常刷新机制重新载入；若当前会话仍缓存旧说明，开一个新会话验证，不强制重启用户的应用。

## 3. 接通查询：先原生 MCP，缺失时 Python 直连

**安装顺序：先原生 MCP，再 Python 直连。** 先查看宿主当前工具。查询服务和申请助手分别确认，不重复建立同名连接。用户要求安装这份 Skill 时，应完成宿主支持的原生连接配置和验证；不要因为包内已有 Python 就直接跳过原生 MCP。宿主不支持、安装失败或当前环境无法登记时，记录原因，再启用直连路径。

| 服务 | 地址 | 授权 |
| --- | --- | --- |
| 留学信息查询 | `https://hi.xwoork.cn/info/mcp` | 匿名规则下可查询 |
| 留学申请助手 | `https://hi.xwoork.cn/assist/mcp` | 个人 API Key |

### Codex 原生连接

若当前已经可用，直接跳过登记。需要登记时，按现有配置只增缺少的项，不覆盖其他连接：

```sh
codex mcp add liuxue-info-query --url https://hi.xwoork.cn/info/mcp
codex mcp add liuxue-application-helper --url https://hi.xwoork.cn/assist/mcp --bearer-token-env-var LIUXUE_API_KEY
codex mcp list
```

第二条只保存环境变量**名称**，不提供真实密钥。运行 Codex 的环境须取得用户自己的值；不要在命令示例、聊天或仓库中填入密钥。当前服务采用 API Key，不能把 `mcp login` 的 OAuth 流程当作领 Key 方法。

`agents/openai.yaml` 也声明两项依赖，支持相关元数据的宿主可据此引导连接；不能保证所有应用只读一个 Skill 文件就自动安装完成。[Codex 官方说明](https://developers.openai.com/codex/mcp/)

### WorkBuddy 原生连接

在当前客户端的连接器 / MCP 设置中登记远程地址；已有连接先测试，不重复创建。通用参考为 `mcp.example.json`，需按当前客户端字段与变量规则调整。

若发布为 WorkBuddy **专家 / 插件**，官方支持 `.codebuddy-plugin/plugin.json` 中 `dependencies.mcpServers` 指向 `.mcp.json`。可使用 `references/workbuddy-mcp.example.json` 作为该文件的内容，申请助手带私密 Token 输入表单。它是接入示例，本仓库不是已上架的 WorkBuddy 官方专家；普通 Skill 不保证出现该引导。[WorkBuddy 官方说明](https://open.workbuddy.cn/docs/expert)

### Python 直连

原生 MCP 无法安装或当前不可用时启用此路径。先检查现有 Python 3.9+，满足要求就直接使用，不需安装 SDK，也不另装一套 Python。若缺运行环境，使用宿主允许的官方 Python 安装渠道或可信系统包管理器完成安装，保留既有环境；不能安装时说明缺项并继续一般咨询，不假装已经连接。

```sh
python3 scripts/mcp_client.py list-tools --server query
python3 scripts/mcp_client.py doctor --server query --probe
```

脚本初始化 MCP、发现工具、按 schema 发送请求，并完成必要的 PoW。它不是普通 REST URL 替换器，也不是离线数据库。

助手使用用户主动设置的 `LIUXUE_API_KEY`，或命令的 `--key-file "<私密文件>"`。不要索要密钥粘贴到对话，不扫描其他应用存储的凭据。未提供个人授权时记录“申请助手待授权”，查询和本地导出仍可完成。

后续每次使用仍优先原生 MCP。只读请求出现宿主连接 / 协议故障，可按相同参数切换直连一次；两条都失败就停止重试。401 / 403、额度、限流、过宽查询和正常 PoW 挑战不属于该故障降级，不通过换客户端规避。提交请求超时不能确定是否已执行，不自动切换后重发。

详细调用和错误含义见 [Python 客户端说明](references/mcp-client.md)。

## 4. 安装验收

### A. 离线检查

```sh
python3 -m unittest discover -s tests -p 'test_*.py' -v
python3 scripts/validate_plan.py templates/plan.example.json
python3 scripts/build_plan.py templates/plan.example.json --output "<新的临时目录>/demo"
```

必须检查生成的 `report.html`、`report.md`、`report.csv` 与 `plan.json` 确实存在。示例全部为合成数据，不能将示例院校 / 费用当作查询结果。允许浏览器时验证网页关键词和状态筛选；预览器禁用脚本时说明筛选不可用，内容仍应可读。

如果用户需要 Excel：在单独虚拟环境安装 `openpyxl` 后加 `--xlsx`。不要仅为普通安装修改全局 Python。依赖未安装时保留 CSV，报告没有生成 XLSX，不称全部格式成功。

### B. 一次只读在线检查

```sh
python3 scripts/mcp_client.py doctor --server query --probe
```

此命令做工具发现、合成绩点换算和限制为 1 条的专业查询，足够确认协议与 PoW；不要重复枚举数据库或用真实学生资料测试。若第 3 步已经执行成功，复用结果，不必再次请求。

原生 MCP 还需要在当前宿主发现工具并执行一次合成只读查询，才能报告“原生连接可用”。Python 测试成功仅证明直连路径成功。

已有用户提供的助手授权时，可做 `doctor --server assist` 的发现检查；没有授权不做反复尝试，不调用提交接口“测试写入”。网络失败、401、429 记录实际结果，不绕过限制。

### C. 更新检测

```sh
python3 scripts/check_update.py --status
python3 scripts/check_update.py --background
```

后台模式应立即返回缓存或启动状态，不等待联网。默认缓存 6 小时，离线不影响业务；它只检查和提示，不自动下载执行。需要立即检查时用 `--check`。

### D. 验收回报

用用户能理解的语言报告：

- 已安装版本、应用与目录；旧包备份位置（如有）。
- 查询：原生 MCP / Python 哪条路径实际成功。
- 申请助手：已授权并通过发现，或仍待个人授权；不能用查询成功代替。
- 本地报告：实际生成的格式及示例入口。
- 是否发现新版本、任何真实失败或尚未验证项。

不要仅回复“全部搞定”而略过授权、加载或联网失败。

### E. 顾问与语音边界

安装测试不自动开麦、拨打或进入真实咨询。学生明确要找顾问时，才按 Skill 的触发规则打开页面。语音优先由当前网页原生能力处理；旧工程的通话助手只是采音测试工具，本包不将它当成正式备用通道安装。具体见 [通话说明](references/voice-calling.md)。

## 5. 更新与回退

检测到新版且用户要求更新时，从同一官方仓库取指定稳定 Release，重新执行校验、安装和验收。别在正在处理的咨询中直接覆盖已加载代码。

回退使用安装器给出的旧目录备份：先保留当前目录，再将对应备份恢复到原目标，最后重新载入并验证。不要回退服务端数据、数据库、个人密钥或无关应用配置。需要让旧备份通过新安装器重装时，只有带有效清单的包才适用；早期无清单版本直接恢复完整备份。

## 维护者发布检查

更新 `VERSION`、Skill 元数据与发布文案，重新生成 `PACKAGE-MANIFEST.json` 的大小和 SHA-256（清单不列自身）。仅收录公开 Skill 文件，不含 Git 元数据、Python 缓存、密钥、真实学生数据或本地报告。跑完离线测试与一次必要在线探测后打版本标签，上传 ZIP 并校验实际下载内容。
