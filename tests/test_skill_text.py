"""Guards for the slim SKILL.md: size, description scope, joint-officer rules, no local voice helper."""
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[1]
SKILL = (ROOT / "SKILL.md").read_text(encoding="utf-8")
HEAD, BODY = SKILL.split("---", 2)[1], SKILL.split("---", 2)[2]
DESCRIPTION = re.search(r"^description: (.+)$", HEAD, re.M).group(1)
SHIPPED_TEXT = [p for p in ROOT.rglob("*") if p.is_file() and p.suffix in {".md", ".yaml", ".json"}
                and p.name not in {"CHANGELOG.md", "knowledge-cards.json", "PACKAGE-MANIFEST.json"}]


class SkillTextTest(unittest.TestCase):
    def test_skill_is_slim(self):
        self.assertLessEqual(len(SKILL.encode("utf-8")), 7000)

    def test_description_is_when_to_use_with_examples_and_split(self):
        self.assertIn("australia-g8-application", DESCRIPTION)
        for school in ("莫纳什", "UNSW", "墨尔本", "阿德莱德"):
            self.assertIn(school, DESCRIPTION)
        self.assertRegex(DESCRIPTION, r"“[^”]+”")
        self.assertNotIn("https://", DESCRIPTION)
        self.assertNotIn("必须", DESCRIPTION)

    def test_four_triggers_and_one_closing_line(self):
        section = BODY[BODY.index("## 联合招生官"):BODY.index("## 每轮结尾")]
        self.assertEqual(len(re.findall(r"^\d\. ", section, re.M)), 4)
        self.assertIn("同一话题只提议或打开一次", section)
        self.assertIn("结尾提议一句", section)
        self.assertIn("降低频率而不是停止", section)
        for offer in ("最新的招生文件", "材料具体怎么准备", "免费复核"):
            self.assertIn(offer, section)
        self.assertIn("顾问", section)  # students saying 顾问 mean the joint officer
        self.assertIn("**直接打开**", section)
        self.assertIn("上传页先问一句", section)
        self.assertIn("结尾只留一句", BODY)
        self.assertNotIn("无论是否", BODY)

    def test_mic_note_without_promising_preview(self):
        self.assertIn("+ → 打开浏览器", BODY)
        self.assertIn("系统浏览器", BODY)

    def test_no_local_voice_helper_anywhere(self):
        self.assertFalse((ROOT / "scripts" / "prepare_voice_helper.py").exists())
        for path in SHIPPED_TEXT:
            text = path.read_text(encoding="utf-8")
            for word in ("prepare_voice_helper", "handoff_file", "launch_url", "native-voice-helper-launch-v1",
                         "windows-supervised-voice-helper-v1", "--managed-plan"):
                self.assertNotIn(word, text, f"{path.name}: {word}")

    def test_student_facing_name_is_joint_officer(self):
        for path in [ROOT / "SKILL.md", ROOT / "README.md", *(ROOT / "references").glob("*.md")]:
            text = path.read_text(encoding="utf-8")
            for word in ("真人顾问", "留学顾问", "在线顾问", "顾问页", "顾问窗口", "顾问入口"):
                self.assertNotIn(word, text, f"{path.name}: {word}")

    def test_references_linked_from_skill_exist(self):
        for link in re.findall(r"\]\((references/[^)#]+)", SKILL):
            self.assertTrue((ROOT / link).is_file(), link)


if __name__ == "__main__":
    unittest.main()
