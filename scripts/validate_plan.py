#!/usr/bin/env python3
"""Validate anonymous planning data without making admission judgments."""
from __future__ import annotations

import argparse
from datetime import date
import json
import math
from pathlib import Path
import re
import sys
from urllib.parse import parse_qsl, urlsplit

SECTIONS = {
    "profile": "背景摘要", "conditions": "条件核对", "selection": "候选项目",
    "budget": "费用预算", "timeline": "申请时间线", "materials": "材料清单",
    "offers": "Offer 比较", "interview": "面试准备", "advisor": "真人顾问与下一步",
}
STATUSES = {
    "provided": "用户提供，未独立核验", "retrieved": "已读取，未独立复核",
    "reviewed": "已复核（输入声明）", "advice": "建议与判断", "unknown": "待核验",
}
SOURCE_KINDS = {"official", "historical_case", "student", "experience", "calculation", "synthetic"}
ID = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
EMAIL = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.I)
IDENTITY = re.compile(r"学生姓名|真实姓名|手机号|手机号码|身份证(?:号码?|编号)|护照号|学生证号|家庭住址|联系方式|邮箱|student[_ -]?name|passport[_ -]*(?:number\b|no\b\.?|id\b|#)|id[_ -]?number|phone|e-?mail", re.I)
PERSONAL_PROBABILITY = re.compile(r"个人录取(?:概率|率)|admit_probability|personal[_ -]?(?:admission[_ -]?)?probability", re.I)


class PlanError(ValueError):
    pass


def fail(path: str, message: str) -> None:
    raise PlanError(f"{path}: {message}")


def obj(value, path, required, optional=()):
    if not isinstance(value, dict):
        fail(path, "必须是对象")
    missing = set(required) - value.keys()
    extra = value.keys() - set(required) - set(optional)
    if missing:
        fail(path, "缺字段 " + ", ".join(sorted(missing)))
    if extra:
        fail(path, "包含未支持字段；请使用约定的匿名方案结构")


def text(value, path, limit=3000):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        fail(path, f"必须是 1–{limit} 字符的文本")
    if any(ord(c) < 32 and c not in "\n\r\t" for c in value):
        fail(path, "不支持控制字符")
    if EMAIL.search(value) or re.search(r"(?<!\d)\d{17}[\dXx](?!\d)", value):
        fail(path, "请移除邮箱或证件号码；方案只接受匿名摘要")


def label(value, path):
    text(value, path, 100)
    if IDENTITY.search(value) or value.strip().lower() in {"姓名", "地址", "address", "student_id"}:
        fail(path, "不支持身份或联系方式字段")
    if PERSONAL_PROBABILITY.search(value):
        fail(path, "不输出个人录取概率；可单列有来源和样本量的历史统计")


def day(value, path, nullable=False):
    if nullable and value is None:
        return None
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        fail(path, "日期须为 YYYY-MM-DD")
    try:
        return date.fromisoformat(value)
    except ValueError:
        fail(path, "日期不存在")


def scalar(value, path):
    if value is None or isinstance(value, bool):
        return
    if isinstance(value, (int, float)):
        if isinstance(value, float) and not math.isfinite(value):
            fail(path, "数字必须有限")
        return
    text(value, path)


def validate_plan(plan: dict) -> dict:
    obj(plan, "plan", {"schema_version", "title", "as_of", "sources", "sections"})
    if plan["schema_version"] != "1.0":
        fail("schema_version", "只支持 1.0")
    label(plan["title"], "title")
    as_of = day(plan["as_of"], "as_of")
    if not isinstance(plan["sources"], list) or len(plan["sources"]) > 200:
        fail("sources", "必须是最多 200 项的数组")
    sources = {}
    for i, source in enumerate(plan["sources"]):
        path = f"sources[{i}]"
        obj(source, path, {"id", "kind", "title", "url"})
        sid = source["id"]
        if not isinstance(sid, str) or not ID.fullmatch(sid) or sid in sources:
            fail(path + ".id", "来源编号无效或重复")
        if not isinstance(source["kind"], str) or source["kind"] not in SOURCE_KINDS:
            fail(path + ".kind", "来源类型无效")
        text(source["title"], path + ".title", 200)
        url = source["url"]
        if url is not None:
            text(url, path + ".url", 2000)
            try:
                parsed = urlsplit(url)
            except ValueError:
                fail(path + ".url", "来源网址格式无效")
            if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
                fail(path + ".url", "来源网址须为不含凭据的 HTTPS 地址")
            if any(re.search(r"token|secret|password|api[_-]?key|authorization", k, re.I)
                   for part in (parsed.query, parsed.fragment)
                   for k, _ in parse_qsl(part, keep_blank_values=True)):
                fail(path + ".url", "来源网址不可包含凭据参数")
        if source["kind"] == "official" and not url:
            fail(path + ".url", "学校或政策官方事实需可追溯的官网网址")
        sources[sid] = source
    sections = plan["sections"]
    if not isinstance(sections, dict) or not sections or sections.keys() - SECTIONS.keys():
        fail("sections", "至少选择一项受支持章节，不必填满所有章节")
    for section, rows in sections.items():
        if not isinstance(rows, list) or not 1 <= len(rows) <= 200:
            fail(section, "已选择章节须包含 1–200 行")
        for i, row in enumerate(rows):
            path = f"sections.{section}[{i}]"
            obj(row, path, {"label", "facts"})
            label(row["label"], path + ".label")
            if not isinstance(row["facts"], dict) or not 1 <= len(row["facts"]) <= 20:
                fail(path + ".facts", "每行须包含 1–20 个事实或建议")
            for key, fact in row["facts"].items():
                label(key, path + ".facts 字段名")
                fp = path + ".facts." + key
                obj(fact, fp, {"value", "status", "source_ids", "checked_at"}, {"valid_until", "note"})
                status = fact["status"]
                if not isinstance(status, str) or status not in STATUSES:
                    fail(fp + ".status", "证据状态无效")
                value = fact["value"]
                if isinstance(value, list):
                    if not value or len(value) > 30:
                        fail(fp + ".value", "数组须有 1–30 项")
                    for j, item in enumerate(value):
                        scalar(item, fp + f".value[{j}]")
                else:
                    scalar(value, fp + ".value")
                if (status == "unknown") != (value is None):
                    fail(fp, "unknown 必须使用 null；已知值不得使用 null")
                refs = fact["source_ids"]
                if not isinstance(refs, list) or any(not isinstance(x, str) or x not in sources for x in refs):
                    fail(fp + ".source_ids", "来源编号必须存在于 sources")
                if len(refs) != len(set(refs)):
                    fail(fp + ".source_ids", "来源编号重复")
                checked = day(fact["checked_at"], fp + ".checked_at", True)
                if checked and checked > as_of:
                    fail(fp + ".checked_at", "查询日期不能晚于方案日期")
                if "valid_until" in fact:
                    day(fact["valid_until"], fp + ".valid_until", True)
                note = fact.get("note")
                if note is not None:
                    text(note, fp + ".note")
                if status in {"provided", "retrieved", "reviewed"} and (not refs or checked is None):
                    fail(fp, "事实须有来源和读取/提供日期")
                if status == "provided" and any(sources[x]["kind"] not in {"student", "synthetic"} for x in refs):
                    fail(fp, "provided 只用于用户提供或合成示例")
                if status == "reviewed" and (not note or not any(sources[x]["kind"] == "official" for x in refs)):
                    fail(fp, "reviewed 须有官方来源及具体独立复核说明；程序不会自动升级状态")
                if status == "advice" and not note:
                    fail(fp, "建议须写 note 说明判断依据；一般方法可不编造来源网址")
    return plan


def load_plan(path: Path) -> dict:
    try:
        if path.stat().st_size > 2 * 1024 * 1024:
            raise PlanError("方案超过 2 MiB；请只提供匿名结构化摘要")
        def no_constant(_):
            raise PlanError("JSON 不支持 NaN 或 Infinity")
        def unique_keys(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise PlanError("JSON 包含重复字段，无法确定要保留的来源或事实")
                result[key] = value
            return result
        plan = json.loads(path.read_text(encoding="utf-8"), parse_constant=no_constant, object_pairs_hook=unique_keys)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PlanError("无法读取有效 UTF-8 JSON 方案") from exc
    return validate_plan(plan)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    args = parser.parse_args(argv)
    try:
        plan = load_plan(args.input)
    except PlanError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(json.dumps({"valid": True, "sections": list(plan["sections"]),
                      "note": "结构通过不代表学校事实已核实或录取判断正确"}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
