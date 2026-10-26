#!/usr/bin/env python3
"""Create local JSON, Markdown, CSV and self-contained HTML from an anonymous plan."""
from __future__ import annotations

import argparse
import csv
from datetime import date
import html
import json
from pathlib import Path
import sys

from validate_plan import PlanError, SECTIONS, STATUSES, load_plan, validate_plan

KIND_LABELS = {"official": "官方来源", "historical_case": "历史案例", "student": "用户提供",
               "experience": "经验参考", "calculation": "计算依据", "synthetic": "合成演示"}
NOTICE = "本报告区分来源事实与建议；结构校验不代表事实已核实。案例样本比例不代表个人录取概率。"
HEADERS = ["章节", "项目", "字段", "值", "证据状态", "来源编号", "来源说明", "查询/提供日期", "有效至", "时效", "依据/备注"]


def value_text(value):
    if value is None:
        return "未知 / 待核验"
    if isinstance(value, list):
        return "；".join(value_text(x) for x in value)
    if isinstance(value, bool):
        return "是" if value else "否"
    return str(value)


def is_stale(fact, as_of):
    until = fact.get("valid_until")
    return bool(until and date.fromisoformat(until) < date.fromisoformat(as_of))


def source_text(fact, sources):
    if not fact["source_ids"]:
        return "无来源：待核验" if fact["status"] == "unknown" else "方法建议，依据见备注"
    return "；".join(f"{sid}: {sources[sid]['title']}（{KIND_LABELS[sources[sid]['kind']]}）"
                     + (f" {sources[sid]['url']}" if sources[sid]["url"] else "") for sid in fact["source_ids"])


def evidence(fact, as_of, sources):
    status = STATUSES[fact["status"]]
    if any(sources[x]["kind"] == "synthetic" for x in fact["source_ids"]):
        status = "合成演示；" + status
    stale = "已过期，需重新核验" if is_stale(fact, as_of) else ("未设有效期，需按任务复核" if not fact.get("valid_until") else "未到所填有效期，不等于已核实")
    return status, stale


def cells(section, row, key, fact, plan, sources):
    status, stale = evidence(fact, plan["as_of"], sources)
    return [SECTIONS[section], row["label"], key, value_text(fact["value"]), status,
            ", ".join(fact["source_ids"]), source_text(fact, sources),
            fact["checked_at"] or "未记录", fact.get("valid_until") or "未设", stale, fact.get("note", "")]


def all_cells(plan):
    sources = {x["id"]: x for x in plan["sources"]}
    for section, rows in plan["sections"].items():
        for row in rows:
            for key, fact in row["facts"].items():
                yield cells(section, row, key, fact, plan, sources)


def csv_safe(value):
    text = str(value)
    # Prevent a spreadsheet from treating untrusted labels or values as formulas.
    if text.lstrip().startswith(("=", "+", "-", "@")):
        return "'" + text
    return text


def md(value):
    # Entities are displayed as text after Markdown parsing; untrusted image/link
    # syntax must never become a remote resource in a Markdown preview.
    escaped = html.escape(str(value), quote=True)
    # Escape URL punctuation too, so GFM autolinking cannot re-activate a URL
    # after its surrounding explicit link/image syntax has been neutralized.
    punctuation = {ord(char): f"&#{ord(char)};" for char in "\\[]()!`|*_~:./"}
    return escaped.translate(punctuation).replace("\r", "").replace("\n", "<br>")


def visible_rows(rows, mode):
    return rows[:2] if mode == "brief" else rows


def markdown_report(plan, mode):
    sources = {x["id"]: x for x in plan["sources"]}
    out = [f"# {md(plan['title'])}", "", f"方案日期：{plan['as_of']}", "", NOTICE, ""]
    if any(x["kind"] == "synthetic" for x in plan["sources"]):
        out.extend(["**包含合成演示数据；虚构项目、费用和条件均不可用于真实申请。**", ""])
    if mode == "brief":
        out.extend(["简版每节最多展示 2 项；完整数据保留在 plan.json 和 report.csv。", ""])
    for section, rows in plan["sections"].items():
        out.extend(["## " + SECTIONS[section], "", "| 项目 / 字段 | 内容 | 来源与日期 | 状态与依据 |", "|---|---|---|---|"])
        for row in visible_rows(rows, mode):
            for key, fact in row["facts"].items():
                status, stale = evidence(fact, plan["as_of"], sources)
                origin = source_text(fact, sources) + "；日期：" + (fact["checked_at"] or "未记录")
                detail = f"{status}；{stale}；有效至：{fact.get('valid_until') or '未设'}"
                if fact.get("note"):
                    detail += "；" + fact["note"]
                out.append("| " + " | ".join(md(x) for x in [row["label"] + " / " + key, value_text(fact["value"]), origin, detail]) + " |")
        if len(visible_rows(rows, mode)) < len(rows):
            out.append(f"\n另有 {len(rows)-2} 项见完整 JSON/CSV。")
        out.append("")
    return "\n".join(out)


CSS = """
:root{color-scheme:light;--ink:#17352e;--soft:#64756e;--line:#dce7e1;--paper:#f4f7f4}
*{box-sizing:border-box}body{margin:0;background:var(--paper);color:var(--ink);font:15px/1.65 system-ui,-apple-system,sans-serif}
main{max-width:1120px;margin:auto;padding:32px 22px 60px}header{padding:10px 0 24px;border-bottom:1px solid var(--line)}
h1{font-size:30px;line-height:1.3;margin:8px 0 16px}h2{font-size:22px;margin:32px 0 14px}h3{font-size:17px;margin:0 0 10px}
p{margin:8px 0}.eyebrow{color:var(--soft);font-size:12px;letter-spacing:.08em}.note{color:var(--soft);font-size:13px}
.warning{background:#fff3d6;border-left:4px solid #b78322;padding:10px 14px;border-radius:5px}
.controls{display:flex;gap:12px;flex-wrap:wrap;padding:20px 0}.controls label{display:flex;flex-direction:column;gap:5px;font-size:12px;color:var(--soft)}
input,select{font:inherit;padding:10px 12px;border:1px solid #bdcec4;border-radius:7px;background:white;color:var(--ink)}input{min-width:260px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,420px),1fr));gap:14px}.card{background:white;border:1px solid var(--line);border-radius:12px;padding:20px}
dl{margin:0}.fact{padding:10px 0;border-top:1px solid #edf1ed}dt{font-size:12px;color:var(--soft)}dd{margin:4px 0 0;overflow-wrap:anywhere;white-space:pre-wrap}
.value{font-size:16px;font-weight:550}.meta{display:block;font-size:12px;color:var(--soft);margin-top:5px}.expired{color:#9c5219}.sources{font-size:13px;overflow-wrap:anywhere}
[hidden]{display:none!important}footer{margin-top:36px;padding-top:20px;border-top:1px solid var(--line);color:var(--soft);font-size:12px}
@media(max-width:540px){main{padding:20px 14px}h1{font-size:25px}.controls label,input,select{width:100%}.card{padding:16px}}
@media print{body{background:white}.controls{display:none}.card{break-inside:avoid}.grid{display:block}.card{margin-bottom:12px}}
"""
JS = """
const query=document.getElementById('query'),section=document.getElementById('section'),state=document.getElementById('state');
function apply(){const q=query.value.trim().toLocaleLowerCase();let count=0;
document.querySelectorAll('.card').forEach(card=>{const ok=(!q||card.textContent.toLocaleLowerCase().includes(q))&&(!section.value||card.dataset.section===section.value)&&(!state.value||card.dataset.states.split(' ').includes(state.value));card.hidden=!ok;if(ok)count++;});
document.querySelectorAll('.report-section').forEach(node=>{node.hidden=![...node.querySelectorAll('.card')].some(card=>!card.hidden);});
document.getElementById('count').textContent=`显示 ${count} 项`;document.getElementById('empty').hidden=count!==0;}
[query,section,state].forEach(el=>el.addEventListener('input',apply));apply();
"""


def html_report(plan, mode):
    e = html.escape
    sources = {x["id"]: x for x in plan["sources"]}
    parts = ["<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">",
             "<meta http-equiv=\"Content-Security-Policy\" content=\"default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; base-uri 'none'; form-action 'none'; connect-src 'none'\">",
             f"<title>{e(plan['title'])}</title><style>{CSS}</style></head><body><main>",
             f"<header><div class=\"eyebrow\">留学规划 · {plan['as_of']}</div><h1>{e(plan['title'])}</h1><p class=\"note\">{NOTICE}</p>"]
    if any(x["kind"] == "synthetic" for x in plan["sources"]):
        parts.append('<p class="warning">合成演示：项目、费用和条件均为虚构，不能用于真实申请。</p>')
    if mode == "brief":
        parts.append('<p class="note">简版每节展示前 2 项；完整数据见 plan.json 和 report.csv。</p>')
    parts.append('</header><div class="controls"><label>搜索<input id="query" type="search" placeholder="项目、条件或关键词"></label><label>章节<select id="section"><option value="">全部章节</option>')
    parts.extend(f'<option value="{key}">{SECTIONS[key]}</option>' for key in plan["sections"])
    parts.append('</select></label><label>证据状态<select id="state"><option value="">全部状态</option>')
    for key, title in [("stale", "已过期"), ("synthetic", "合成演示"), *STATUSES.items()]:
        parts.append(f'<option value="{key}">{e(title)}</option>')
    parts.append('</select></label></div><p id="count" class="note" role="status" aria-live="polite"></p><p id="empty" hidden>没有匹配项目，可清空筛选。</p><noscript><p class="note">当前环境未运行脚本；全部内容仍可阅读，筛选不可用。</p></noscript>')
    for section, rows in plan["sections"].items():
        parts.append(f'<section class="report-section"><h2>{SECTIONS[section]}</h2><div class="grid">')
        for row in visible_rows(rows, mode):
            states = {f["status"] for f in row["facts"].values()}
            if any(is_stale(f, plan["as_of"]) for f in row["facts"].values()):
                states.add("stale")
            if any(sources[sid]["kind"] == "synthetic" for f in row["facts"].values() for sid in f["source_ids"]):
                states.add("synthetic")
            parts.append(f'<article class="card" data-section="{section}" data-states="{" ".join(sorted(states))}"><h3>{e(row["label"])}</h3><dl>')
            for key, fact in row["facts"].items():
                status, stale = evidence(fact, plan["as_of"], sources)
                parts.append(f'<div class="fact"><dt>{e(key)}</dt><dd><span class="value">{e(value_text(fact["value"]))}</span><span class="meta">{e(status)} · 查询/提供：{fact["checked_at"] or "未记录"} · 有效至：{fact.get("valid_until") or "未设"}</span>')
                parts.append(f'<span class="meta {"expired" if is_stale(fact, plan["as_of"]) else ""}">{e(stale)}</span><span class="meta">{e(source_text(fact, sources))}</span>')
                if fact.get("note"):
                    parts.append(f'<span class="meta">依据 / 备注：{e(fact["note"])}</span>')
                parts.append('</dd></div>')
            parts.append('</dl></article>')
        parts.append('</div></section>')
    parts.append('<footer>全部内容来自输入方案；页面没有外部字体、CDN、统计或网络请求。来源网址仅作为文本展示。生成器不查询学校、不升级证据状态、不发送资料，也不登记咨询。</footer>')
    parts.append(f'</main><script>{JS}</script></body></html>')
    return "".join(parts)


def build_plan(plan, output: Path, mode="full", xlsx=False):
    validate_plan(plan)
    if mode not in {"brief", "full"}:
        raise PlanError("mode 须为 brief 或 full")
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        output.mkdir()
    except FileExistsError as exc:
        raise PlanError("输出目录已存在；请使用新的版本目录，不覆盖旧方案") from exc
    created = []
    warnings = []
    def write(name, content):
        path = output / name
        with path.open("x", encoding="utf-8") as handle:
            created.append(path)
            handle.write(content)
    try:
        write("plan.json", json.dumps(plan, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
        write("report.md", markdown_report(plan, mode))
        write("report.html", html_report(plan, mode))
        path = output / "report.csv"
        with path.open("x", encoding="utf-8-sig", newline="") as handle:
            created.append(path)
            writer = csv.writer(handle)
            writer.writerow(HEADERS)
            writer.writerows([csv_safe(v) for v in row] for row in all_cells(plan))
        if xlsx:
            try:
                from openpyxl import Workbook
            except ImportError:
                warnings.append("未安装 openpyxl；已保留完整 CSV，可直接用 Excel 打开。")
            else:
                workbook = Workbook()
                sheet = workbook.active
                sheet.title = "留学方案"
                sheet.append(HEADERS)
                for row in all_cells(plan):
                    sheet.append([csv_safe(v) for v in row])
                sheet.freeze_panes = "A2"
                sheet.auto_filter.ref = sheet.dimensions
                for col in sheet.columns:
                    sheet.column_dimensions[col[0].column_letter].width = min(55, max(14, max(len(str(c.value or "")) for c in col) + 2))
                path = output / "report.xlsx"
                with path.open("xb") as handle:
                    created.append(path)
                    workbook.save(handle)
        return {"output": str(output.resolve()), "files": [x.name for x in created], "warnings": warnings}
    except Exception:
        for path in reversed(created):
            path.unlink(missing_ok=True)
        try:
            output.rmdir()
        except OSError:
            pass
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--mode", choices=["brief", "full"], default="full")
    parser.add_argument("--xlsx", action="store_true", help="可选 openpyxl；不可用时保留 CSV")
    args = parser.parse_args(argv)
    try:
        result = build_plan(load_plan(args.input), args.output, args.mode, args.xlsx)
    except (PlanError, OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
