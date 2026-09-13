"""Retained generic evidence regressions; native backends are the only proof."""

from pathlib import Path
import tempfile
import unittest

from scripts.common import AppError
from scripts import verify_native as evidence


class EvidenceUtilityTests(unittest.TestCase):
    def test_supported_cli_versions_normalize_without_guessing(self):
        for value in ("1.0.84-5", "v1.0.84-5", "GitHub Copilot CLI 1.0.84-5."):
            self.assertEqual(evidence._cli_version(value), "1.0.84-5")
        for value in (None, "", "unknown", "github 1.0.84", "1.0.84\nmalformed"):
            with self.subTest(value=value), self.assertRaises(AppError):
                evidence._cli_version(value)

    def test_evidence_timestamps_require_explicit_utc(self):
        for value in ("2026-09-13T12:00:00Z", "2026-09-13T12:00:00+00:00"):
            self.assertEqual(evidence._utc(value).utcoffset().total_seconds(), 0)
        for value in (None, "", "2026-09-13T12:00:00", "2026-09-13T12:00:00+01:00"):
            with self.subTest(value=value), self.assertRaises(AppError):
                evidence._utc(value)

    def test_private_paths_cannot_escape_or_follow_links(self):
        with tempfile.TemporaryDirectory() as directory:
            local = Path(directory)
            safe = local / "runs" / "result.json"
            self.assertEqual(evidence.trusted_path(str(safe), local), safe)
            (local / "link").symlink_to(local / "target")
            for value in (str(local), "relative", str(local / ".." / "escape"),
                          str(local / "link" / "result.json")):
                with self.subTest(value=value), self.assertRaises(AppError):
                    evidence.trusted_path(value, local)

    def test_content_detection_handles_native_json_and_nested_dotted_attributes(self):
        self.assertEqual(evidence._content({
            "gen_ai": {"input": {"messages": '[{"content":"synthetic"}]'}},
            "gen_ai.tool.call.arguments.foo": "bar", "ordinary": "metadata",
        }), {
            "gen_ai.input.messages": [{"content": "synthetic"}],
            "gen_ai.tool.call.arguments.foo": "bar",
        })

    def test_retired_classic_query_and_runner_are_absent(self):
        root = Path(__file__).resolve().parent.parent
        self.assertFalse((root / "scripts" / "verify_ingestion.py").exists())
        self.assertFalse((root / "queries" / "verify_ingestion.kql").exists())


if __name__ == "__main__":
    unittest.main()
