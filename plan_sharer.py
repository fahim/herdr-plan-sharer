#!/usr/bin/env python3
"""Herdr plugin for copying and exporting plans from Codex and Claude."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable, Mapping, Sequence


PLUGIN_NAME = "Plan Sharer"
MAX_CLAUDE_PLAN_BYTES = 2 * 1024 * 1024
CODEX_PLAN_RE = re.compile(
    r"<proposed_plan>\s*(.*?)\s*</proposed_plan>", re.DOTALL | re.IGNORECASE
)
CLAUDE_PLAN_REF_RE = re.compile(
    r"Plan file referenced\s*(?:\n\s*)?\(?\s*((?:~|/)[^\n)]*?\.md)\s*\)?",
    re.IGNORECASE,
)


class PlanSharerError(RuntimeError):
    """An actionable error that should be shown to the user."""


@dataclass(frozen=True)
class CapturedPlan:
    text: str
    agent: str
    source: str


def run_command(
    argv: Sequence[str],
    *,
    input_text: str | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            list(argv),
            input=input_text,
            text=True,
            capture_output=True,
            check=check,
            timeout=15,
        )
    except FileNotFoundError as exc:
        raise PlanSharerError(f"Required command not found: {argv[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise PlanSharerError(f"Command timed out: {argv[0]}") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "").strip()
        suffix = f": {detail}" if detail else ""
        raise PlanSharerError(f"Command failed: {argv[0]}{suffix}") from exc


def herdr_bin(env: Mapping[str, str]) -> str:
    return env.get("HERDR_BIN_PATH", "herdr")


def notify(env: Mapping[str, str], title: str, body: str) -> None:
    try:
        run_command(
            [
                herdr_bin(env),
                "notification",
                "show",
                title,
                "--body",
                body,
                "--sound",
                "none",
            ],
            check=False,
        )
    except PlanSharerError:
        pass


def load_context(env: Mapping[str, str]) -> dict:
    raw = env.get("HERDR_PLUGIN_CONTEXT_JSON", "{}")
    try:
        context = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise PlanSharerError("Herdr supplied invalid plugin context") from exc
    if not isinstance(context, dict):
        raise PlanSharerError("Herdr plugin context is not an object")
    return context


def clean_plan(text: str) -> str:
    return text.strip()


def selected_plan(context: Mapping[str, object]) -> CapturedPlan | None:
    selected = context.get("selected_text")
    if not isinstance(selected, str) or not clean_plan(selected):
        return None
    agent = str(context.get("focused_pane_agent") or "selection")
    return CapturedPlan(clean_plan(selected), agent, "selection")


def pane_process_info(
    env: Mapping[str, str], pane_id: str
) -> tuple[list[int], str | None]:
    result = run_command(
        [herdr_bin(env), "pane", "process-info", "--pane", pane_id]
    )
    try:
        payload = json.loads(result.stdout)["result"]["process_info"]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise PlanSharerError("Could not read the focused pane process") from exc

    processes = payload.get("foreground_processes") or []
    codex_pids: list[int] = []
    other_pids: list[int] = []
    cwd = None
    for process in processes:
        if not isinstance(process, dict):
            continue
        pid = process.get("pid")
        if not isinstance(pid, int):
            continue
        process_text = " ".join(
            str(process.get(key) or "") for key in ("name", "argv0", "cmdline")
        ).lower()
        (codex_pids if "codex" in process_text else other_pids).append(pid)
        if cwd is None and isinstance(process.get("cwd"), str):
            cwd = process["cwd"]
    pids = codex_pids or other_pids
    if not pids:
        raise PlanSharerError("No foreground agent process was found")
    return pids, cwd


def codex_session_paths_for_pid(pid: int) -> list[Path]:
    result = run_command(["lsof", "-Fn", "-p", str(pid)])
    paths: list[Path] = []
    for line in result.stdout.splitlines():
        if not line.startswith("n"):
            continue
        value = line[1:]
        if "/.codex/sessions/" in value and value.endswith(".jsonl"):
            path = Path(value)
            if path not in paths:
                paths.append(path)
    return paths


def read_session_meta(path: Path) -> dict | None:
    try:
        with path.open(encoding="utf-8") as handle:
            first = json.loads(handle.readline())
    except (OSError, json.JSONDecodeError):
        return None
    if first.get("type") != "session_meta":
        return None
    payload = first.get("payload")
    return payload if isinstance(payload, dict) else None


def choose_codex_session(paths: Iterable[Path], cwd: str | None) -> Path:
    roots: list[tuple[Path, dict]] = []
    for path in paths:
        meta = read_session_meta(path)
        if meta and meta.get("source") == "cli":
            roots.append((path, meta))
    if not roots:
        raise PlanSharerError("Could not identify the focused Codex session")

    if cwd:
        matching = [(path, meta) for path, meta in roots if meta.get("cwd") == cwd]
        if matching:
            roots = matching
    return max(roots, key=lambda item: item[0].stat().st_mtime)[0]


def extract_codex_plan(path: Path) -> str:
    latest: str | None = None
    try:
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                payload = item.get("payload")
                if (
                    item.get("type") != "response_item"
                    or not isinstance(payload, dict)
                    or payload.get("type") != "message"
                    or payload.get("role") != "assistant"
                ):
                    continue
                content = payload.get("content") or []
                text = "".join(
                    part.get("text", "")
                    for part in content
                    if isinstance(part, dict)
                    and part.get("type") in {"output_text", "text"}
                )
                for match in CODEX_PLAN_RE.finditer(text):
                    candidate = clean_plan(match.group(1))
                    if candidate:
                        latest = candidate
    except OSError as exc:
        raise PlanSharerError("Could not read the focused Codex transcript") from exc
    if latest is None:
        raise PlanSharerError("No completed Codex plan was found in this session")
    return latest


def capture_codex(
    env: Mapping[str, str], context: Mapping[str, object]
) -> CapturedPlan:
    pane_id = context.get("focused_pane_id") or env.get("HERDR_PANE_ID")
    if not isinstance(pane_id, str) or not pane_id:
        raise PlanSharerError("The focused Herdr pane is unavailable")
    pids, process_cwd = pane_process_info(env, pane_id)
    paths: list[Path] = []
    for pid in pids:
        paths.extend(codex_session_paths_for_pid(pid))
    context_cwd = context.get("focused_pane_cwd")
    cwd = process_cwd or (context_cwd if isinstance(context_cwd, str) else None)
    session = choose_codex_session(paths, cwd)
    return CapturedPlan(extract_codex_plan(session), "codex", str(session))


def extract_claude_plan_reference(text: str) -> str:
    matches = list(CLAUDE_PLAN_REF_RE.finditer(text))
    if not matches:
        raise PlanSharerError("No Claude plan reference was found in this pane")
    return matches[-1].group(1).strip()


def validated_claude_plan_path(raw: str, home: Path) -> Path:
    if raw.startswith("~/"):
        candidate = home / raw[2:]
    else:
        candidate = Path(raw)
    try:
        resolved = candidate.resolve(strict=True)
        root = (home / ".claude" / "plans").resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, ValueError) as exc:
        raise PlanSharerError("Claude plan path is missing or outside ~/.claude/plans") from exc
    if not resolved.is_file() or resolved.suffix.lower() != ".md":
        raise PlanSharerError("Claude plan is not a Markdown file")
    if resolved.stat().st_size > MAX_CLAUDE_PLAN_BYTES:
        raise PlanSharerError("Claude plan is unexpectedly large")
    return resolved


def capture_claude(
    env: Mapping[str, str], context: Mapping[str, object]
) -> CapturedPlan:
    pane_id = context.get("focused_pane_id") or env.get("HERDR_PANE_ID")
    if not isinstance(pane_id, str) or not pane_id:
        raise PlanSharerError("The focused Herdr pane is unavailable")
    result = run_command(
        [
            herdr_bin(env),
            "pane",
            "read",
            pane_id,
            "--source",
            "recent-unwrapped",
            "--lines",
            "500",
        ]
    )
    raw_path = extract_claude_plan_reference(result.stdout)
    path = validated_claude_plan_path(raw_path, Path.home())
    try:
        text = clean_plan(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise PlanSharerError("Could not read the Claude plan file") from exc
    if not text:
        raise PlanSharerError("The Claude plan file is empty")
    return CapturedPlan(text, "claude", str(path))


def capture_plan(env: Mapping[str, str]) -> CapturedPlan:
    context = load_context(env)
    selected = selected_plan(context)
    if selected:
        return selected
    agent = str(context.get("focused_pane_agent") or "").lower()
    if agent == "codex":
        return capture_codex(env, context)
    if agent == "claude":
        return capture_claude(env, context)
    if not agent:
        raise PlanSharerError("No recognized agent is focused; select plan text first")
    raise PlanSharerError(f"Automatic plan capture does not support agent: {agent}")


def copy_to_clipboard(text: str) -> None:
    run_command(["pbcopy"], input_text=text)


def plan_slug(text: str, agent: str) -> str:
    match = re.search(r"(?m)^#\s+(.+?)\s*$", text)
    title = match.group(1) if match else f"{agent}-plan"
    ascii_title = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_title.lower()).strip("-")
    return (slug or f"{agent}-plan")[:60].rstrip("-")


def write_export(
    plan: CapturedPlan,
    directory: Path,
    *,
    now: datetime | None = None,
) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    timestamp = (now or datetime.now()).strftime("%Y-%m-%d-%H%M%S")
    stem = f"{plan_slug(plan.text, plan.agent)}-{timestamp}"
    target = directory / f"{stem}.md"
    suffix = 2
    while target.exists():
        target = directory / f"{stem}-{suffix}.md"
        suffix += 1

    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=directory,
            prefix=".plan-sharer-",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(plan.text)
        temporary.chmod(0o600)
        os.replace(temporary, target)
    except OSError as exc:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        raise PlanSharerError(f"Could not export the plan to {directory}") from exc
    return target


def export_directory(env: Mapping[str, str]) -> Path:
    override = env.get("HERDR_PLAN_SHARER_EXPORT_DIR")
    return Path(override).expanduser() if override else Path.home() / "Documents" / "Herdr Plans"


def copy_action(env: Mapping[str, str]) -> None:
    plan = capture_plan(env)
    copy_to_clipboard(plan.text)
    notify(env, "Plan copied", f"Copied the latest {plan.agent} plan")


def export_action(env: Mapping[str, str]) -> None:
    plan = capture_plan(env)
    path = write_export(plan, export_directory(env))
    warnings: list[str] = []
    try:
        copy_to_clipboard(str(path))
    except PlanSharerError:
        warnings.append("path was not copied")
    result = run_command(["open", "-R", str(path)], check=False)
    if result.returncode != 0:
        warnings.append("Finder did not open")
    body = f"Exported {path.name}; path copied"
    if warnings:
        body = f"Exported {path.name}; " + ", ".join(warnings)
    notify(env, "Plan exported", body)
    print(path)


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("copy", "export"))
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None, env: Mapping[str, str] | None = None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])
    environment = env if env is not None else os.environ
    try:
        if args.action == "copy":
            copy_action(environment)
        else:
            export_action(environment)
        return 0
    except PlanSharerError as exc:
        notify(environment, "Plan not captured", str(exc))
        print(f"{PLUGIN_NAME}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
