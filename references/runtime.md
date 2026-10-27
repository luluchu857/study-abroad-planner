# 运行环境、连接方式与顾问窗口

安装、工具缺失或切换宿主时阅读本页；日常咨询只向学生说明结果和实际缺项。

## 两种调用路径

1. **宿主 MCP**：首次安装优先配置并验证；每次使用先检查当前工具列表，复用已连接的服务，不重复安装同名连接。
2. **包内 Python 直连**：原生 MCP 无法安装、未加载，或本次只读调用发生连接 / 协议故障时，使用 `scripts/mcp_client.py`。先复用已有 Python 3.9+，缺少运行环境才按安装指南处理。它直接实现 MCP 的初始化、工具发现和调用，不依赖先在宿主登记连接，也不需要安装 MCP SDK。

两条路径访问同一远程服务，并非把服务数据库搬到本机。详情与错误处理见 [客户端说明](mcp-client.md)。

| 服务 | 地址 | 授权 |
| --- | --- | --- |
| 查询 | `https://hi.xwoork.cn/info/mcp` | 按当前匿名查询规则 |
| 申请助手 | `https://hi.xwoork.cn/assist/mcp` | 个人 API Key |

原生连接凭据不会自动交给 Python；直连读取用户明确设置的 `LIUXUE_API_KEY` 或指定密钥文件。不能扫描其他应用的凭据，不向聊天、报告、URL 或公共仓库写密钥。查询可用与助手授权完成是两项独立结果。

只读连接故障最多切换一次直连，保持原业务参数；失败不循环。`401` / `403` 停止同类重试；`429`、配额与过宽查询按返回说明收窄或稍后处理，不属于连接降级。正常 PoW 先处理挑战；超时不自动重发有副作用的操作。不可通过换客户端规避限制。

## 原生 MCP 的依赖声明

- `agents/openai.yaml` 为支持该元数据的 OpenAI 宿主声明依赖；实际安装和授权由宿主控制。
- Codex CLI 配置方式见 [INSTALL.md](../INSTALL.md)。
- `mcp.example.json` 是通用示例，字段与变量展开须按目标应用确认。
- WorkBuddy **专家 / 插件包**可在 `.codebuddy-plugin/plugin.json` 的 `dependencies.mcpServers` 指向 `.mcp.json`；本仓库提供 `references/workbuddy-mcp.example.json` 接入示例。普通 Skill 文件本身不等于官方专家，也不能保证弹出授权卡片。

参考：[OpenAI Skill 依赖](https://developers.openai.com/plugins/build/skills)、[Codex MCP](https://developers.openai.com/codex/mcp/)、[WorkBuddy 专家依赖](https://open.workbuddy.cn/docs/expert)。

## 打开在线顾问

触发时机见 `SKILL.md` 的「在线顾问」。`open_advisor` 只是返回入口，真正打开窗口的是宿主工具。

| 宿主 | 操作 |
| --- | --- |
| WorkBuddy | 当前可用的 `present_files(cwd=当前目录, explanation="在线沟通", files=["https://hi.xwoork.cn"])` |
| Codex | 当前可用的 `open_in_codex(target={type:"browser",url:"https://hi.xwoork.cn"}, placement:"right")` |
| 其他应用 | 使用实际提供的网页打开能力；没有内嵌窗口则给出链接 |

助手授权不可用时仍可打开公共入口。工具只有 URL 或返回 queued / pending 不等于页面已显示。可用宿主浏览器工具检查现有标签、页面内容和可见状态；已有顾问标签就复用，按当前浏览器实际支持的接口显示。仍无法确认时，说明请求已排队，不反复创建窗口。入口不携带学生资料，不说已转交背景或附件。不展示不存在的 `guide_url` / `progress_url` 页面。

Skill 能说明何时打开页面，永久侧栏按钮仍取决于宿主界面功能；Python 客户端也不会自行增加宿主按钮。

## 通话助手使用系统浏览器

咨询入口可在宿主内嵌窗口显示；麦克风明确受限或用户选择助手后，语音助手应在真正的系统浏览器打开 **https://hi.xwoork.cn/voice-helper**。原咨询页保持打开。宿主的内嵌 `open_in_codex` / `present_files` 不等于打开系统浏览器；优先用当前可用的 Chrome / Edge / Safari 能力或“在浏览器打开”，缺少该能力则给用户固定链接，不虚报已经打开。

助手是无需本地安装的 HTTPS 网页，不新增 MCP 连接、不安装 Python 音频程序。按 [通话流程](voice-calling.md) 完成准备配对、原页确认 10 位码、用户启用语音和本次开始 / 接听。原生权限请求 pending 不视为失败。Python 查询客户端、旧 localhost 检测工具与真实通话媒体分别处理，不把查询连接成功当成音频验收。

## 自动更新

每次触发从原安装目录执行 `python3 <安装目录>/scripts/check_update.py --background`，读取返回的 `runtime_path`、`runtime_skill_path`、`effective_version`。读取该运行版说明后跳过已完成的启动步骤，本次咨询一直使用同一运行目录，不能中途再跑启动器重选版本。宿主没有脚本执行能力时直接用已加载的说明，不因更新阻断咨询。此触发由 Agent 遵守 Skill 指令执行，不是所有宿主都有的强制事件钩子。

后台读取官方仓库的 `updates.json`（固定 GitHub raw 地址），下载固定提交对应的包。只接受更高版本；稳定安装只跟随 stable，试用安装可选择更高的 pilot 或 stable。正常 6 小时缓存；网络失败从 5 分钟退避至最多 1 小时，403/429 按服务器要求等待，`--check` 也不绕过等待。缓存支持 ETag，避免重复传输。不读用户 GitHub Token，不修改 MCP 密钥或配置。

通过来源、清单、逐文件大小与 SHA-256、兼容能力、已有文件和知识卡保护后，新版保存到安装目录之外的独立版本目录，最后原子切换本机指针。后台不覆盖正在使用的文件、不执行下载包中的安装器或测试，也不重启应用。旧运行版一直保留，下载/校验/切换失败继续原版，不按时间猜测当前咨询已结束。检查能力声明不等于证明所有业务行为；发布者仍须完成原功能回归。

本地编辑、额外文件、知识条目变更或丢失、功能声明缺失会阻止自动切换，状态中给出原因；不要为了更新删除这些保护。知识卡目前允许保留全部原条目后追加新卡，已有卡内容修订需维护者明确迁移并重新验收，不能悄悄覆盖用户试用内容。安装目录根的 `VERSION` 是入口版本，实际本次使用版本以 `effective_version` 为准；运行目录和原目录分别保留。旧会话若仍缓存以前的启动说明，开新会话载入新入口，不强制重启应用。

`--status` 不联网；`--check` 由维护者显式执行一次前台更新；正常咨询只用 `--background`。所有更新状态以 JSON 返回，不在学生回答中堆叠技术信息或每轮提醒。更新不采集学生资料、不进入咨询、不请求音频权限。
