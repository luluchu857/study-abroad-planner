# 给 AI 的安装与验收指南

目标：把这份 Skill 装到当前宿主，验证实际可用的功能，并向用户报告结果。不能把“文件已复制”“MCP 已登记”“已授权”“业务调用成功”混为一谈。

**用户只给 `github:luluchu857/study-abroad-planner`、没有指定版本时，安装本仓库当前公开的最新可用版本，不把版本号写死。** 从 `updates.json` 的 stable / pilot 条目按语义版本比较选择最高版本；当前公开试用版也属于可选发布，安装结果必须如实标明试用状态。只有用户明确说“只要稳定版”时才只看 stable。用户明确指定版本则遵照该版本，但已有更高版本时不自动降级。

不要把搜索引擎缓存、旧 README 截图、某个历史 Git 标签或 GitHub `/releases/latest` 当作默认安装依据；后者只代表 GitHub Release，可能落后于本仓库公开更新目录中的稳定版或试用版。用户仅要求稳定版时仍由 `install_latest.py --stable-only` 读取当前 stable 条目；只有明确指定某个历史 Release 时才使用历史 Release 页面。更新目录读取失败就报告无法确认最新，不自行把旧包称为最新版。

**本包是 0.10.1，包含 181 条已审知识，对应已审阅的 263 篇会议。** 通过仓库安装入口按公开更新目录选择当前版本；GitHub Release 发布状态以实际页面为准。安装后先发现同一查询服务的工具目录，实际检索并读取一条知识；远程暂不可用时才使用包内审核快照。学校查询、知识查询与顾问窗口分别验收，不把工具登记当业务调用成功。安装过程不采音、不拨号。

## 1. 确认环境与取包

- 官方仓库：https://github.com/luluchu857/study-abroad-planner
- 公开更新目录：https://raw.githubusercontent.com/luluchu857/study-abroad-planner/main/updates.json
- 历史 Release：https://github.com/luluchu857/study-abroad-planner/releases（仅明确指定历史 Release 时使用；只要求稳定版仍读取更新目录的 stable 条目）
- Skill 文本与原生 MCP 本身**不要求先安装 Python**。先配置原生 MCP；环境已有 Python 可直接复用，只有需要包内安装器、直连、报告导出或版本检测时才使用 Python **3.9+**。这些核心脚本均不需要额外 Python 依赖。
- 识别当前是 Codex、WorkBuddy 还是其他宿主；复用已有授权，不为安装无关插件或开启额外权限。
- 获取默认分支的安装入口，再由 `install_latest.py` 动态取公开最新版；不要在默认安装命令中写历史标签或试用分支。源码工作目录只用于读取指南和运行安装器，不能把带 `.git` / `updates.json` 的工作目录整包复制进宿主 Skill 目录。

```sh
git clone --depth 1 https://github.com/luluchu857/study-abroad-planner.git study-abroad-planner-install
cd study-abroad-planner-install
python3 --version
```

如果目录已存在，使用新的临时目录，不能覆盖用户已有工作。没有 Git 时，可用当前宿主从官方默认分支获取入口文件及所需模块，或按更新目录的固定提交取得完整包；仍核对清单后安装，不用搜索缓存代替当前目录。下载来源固定为本仓库，避免第三方镜像。

Windows 若使用 `py -3`，后续 `python3` 替换为同一 Python 解释器。最新版安装器校验更新目录指定的固定提交、清单 SHA-256 和每个包文件；GitHub 源码 ZIP 压缩字节可能变化，不用历史 ZIP 哈希替代目录中的清单校验。用户指定历史 Release 附件时另核对该附件 SHA-256。包内 `PACKAGE-MANIFEST.json` 列出文件大小与 SHA-256，安装器会逐项验证；清单证明内容一致，不代替对下载来源的核对。

先阅读 `SKILL.md`、本指南和要执行的脚本。目录应有 `VERSION`、`PACKAGE-MANIFEST.json`、`scripts/`、`references/`、`templates/`、`tests/`。

## 2. 安装公开最新版

先按第 3 节确认原生 MCP 的连接方案，已有 Python 3.9+ 直接复用。以下命令在刚取得的官方安装入口目录执行，由脚本从当前更新目录选择版本：

Codex：

```sh
python3 scripts/install_latest.py --host codex
```

WorkBuddy：

```sh
python3 scripts/install_latest.py --host workbuddy
```

其他宿主或隔离安装验证：

```sh
python3 scripts/install_latest.py --host custom --destination "<宿主要求的最终Skill目录>"
```

`--destination` 是最终 Skill 目录，不是父目录。只有用户明确要求只用稳定版时加 `--stable-only`。脚本报告所选版本、通道、安装或当前生效目录及备份；更新目录暂不可用则停止本次安装，不冒称已经取得最新版。

**已有安装的处理：** 先核对当前真实生效版本与本地完整性。更高的已有版本保持；本地修改、额外文件、知识丢失或不兼容包拒绝覆盖。已有自动更新入口时只提交新的独立运行版，不替换入口，因此不会破坏原咨询或导致基线变更保护。旧的无自动更新版本只有在清单完整、无自定义差异、兼容且更高版本时才备份迁移。不能为消除错误而直接清空缓存、旧包或用户自定义文件。

默认 Codex 目录为 `$CODEX_HOME/skills/study-abroad-planner`（未配置时 `~/.codex/skills/study-abroad-planner`），WorkBuddy 为 `~/.workbuddy/skills/study-abroad-planner`。不覆盖 WorkBuddy 官方“留学顾问｜留洋洋”，不修改其他 Skill 或个人 MCP 密钥。新版运行目录可能在用户缓存中，安装根 VERSION 是入口版本，验收必须同时记录 actual / effective 版本。

没有 Python、原生 MCP 可用时，可由有文件/校验能力的 Agent 读取当前 updates.json，按同一版本选择规则下载固定提交并核对清单，只复制包清单列出的文件和清单本身。先保留旧包且完成防降级、知识和本地修改检查；能力不足就报告待完成，不强行覆盖。若宿主不支持脚本执行，正常查询仍可用，但不能声称后台自动升级已实际运行。

`install.py` 是安装已指定完整包的底层工具，只用于维护者明确版本的安装和迁移；普通“帮我安装”默认使用上面的 `install_latest.py`。成功后按宿主正常机制重新载入；旧会话缓存说明时开新会话，不强制重启用户应用。

### 指定完整包与知识迁移

收到并明确指定完整包时，先核对清单、版本和目标状态。全新空目录可用 `scripts/install.py` 的 `--dry-run` 预检，再按同一目标安装；已有安装必须先检查原入口与实际运行版，不直接覆盖入口或清空自动更新状态。

当前 0.10.0 的 181 条知识与本包逐条一致，完整性与能力检查通过时，可经现有更新入口选择更高的 0.10.1 运行版，原入口及旧运行目录保留。稳定入口的后台更新还要求对应包实际进入公开 stable 目录；本地版本号本身不表示已经发布。

旧 pilot.2—pilot.5 的 26 个知识 ID 均保留在 181 条知识中，但对象内容已审核修订，包含新增检索别名、来源信息与部分正文调整。现有更新保护会返回 `knowledge_would_change_or_be_removed`，这是正确拒绝。即使当前运行版另有更新，也会同时检查原入口知识；不能改 ID、删旧卡、放宽比较或直接覆盖入口来绕过。需要维护者独立逐卡审阅迁移，先保留原包、知识与整个更新状态，并确认使用旧运行目录的咨询已经结束；再由明确授权且有校验回执的迁移流程处理。未完成迁移时继续旧版，不能声称已升级。

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

以下业务与文件检查在安装器返回的实际 `runtime_path` 中执行，记录 `effective_version`；不能只测试临时入口源码目录就声称安装后的包可用。更新启动和状态检查仍从原安装目录的 `scripts/check_update.py` 执行，避免把版本快照误当成另一个独立安装。

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

后台模式应立即返回当前 `runtime_path`、`runtime_skill_path` 与 `effective_version`，不等待联网。Agent 读取该运行版说明，并固定用该目录完成本次咨询。默认成功检查缓存 6 小时，离线继续原版；兼容新版在后台下载、校验并供下次触发自动使用。`--status` 不联网，维护时用 `--check` 前台检查并更新，但不绕过服务器要求的限流等待。

### D. 验收回报

用用户能理解的语言报告：

- 已安装版本、应用与目录；旧包备份位置（如有）。
- 查询：原生 MCP / Python 哪条路径实际成功。
- 申请助手：已授权并通过发现，或仍待个人授权；不能用查询成功代替。
- 本地报告：实际生成的格式及示例入口。
- 是否发现新版本、任何真实失败或尚未验证项。

不要仅回复“全部搞定”而略过授权、加载或联网失败。

### E. 顾问与语音边界

语音助手是 **https://hi.xwoork.cn/voice-helper** 的系统浏览器 HTTPS 页面，不是本地 Python 音频程序，也不需要新增语音 MCP、浏览器扩展或 localhost 服务。Skill / 原生查询 MCP / 通话助手均不要求为了语音先安装 Python。

安装检查不自动开麦、配对、拨打或进入真实咨询。用户需要在线顾问时按 Skill 的触发规则打开原咨询页；用户明确要求通话时，再按 [通话说明](references/voice-calling.md) 推进。原生麦克风优先；系统请求仍在等待，不当作失败，也不重复请求。明确无 API、权限 / 容器政策阻止或用户选择后，才使用系统浏览器助手。

实际使用顺序：原咨询页保持打开 → 系统浏览器助手「准备配对」取得 10 位码 → 回原咨询页确认配对 → 助手「启用语音」并由用户允许麦克风 → 原咨询页完成同一次开始 / 接听。若原先开始 / 接听动作在配对后继续执行，不重复拨号。助手提供静音、播放、停止 / 挂断；RTC 连接必要时经 TURN 中继。不要把助手再开进同一个受限内嵌容器，不向 URL 复制原咨询身份令牌。

安装验收可核对页面和指引，并明确“尚未请求麦克风、尚未验证实际通话”。用户另行要求实际验证时，分别记录配对、设备授权、实际双向声音、静音与挂断结果。新助手已有合成链路验证，不能据此声称真人设备或所有 WorkBuddy 版本通过。旧 `localhost:18976` / `pis-voice` 测试工具不属于本次安装，不安装或唤起它们。

可让用户复制给 Agent：

> 我已经打开在线咨询，但页面提示当前应用无法使用麦克风。请保留这个咨询，在系统浏览器打开 https://hi.xwoork.cn/voice-helper，指导我取得配对码、回原页确认和启用语音。不要另建咨询，也不要安装 Python 音频程序；系统权限由我确认。

> 帮我检查留学 Skill 的安装、查询连接和通话助手说明。现在不需要开麦或真实拨号，请把已完成与尚未验证的项目分别告诉我。

## 5. 自动更新与保留旧版

安装此版本后，按 Skill 启动步骤启用后台自动更新，不需要每次询问。正式版只选择更高正式版；试用版允许更高试用版或正式版，不会被旧稳定版降级。新版本必须兼容本机能力与知识保护；发现本地修改则继续使用原内容，交给维护者处理差异。

安装目录是稳定入口；自动更新保存到每个安装目录独立的用户缓存下，校验完成才原子切换 `active.json`。当前咨询固定自己的完整运行目录，后续触发采用新版；后台不替换安装根、不改密钥、不重启宿主。入口根 `VERSION` 与实际 `effective_version` 可能不同，验收应记录两者和 `runtime_path`。旧运行目录保留，不设置会删除仍在使用版本的自动清理。

检查或安装失败会保留当前版；不完整下载没有资格成为运行版。已有卡片必须完整保留，增加新卡可自动更新；修改已有卡片内容或迁移功能需单独审核，不靠更高版本号跳过保护。旧会话需要重新载入新版启动说明才能使用此机制，通常开一个新会话即可。

需要人工回退或迁移时先记录 `--status` 返回的状态目录并保留整个目录，再按安装器备份恢复安装根，将原自动更新状态目录完整移到独立备份位置，重新载入并验证。没有确认当前咨询结束时不要移动它的运行目录。不要回退服务端数据、数据库、个人密钥或无关配置。

完整状态、检查间隔和失败处理见 [自动更新运行说明](references/runtime.md#自动更新)。

## 维护者发布检查

更新 `VERSION`、Skill 元数据与发布文案，重新生成 `PACKAGE-MANIFEST.json` 的大小和 SHA-256（清单不列自身）。仅收录公开 Skill 文件，不含 Git 元数据、Python 缓存、密钥、真实学生数据或本地报告。跑完离线测试与一次必要在线探测后打版本标签，上传 ZIP 并校验实际下载内容。

自动发布还须维护仓库根 `updates.json`：schema_version=1、repository=`luluchu857/study-abroad-planner`，channels.stable / channels.pilot 分别指向版本、固定 40 位提交 SHA 与包清单 SHA-256。可以同时固定归档大小和 SHA-256；先提交完整包，再校验该不可变提交的实际下载，最后更新目录。试用版只放 pilot，稳定版拒收预发布版本。现有入口协议为 v1；不兼容的未来入口升级需要独立迁移，不能在后台执行未知安装程序。更新目录与下载来源均固定官方仓库，不引入第三方镜像。
