"""Behavioral tests for anonymous local deliverables; no network or real student data."""
from __future__ import annotations

import builtins
import copy
import csv
from html.parser import HTMLParser
from html import unescape
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from build_plan import build_plan, html_report, markdown_report
from validate_plan import PlanError, load_plan, validate_plan


def advice(value="先核对目标和预算。"):
    return {"value": value, "status": "advice", "source_ids": [], "checked_at": None,
            "note": "一般咨询方法；没有声称是学校规则。"}


def minimal():
    return {"schema_version": "1.0", "title": "匿名方案", "as_of": "2026-10-26", "sources": [],
            "sections": {"materials": [{"label": "准备顺序", "facts": {"建议": advice()}}]}}


class Tags(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags = []
        self.attrs = []
    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)
        self.attrs.extend(attrs)


class PlanTests(unittest.TestCase):
    def test_real_template_valid_and_has_whole_workflow(self):
        plan = load_plan(ROOT / "templates/plan.example.json")
        self.assertEqual(set(plan["sections"]), {"profile", "conditions", "selection", "budget", "timeline", "materials", "offers", "interview", "advisor"})
        self.assertTrue(all(s["kind"] == "synthetic" for s in plan["sources"]))

    def test_single_task_needs_only_one_section_and_advice_needs_no_fake_url(self):
        self.assertEqual(validate_plan(minimal())["sources"], [])

    def test_advice_requires_reason(self):
        plan = minimal()
        del plan["sections"]["materials"][0]["facts"]["建议"]["note"]
        with self.assertRaises(PlanError):
            validate_plan(plan)

    def test_unknown_cannot_contain_guessed_value(self):
        plan = minimal()
        plan["sections"]["materials"][0]["facts"]["建议"]["status"] = "unknown"
        with self.assertRaises(PlanError):
            validate_plan(plan)

    def test_facts_require_source_date_and_real_reference(self):
        plan = minimal()
        fact = plan["sections"]["materials"][0]["facts"]["建议"]
        fact["status"] = "retrieved"
        for refs, checked in [([], None), (["missing"], "2026-10-26")]:
            fact.update(source_ids=refs, checked_at=checked)
            with self.assertRaises(PlanError):
                validate_plan(plan)

    def test_official_sources_require_https_and_no_secret_query(self):
        for url in [None, "javascript:alert(1)", "https://user:password@example.edu", "https://example.edu/?token=example"]:
            plan = minimal()
            plan["sources"] = [{"id": "official", "kind": "official", "title": "项目官网", "url": url}]
            with self.assertRaises(PlanError):
                validate_plan(plan)

    def test_source_fragments_reject_credentials_but_allow_section_anchors(self):
        for fragment in ["access_token=synthetic", "api_key=synthetic", "access_token", "route?access_token=synthetic", "%61ccess_token=synthetic"]:
            with self.subTest(fragment=fragment):
                plan = minimal()
                plan["sources"] = [{"id": "official", "kind": "official", "title": "项目官网", "url": "https://example.edu/#" + fragment}]
                with self.assertRaises(PlanError):
                    validate_plan(plan)
        plan["sources"][0]["url"] = "https://example.edu/program#entry-requirements"
        self.assertEqual(validate_plan(plan), plan)

    def test_reviewed_needs_evidence_note_and_does_not_upgrade_retrieved(self):
        plan = minimal()
        plan["sources"] = [{"id": "official", "kind": "official", "title": "示例官网", "url": "https://example.edu/program"}]
        fact = {"value": "测试字段", "status": "reviewed", "source_ids": ["official"], "checked_at": "2026-10-25"}
        plan["sections"]["materials"][0]["facts"] = {"要求": fact}
        with self.assertRaises(PlanError):
            validate_plan(plan)
        fact["note"] = "输入者记录独立复核过程。"
        validate_plan(plan)
        fact["status"] = "retrieved"
        before = copy.deepcopy(plan)
        report = html_report(validate_plan(plan), "full")
        self.assertIn("未独立复核", report)
        self.assertEqual(plan, before)

    def test_date_invalid_and_future_checked_rejected(self):
        for value in ["2026-02-30", "20261026", "2027-01-01"]:
            plan = minimal()
            plan["sections"]["materials"][0]["facts"]["建议"]["checked_at"] = value
            with self.assertRaises(PlanError):
                validate_plan(plan)

    def test_unknown_and_stale_are_visible_in_all_formats(self):
        plan = load_plan(ROOT / "templates/plan.example.json")
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "v1"
            build_plan(plan, output)
            for name in ["report.html", "report.md", "report.csv"]:
                data = (output / name).read_text(encoding="utf-8-sig")
                self.assertIn("已过期", data)
                self.assertIn("待核验", data)
                self.assertIn("合成演示", data)

    def test_identity_and_personal_probability_rejected(self):
        for key in ["学生姓名", "手机号", "邮箱", "个人录取概率", "admit_probability"]:
            plan = minimal()
            plan["sections"]["materials"][0]["facts"] = {key: advice()}
            with self.assertRaises(PlanError):
                validate_plan(plan)
        plan = minimal()
        plan["sections"]["materials"][0]["facts"]["建议"]["value"] = "example@example.test"
        with self.assertRaises(PlanError):
            validate_plan(plan)

    def test_passport_material_names_allowed_but_identity_number_fields_rejected(self):
        for name in ["Passport", "Passport copy", "Passport bio page", "护照复印件", "身份证复印件"]:
            with self.subTest(name=name):
                plan = minimal()
                plan["sections"]["materials"][0].update(label=name, facts={name: advice("按项目清单核对所需文件。")})
                self.assertEqual(validate_plan(plan), plan)
        for name in ["Passport number", "passport_no", "Passport No.", "passport ID", "Passport #", "护照号码", "身份证号码"]:
            with self.subTest(name=name):
                plan = minimal()
                plan["sections"]["materials"][0]["facts"] = {name: advice()}
                with self.assertRaises(PlanError):
                    validate_plan(plan)

    def test_html_and_markdown_escape_untrusted_text(self):
        payload = '</script><img src=x onerror="alert(1)"> | `x`'
        plan = minimal()
        plan["title"] = "<b>测试</b>"
        plan["sections"]["materials"][0]["label"] = payload
        plan["sections"]["materials"][0]["facts"]["建议"]["value"] = payload
        validate_plan(plan)
        rendered = html_report(plan, "full")
        tags = Tags()
        tags.feed(rendered)
        self.assertNotIn("img", tags.tags)
        self.assertNotIn("b", tags.tags)
        self.assertEqual(tags.tags.count("script"), 1)
        self.assertFalse(any(k.startswith("on") for k, _ in tags.attrs))
        markdown = markdown_report(plan, "full")
        self.assertNotIn("<img", markdown)
        self.assertIn("&#124;", markdown)

    def test_html_has_no_remote_resources_or_network_script(self):
        report = html_report(minimal(), "full")
        tags = Tags()
        tags.feed(report)
        self.assertFalse(any(k in {"src", "href", "action"} for k, _ in tags.attrs))
        self.assertNotIn("fetch(", report)
        self.assertNotIn("XMLHttpRequest", report)
        self.assertIn("connect-src 'none'", report)

    def test_markdown_image_link_and_reference_syntax_remain_literal(self):
        payload = r'![remote](https://example.invalid/pixel) [link](https://example.invalid/) ![ref][image] [image]: https://example.invalid/ref \\[escaped](https://example.invalid/)'
        plan = minimal()
        plan["title"] = "![title](https://example.invalid/title)"
        row = plan["sections"]["materials"][0]
        row["label"] = "![row](https://example.invalid/row)"
        row["facts"] = {"[field](https://example.invalid/field)": advice(payload)}
        row["facts"][next(iter(row["facts"]))]["note"] = payload
        validate_plan(plan)
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "v1"
            build_plan(plan, output)
            markdown = (output / "report.md").read_text()
            self.assertNotIn("[", markdown)
            self.assertNotIn("]", markdown)
            self.assertIn("https://example.invalid/pixel", unescape(markdown))
            self.assertNotIn("https://", markdown)
            # Escaping changes only the presentation; structured data stays exact.
            self.assertEqual(load_plan(output / "plan.json"), plan)

    def test_generated_csv_protects_formula_and_preserves_newlines(self):
        plan = minimal()
        plan["sections"]["materials"][0]["facts"]["建议"]["value"] = '=HYPERLINK("https://example.test")\n第二行'
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "v1"
            build_plan(plan, output)
            with (output / "report.csv").open(encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.reader(handle))
            self.assertTrue(rows[1][3].startswith("'="))
            self.assertIn("\n第二行", rows[1][3])

    def test_existing_output_not_overwritten_even_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "v1"
            output.mkdir()
            with self.assertRaises(PlanError):
                build_plan(minimal(), output)
            sentinel = output / "existing.txt"
            sentinel.write_text("keep")
            with self.assertRaises(PlanError):
                build_plan(minimal(), output)
            self.assertEqual(sentinel.read_text(), "keep")
            self.assertEqual(len(list(output.iterdir())), 1)

    def test_validation_failure_creates_no_output(self):
        plan = minimal()
        plan["as_of"] = "bad"
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "v1"
            with self.assertRaises(PlanError):
                build_plan(plan, output)
            self.assertFalse(output.exists())

    def test_malformed_enum_and_url_give_validation_error(self):
        plan = minimal()
        plan["sections"]["materials"][0]["facts"]["建议"]["status"] = []
        with self.assertRaises(PlanError):
            validate_plan(plan)
        for kind, url in [([], None), ("official", "https://[bad")]:
            plan = minimal()
            plan["sources"] = [{"id": "source", "kind": kind, "title": "来源", "url": url}]
            with self.assertRaises(PlanError):
                validate_plan(plan)

    def test_duplicate_json_fields_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "plan.json"
            path.write_text('{"schema_version":"1.0","schema_version":"2.0"}')
            with self.assertRaises(PlanError):
                load_plan(path)

    def test_failed_render_does_not_leave_half_report(self):
        with tempfile.TemporaryDirectory() as tmp, patch("build_plan.html_report", side_effect=RuntimeError("test failure")):
            output = Path(tmp) / "v1"
            with self.assertRaises(RuntimeError):
                build_plan(minimal(), output)
            self.assertFalse(output.exists())

    def test_brief_is_explicit_and_preserves_full_json_csv(self):
        plan = minimal()
        plan["sections"]["materials"] = [{"label": f"材料{i}", "facts": {"建议": advice()}} for i in range(4)]
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "brief"
            build_plan(plan, output, "brief")
            self.assertNotIn("材料3", (output / "report.html").read_text())
            self.assertIn("简版", (output / "report.html").read_text())
            self.assertIn("材料3", (output / "report.csv").read_text())
            self.assertEqual(len(json.loads((output / "plan.json").read_text())["sections"]["materials"]), 4)

    def test_xlsx_missing_dependency_is_clear_csv_fallback(self):
        original = builtins.__import__
        def missing(name, *args, **kwargs):
            if name == "openpyxl":
                raise ImportError("test missing")
            return original(name, *args, **kwargs)
        with tempfile.TemporaryDirectory() as tmp, patch("builtins.__import__", side_effect=missing):
            result = build_plan(minimal(), Path(tmp) / "v1", xlsx=True)
            self.assertIn("report.csv", result["files"])
            self.assertNotIn("report.xlsx", result["files"])
            self.assertEqual(len(result["warnings"]), 1)

    def test_cli_generates_real_example_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "example"
            result = subprocess.run([sys.executable, str(ROOT / "scripts/build_plan.py"), str(ROOT / "templates/plan.example.json"), "--output", str(output)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(set(json.loads(result.stdout)["files"]), {"plan.json", "report.md", "report.csv", "report.html"})
            self.assertEqual(load_plan(output / "plan.json"), load_plan(ROOT / "templates/plan.example.json"))


if __name__ == "__main__":
    unittest.main()
