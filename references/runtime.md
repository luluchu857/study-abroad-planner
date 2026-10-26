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

助手授权不可用时仍可打开公共入口。只在窗口工具确认成功后说已打开；工具只有 URL 不算成功。入口不携带学生资料，不说已转交背景或附件。不展示不存在的 `guide_url` / `progress_url` 页面。

Skill 能说明何时打开页面，永久侧栏按钮仍取决于宿主界面功能；Python 客户端也不会自行增加宿主按钮。

## 通话助手使用系统浏览器

咨询入口可在宿主内嵌窗口显示；麦克风明确受限或用户选择助手后，语音助手应在真正的系统浏览器打开 **https://hi.xwoork.cn/voice-helper**。原咨询页保持打开。宿主的内嵌 `open_in_codex` / `present_files` 不等于打开系统浏览器；优先用当前可用的 Chrome / Edge / Safari 能力或“在浏览器打开”，缺少该能力则给用户固定链接，不虚报已经打开。

助手是无需本地安装的 HTTPS 网页，不新增 MCP 连接、不安装 Python 音频程序。按 [通话流程](voice-calling.md) 完成准备配对、原页确认 10 位码、用户启用语音和本次开始 / 接听。原生权限请求 pending 不视为失败。Python 查询客户端、旧 localhost 检测工具与真实通话媒体分别处理，不把查询连接成功当成音频验收。

## 更新检查

具备本地执行能力时，每次触发运行 `python3 <skill>/scripts/check_update.py --background`，其中 `<skill>` 替换为实际安装目录，读取返回的缓存状态即可继续咨询。缓存 6 小时；远程检测在后台、带超时和并发锁，不自动下载或替换当前包。后台进程不直接向聊天推送，结果由后续 `--status` 或下一次触发读取。

`--status` 仅读本地状态，`--check` 手动立即检查。无命令权限时跳过检测，不让安装或网络问题拦住日常咨询。更新按照 INSTALL.md 下载指定版本、校验并运行安装器，原目录先保留备份。
