import json
import stat
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from plan_sharer import (  # noqa: E402
    CapturedPlan,
    PlanSharerError,
    choose_codex_session,
    extract_claude_plan_reference,
    extract_codex_plan,
    plan_slug,
    selected_plan,
    validated_claude_plan_path,
    write_export,
)


def jsonl(path: Path, records: list[dict]) -> None:
    path.write_text("".join(json.dumps(record) + "\n" for record in records))


def session_meta(cwd: str, source="cli") -> dict:
    return {
        "type": "session_meta",
        "payload": {"id": "session", "cwd": cwd, "source": source},
    }


def assistant(text: str, phase="final_answer") -> dict:
    return {
        "type": "response_item",
        "payload": {
            "type": "message",
            "role": "assistant",
            "phase": phase,
            "content": [{"type": "output_text", "text": text}],
        },
    }


class SelectionTests(unittest.TestCase):
    def test_nonempty_selection_wins_and_is_trimmed(self):
        captured = selected_plan(
            {"selected_text": "\n# Exact selection\n", "focused_pane_agent": "codex"}
        )
        self.assertEqual(captured.text, "# Exact selection")
        self.assertEqual(captured.source, "selection")

    def test_blank_selection_is_ignored(self):
        self.assertIsNone(selected_plan({"selected_text": "  \n"}))


class CodexTests(unittest.TestCase):
    def test_extracts_latest_tagged_plan_not_later_answer(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "session.jsonl"
            jsonl(
                path,
                [
                    session_meta(temp),
                    assistant("<proposed_plan>\n# First\n</proposed_plan>"),
                    assistant("comment", "commentary"),
                    assistant("<proposed_plan>\n# Second\n</proposed_plan>"),
                    assistant("A later non-plan answer"),
                ],
            )
            self.assertEqual(extract_codex_plan(path), "# Second")

    def test_ignores_malformed_json_lines(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "session.jsonl"
            path.write_text(
                json.dumps(session_meta(temp))
                + "\nnot-json\n"
                + json.dumps(assistant("<proposed_plan># Valid</proposed_plan>"))
                + "\n"
            )
            self.assertEqual(extract_codex_plan(path), "# Valid")

    def test_missing_plan_is_an_error(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "session.jsonl"
            jsonl(path, [session_meta(temp), assistant("ordinary answer")])
            with self.assertRaises(PlanSharerError):
                extract_codex_plan(path)

    def test_chooses_root_cli_session_and_matching_cwd(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            wrong = root / "wrong.jsonl"
            right = root / "right.jsonl"
            subagent = root / "subagent.jsonl"
            jsonl(wrong, [session_meta("/other")])
            jsonl(right, [session_meta("/wanted")])
            jsonl(subagent, [session_meta("/wanted", {"subagent": "x"})])
            self.assertEqual(
                choose_codex_session([wrong, subagent, right], "/wanted"), right
            )


class ClaudeTests(unittest.TestCase):
    def test_extracts_last_reference_across_lines(self):
        text = """
        Plan file referenced (~/.claude/plans/old.md)
        other output
        Plan file referenced
           (~/.claude/plans/current-plan.md)
        """
        self.assertEqual(
            extract_claude_plan_reference(text),
            "~/.claude/plans/current-plan.md",
        )

    def test_validates_path_inside_claude_plans(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            plans = home / ".claude" / "plans"
            plans.mkdir(parents=True)
            plan = plans / "safe.md"
            plan.write_text("# Safe")
            self.assertEqual(
                validated_claude_plan_path("~/.claude/plans/safe.md", home),
                plan.resolve(),
            )

    def test_rejects_path_outside_claude_plans(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            (home / ".claude" / "plans").mkdir(parents=True)
            outside = home / "outside.md"
            outside.write_text("# Not safe")
            with self.assertRaises(PlanSharerError):
                validated_claude_plan_path(str(outside), home)


class ExportTests(unittest.TestCase):
    def test_slug_uses_first_h1(self):
        self.assertEqual(plan_slug("# A Plan: With Punctuation!", "codex"), "a-plan-with-punctuation")

    def test_export_is_private_and_collision_safe(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            plan = CapturedPlan("# Useful Plan\n\nBody", "codex", "fixture")
            now = datetime(2026, 10, 30, 19, 30, 0)
            first = write_export(plan, directory, now=now)
            second = write_export(plan, directory, now=now)
            self.assertEqual(first.name, "useful-plan-2026-10-30-193000.md")
            self.assertEqual(second.name, "useful-plan-2026-10-30-193000-2.md")
            self.assertEqual(first.read_text(), plan.text)
            self.assertEqual(stat.S_IMODE(first.stat().st_mode), 0o600)


if __name__ == "__main__":
    unittest.main()
