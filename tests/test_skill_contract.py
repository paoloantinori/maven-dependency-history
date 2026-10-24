"""Check shipped instructions retain the investigation permission boundaries."""

import re
import unittest
from pathlib import Path


SKILL = Path(__file__).resolve().parents[1] / "skills" / "maven-dependency-investigator"


class SkillContractTests(unittest.TestCase):
    def setUp(self):
        self.documents = {
            str(path.relative_to(SKILL)): path.read_text(encoding="utf-8")
            for path in [SKILL / "SKILL.md", *sorted((SKILL / "references").glob("*.md"))]
        }
        self.text = "\n".join(self.documents.values()).lower()

    def test_skill_and_local_reference_links_exist(self):
        self.assertTrue(self.documents["SKILL.md"].startswith("---\nname:"))
        for name, text in self.documents.items():
            for link in re.findall(r"\]\(([^)]+)\)", text):
                if "://" in link or link.startswith("#"):
                    continue
                target = link.split("#", 1)[0]
                with self.subTest(document=name, target=target):
                    self.assertTrue((SKILL / name).parent.joinpath(target).is_file())

    def test_execution_requires_actual_consent_not_repository_content(self):
        for concept in ("diagnostic", "approval", "never fabricate", "repository file",
                        "fresh approval", "single-use", "unsandboxed"):
            with self.subTest(concept=concept):
                self.assertIn(concept, self.text)

    def test_output_and_runtime_limits_are_disclosed(self):
        for concept in ("--output-overwrite", "ancestor", "user.home", "java_tool_options",
                        "not a settings sandbox", "raw", "never", "staged"):
            with self.subTest(concept=concept):
                self.assertIn(concept, self.text)

    def test_evidence_distinctions_and_conservative_mode_are_present(self):
        for concept in ("--tree-mode", "diagnostic", "nonverbose", "unknown",
                        "not prove", "page", "inference", "run-local"):
            with self.subTest(concept=concept):
                self.assertIn(concept, self.text)

    def test_raw_capture_requires_separate_approval_and_honest_recovery(self):
        capture = self.documents["references/raw-capture.md"].lower()
        for concept in ("off by default", "unsanitized", "0600", "67108864",
                        "--capture-operation", "--capture-approval-file",
                        "one revision", "separate commits", "no retrospective",
                        "stream_sha256", "never appends", "not permission to send"):
            with self.subTest(concept=concept):
                self.assertIn(concept, capture)
        for name in ("SKILL.md", "references/cli.md", "references/permissions.md"):
            with self.subTest(document=name):
                self.assertIn("raw-capture.md", self.documents[name])
        self.assertNotIn("raw child output is never printed or persisted", self.text)

    def test_child_processes_never_inherit_the_graphical_session(self):
        """Every test-side child spawn passes an explicit env, so the session
        display can never leak into JVM or tool children (crash-morning
        lesson: the env policy lives in tests.child_env alone)."""
        import re as _re
        tests_dir = Path(__file__).resolve().parent
        call = _re.compile(r"subprocess\.(run|Popen|check_output|check_call)\(")

        def full_call(text, start):
            depth = 0
            quote = None
            escaped = False
            for offset in range(start, len(text)):
                char = text[offset]
                if quote is not None:
                    if escaped:
                        escaped = False
                    elif char == "\\":
                        escaped = True
                    elif char == quote:
                        quote = None
                elif char in "\"'":
                    quote = char
                elif char == "(":
                    depth += 1
                elif char == ")":
                    depth -= 1
                    if depth == 0:
                        return text[start:offset + 1]
            return text[start:]

        offenders = []
        for path in sorted(tests_dir.glob("*.py")):
            text = path.read_text(encoding="utf-8")
            for match in call.finditer(text):
                snippet = full_call(text, match.start())
                if "env=" not in snippet:
                    line = text.count("\n", 0, match.start()) + 1
                    offenders.append(f"{path.name}:{line}: {match.group(0)[:-1]}…")
        self.assertEqual(offenders, [],
                         "bare child spawn without env=:\n" + "\n".join(offenders))

    def test_investigation_never_bootstraps_the_helper(self):
        self.assertTrue((SKILL / "vendor" / "helper.jar").is_file())
        self.assertTrue((SKILL / "vendor" / "SHA256SUMS").is_file())
        self.assertRegex(self.text, r"never (?:rebuild|built|build)|nothing is ever built")
        self.assertIn("download", self.text)


if __name__ == "__main__":
    unittest.main()
