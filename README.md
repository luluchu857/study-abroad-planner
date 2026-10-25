# 留学规划助手 · Study Abroad Planner

当前 Skill 版本：**0.7.1**。

一个配合远程 MCP 服务使用的留学规划 Skill，帮助学生梳理目标、查询院校与专业、比较申请条件和匿名录取案例、整理材料与时间线，并按需衔接在线顾问。覆盖新加坡、中国香港、马来西亚、英国、澳大利亚、美国、加拿大；各地区的数据覆盖程度不同，以工具实际返回结果为准。

## 文件结构

```text
SKILL.md                       Skill 入口、对话流程与工具使用规则
pow_client.py                  部分 MCP 调用所需的 PoW 校验辅助脚本
references/advisor-methods.md   按需读取的顾问经验与方法参考
README.md                      本说明
agents/openai.yaml             OpenAI 宿主的展示信息与 MCP 依赖声明
mcp.example.json               两项 MCP 连接的无密钥配置示例
references/runtime.md          宿主适配、顾问窗口与 MCP 连接说明
```

本目录只包含 Skill、辅助代码和参考文本，不包含 MCP 服务端、业务数据库、学生档案、API Key 或线上系统部署配置。

## 使用条件

需要一个能够加载 Skill 并调用 MCP 工具的 AI 助手。下载 Release 的 Skill ZIP 后解压，导入 `study-abroad-planner` 文件夹；安装位置和重新加载方式按所用宿主说明操作。支持 OpenAI 技能依赖元数据的宿主可读取 `agents/openai.yaml`；其他宿主按 `mcp.example.json` 与 [运行环境说明](references/runtime.md) 配置连接。依赖声明不等于已安装成功，个人授权仍需单独完成。

该 Skill 依赖两项独立的远程服务：

| MCP | 用途 | 服务地址与授权 |
| --- | --- | --- |
| `liuxue-info-query` | 专业搜索、条件与案例查询、排名、费用等信息 | [查询 MCP](https://hi.xwoork.cn/info/mcp)，按服务当前规则访问 |
| `liuxue-application-helper` | 材料清单、文书提纲、时间线、在线顾问等申请辅助 | [申请助手 MCP](https://hi.xwoork.cn/assist/mcp)，需要服务方提供的个人 API Key |

MCP 通过 Streamable HTTP 连接。请在宿主自己的私密配置中保存授权信息，不要把真实 Key 写入 Skill、仓库或公开示例。远程服务的可用性、权限和额度由服务运营方维护；下载 Skill 本身不等于获得服务授权。

部分工具要求 PoW 校验，`pow_client.py` 提供相应求解与预计算辅助逻辑。该脚本使用 Python 标准库，建议 Python 3.10 或更新版本；它不会自行配置 MCP。辅助类的后台预计算仅在宿主实际调用并保留相应进程时生效。

在线顾问入口为 [hi.xwoork.cn](https://hi.xwoork.cn)。打开入口不表示学生的背景、方案或附件已经传给顾问。

## 什么时候打开在线顾问

学生明确找真人顾问或求推荐、反复拿不准，或者完整评估结束需要真人复核时，按 [Skill 的在线顾问流程](SKILL.md#在线顾问) 打开入口。WorkBuddy 使用 `present_files`，Codex 使用 `open_in_codex` 在右侧打开。助手 MCP 未连接时也可直接打开公共入口；不支持侧栏的宿主提供网页链接。这里包含打开规则，不包含宿主永久侧栏按钮的界面代码。

## v0.7.1 调整

- 移除方法参考逐条附加的固定经验标签；具体学校、费用和政策仍引用实际来源。
- 修正 Skill 元数据格式，补齐 Codex/WorkBuddy 工具适配和 MCP 依赖声明、配置示例。
- 取消统一 150 字上限，完整方案按任务需要展开。
- 纠正递交状态描述：取得入口不等于已登记或已提交材料。
- 修复 PoW 辅助类对无效挑战的处理，以及旧后台结果覆盖新挑战的问题；业务请求仍最多重试一次。

本版本没有合并旧 `study-abroad-counseling` 的方案生成器、导出脚本或 HTTP 客户端；这些属于另一套接口与工作流。

## 数据与使用边界

- 院校条件、费用、政策和截止日期应以学校及相关机构的最新官方信息为准。
- 历史匿名案例及工具返回的估算不代表个人录取概率，也不构成录取承诺。
- `references/advisor-methods.md` 是顾问经验参考，含需要再次核实的经验判断和数字，不是官方规则。
- MCP 不可用时，Skill 应说明未查库，不能把通用知识描述为已核实的数据。
- 申请辅助不等于向学校正式递交或付款；具体操作须遵循 Skill 中的确认流程。

## 更新状态

**当前版本没有自动更新器。** 它不会在每次触发时自动检查 GitHub、下载新文件或替换自身。远程 MCP 服务更新与本地 Skill 文件更新是两件事；本地文件更新后，是否需要新会话或重新加载取决于宿主。

## English overview

Study Abroad Planner is an Agent Skill for study planning, admission research, application preparation, and optional access to an online advisor. It uses two remote MCP services: `liuxue-info-query` and `liuxue-application-helper`. The latter requires an API key supplied by the service operator.

This repository contains Skill instructions, a Python standard-library PoW helper, advisor-method notes, and MCP dependency/configuration examples. It contains no MCP server, student records, business database, credentials, or deployment configuration. Host support and remote service availability must be checked separately. Version **0.7.1 includes neither an automatic updater nor a direct MCP/HTTP client**.
