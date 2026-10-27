# 培训知识查询

按学生当前问题检索相关知识，学校事实仍使用原院校 MCP。不要先把全部资料读入上下文。

## 可用入口

若当前连接的留学服务实际提供 `search_knowledge` / `get_knowledge`，先读它们的 schema 后调用。只有工具名相似、或文档列出名称，不等于已安装或已部署。

宿主目录没有这两项工具时，先用包内客户端发现同一查询服务的当前目录；有些宿主需要重新连接才会刷新工具：

```sh
python3 <skill>/scripts/mcp_client.py list-tools --server query
```

目录确实包含知识工具后，按其实际参数调用：

```sh
python3 <skill>/scripts/mcp_client.py call --server query --tool search_knowledge --args-json '{"query":"合作办学是否适合我","limit":3}'
python3 <skill>/scripts/mcp_client.py call --server query --tool get_knowledge --args-json '{"card_id":"<搜索实际返回的id>"}'
```

不要把这里示例的尖括号当真实ID。客户端沿用原查询服务的只读权限、PoW和频率限制；遇到拒绝或限流，不换路径重复请求。旧服务没有知识工具，或者按启动规则确认远程不可用时，使用随包本地快照：

```sh
python3 <skill>/scripts/knowledge_query.py search --query '合作办学是否适合我' --limit 3
python3 <skill>/scripts/knowledge_query.py get --id <搜索实际返回的id>
```

`<skill>` 是这份 Skill 的真实目录；不要假定当前工作目录。可按实际 CLI 帮助使用国家和层次筛选。学校原生 MCP 和本地知识查询是不同数据来源，不能声称本地卡片已进入远程学校库。

遇到学校简称、新项目或转述的培训信息，先以学生原话检索，不先加猜测的国家筛选。完整卡的专业名和官方链接用来识别对象，再查对应学校事实。没有精确对象时先澄清，不用相似专业代填费用或入学要求。

## 使用结果

- `experience_reviewed` 表示经验内容已对照来源整理，适合用于咨询判断；不表示招生数字、政策已获官方确认。
- `official_verified` 仅覆盖该卡明确核对的结论和适用范围，仍检查日期、申请季和身份。
- 搜索摘要用于选卡，涉及适配结论时继续读完整条件、例外和下一步；不能只看标题下结论。
- 用户问当前政策，必要时补查现行官网；过期、冲突或待核内容不能当当前事实。
- 没有命中就继续原学校查询或一般方法，并说明具体缺口；不自行修改快照、扩大受众权限或搜私人审核文件补答案。

源文件与原文定位只在内部审核档案保留，公共快照不含私人路径、学生身份或商业返佣内容。以实际返回的快照版本、覆盖说明和每条核验范围为准：会议被审阅不等于所有口述事实已获官方确认，未发表的候选也不是可查询的事实。
