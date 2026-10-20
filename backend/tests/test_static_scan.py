import sys
import tempfile
import unittest
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from static_scan import (  # noqa: E402
    EVIDENCE_LIMIT,
    FINDING_FIELDS,
    SEVERITIES,
    normalize_relative_path,
    scan_files,
    scan_text,
)


class ScanTextTests(unittest.TestCase):
    def test_safe_python_code_has_no_findings(self):
        text = 'def greet(name):\n    print("hello", name)\n'
        self.assertEqual(scan_text("src/app.py", text), [])

    def test_safe_javascript_code_has_no_findings(self):
        text = "const total = items.length\nconsole.log(total)\n"
        self.assertEqual(scan_text("src/app.ts", text), [])

    def test_detects_eval_and_exec_with_line_numbers(self):
        text = 'value = eval(user_input)\nexec(payload)\n'
        findings = scan_text("src/runner.py", text)

        self.assertEqual([finding.rule_id for finding in findings], ["PY-EVAL-EXEC", "PY-EVAL-EXEC"])
        self.assertEqual([finding.line for finding in findings], [1, 2])
        self.assertTrue(all(finding.severity == "high" for finding in findings))

    def test_detects_os_system(self):
        findings = scan_text("src/runner.py", "os.system(command)\n")

        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].rule_id, "PY-OS-SYSTEM")
        self.assertEqual(findings[0].line, 1)

    def test_detects_shell_true(self):
        findings = scan_text("src/runner.py", "subprocess.run(cmd, shell=True)\n")

        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].rule_id, "PY-SHELL-TRUE")

    def test_detects_javascript_eval(self):
        findings = scan_text("src/app.js", "const result = eval(input)\n")

        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].rule_id, "JS-EVAL")

    def test_detects_dangerous_html_in_tsx(self):
        findings = scan_text(
            "src/View.tsx",
            "return <div dangerouslySetInnerHTML={{ __html: html }} />\n",
        )

        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].rule_id, "JS-DANGEROUS-HTML")

    def test_detects_inner_html_assignment(self):
        findings = scan_text("src/view.js", "element.innerHTML = userInput\n")

        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].rule_id, "JS-INNER-HTML")

    def test_unsupported_extension_is_ignored(self):
        warnings = ["eval(user_input)", "os.system(command)"]
        self.assertEqual(scan_text("docs/notes.md", "\n".join(warnings)), [])

    def test_comment_lines_are_skipped(self):
        text = '# eval(user_input)\n// eval(user_input)\n* exec(payload)\n'
        self.assertEqual(scan_text("src/app.py", text), [])

    def test_blank_lines_are_skipped(self):
        self.assertEqual(scan_text("src/app.py", "\n\n   \n"), [])

    def test_duplicate_rule_on_same_line_is_reported_once(self):
        findings = scan_text("src/app.py", "eval(a) + eval(b)\n")

        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].rule_id, "PY-EVAL-EXEC")

    def test_findings_are_sorted_by_line_then_rule(self):
        text = "os.system(cmd)\nvalue = eval(data)\n"
        findings = scan_text("src/app.py", text)

        self.assertEqual(
            [(finding.line, finding.rule_id) for finding in findings],
            [(1, "PY-OS-SYSTEM"), (2, "PY-EVAL-EXEC")],
        )

    def test_evidence_is_trimmed_and_truncated(self):
        line = 'eval("' + "a" * (EVIDENCE_LIMIT * 2) + '")\n'
        findings = scan_text("src/app.py", line)

        self.assertEqual(len(findings), 1)
        self.assertEqual(len(findings[0].evidence), EVIDENCE_LIMIT)
        self.assertTrue(findings[0].evidence.endswith("..."))
        self.assertFalse(findings[0].evidence.endswith("\n"))

    def test_to_dict_has_exactly_the_unified_fields(self):
        finding = scan_text("src/app.py", "eval(user_input)\n")[0]
        payload = finding.to_dict()

        self.assertEqual(tuple(payload.keys()), FINDING_FIELDS)
        self.assertEqual(payload["file_path"], "src/app.py")
        self.assertEqual(payload["line"], 1)
        self.assertIn(payload["severity"], SEVERITIES)
        self.assertIsInstance(payload["evidence"], str)
        self.assertIsInstance(payload["explanation"], str)
        self.assertIsInstance(payload["recommendation"], str)

    def test_windows_paths_are_normalized_in_findings(self):
        findings = scan_text(".\\src\\app.py", "eval(user_input)\n")

        self.assertEqual(findings[0].file_path, "src/app.py")

    def test_scan_does_not_execute_scanned_text(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            sentinel = Path(temp_dir) / "sentinel.txt"
            payload = f'exec("open({str(sentinel)!r}, \'w\').write(\'ran\')")\n'

            findings = scan_text("src/app.py", payload)

            self.assertEqual(len(findings), 1)
            self.assertFalse(sentinel.exists())

    def test_rejects_non_string_text(self):
        with self.assertRaises(TypeError):
            scan_text("src/app.py", b"eval(user_input)")


class NormalizePathTests(unittest.TestCase):
    def test_normalizes_backslashes_and_current_directory_prefix(self):
        self.assertEqual(normalize_relative_path("./src\\nested\\app.ts"), "src/nested/app.ts")

    def test_collapses_repeated_separators(self):
        self.assertEqual(normalize_relative_path("src//nested///app.ts"), "src/nested/app.ts")

    def test_rejects_empty_path(self):
        with self.assertRaises(ValueError):
            normalize_relative_path("   ")

    def test_rejects_absolute_paths(self):
        with self.assertRaises(ValueError):
            normalize_relative_path("/etc/passwd")
        with self.assertRaises(ValueError):
            normalize_relative_path("C:\\Users\\example\\app.py")

    def test_rejects_parent_directory_segments(self):
        with self.assertRaises(ValueError):
            normalize_relative_path("src/../../outside.py")

    def test_rejects_nul_characters(self):
        with self.assertRaises(ValueError):
            normalize_relative_path("src/app\x00.py")

    def test_rejects_non_string_path(self):
        with self.assertRaises(TypeError):
            normalize_relative_path(123)  # type: ignore[arg-type]


class ScanFilesTests(unittest.TestCase):
    def test_aggregates_findings_in_input_order(self):
        findings = scan_files(
            [
                ("src/first.py", "os.system(cmd)\n"),
                ("src/second.ts", "eval(input)\n"),
            ]
        )

        self.assertEqual(
            [finding.rule_id for finding in findings],
            ["PY-OS-SYSTEM", "JS-EVAL"],
        )

    def test_empty_input_returns_empty_list(self):
        self.assertEqual(scan_files([]), [])


if __name__ == "__main__":
    unittest.main()
