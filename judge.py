"""Claude-as-judge: rank candidate solutions against the success criteria.

We feed the judge a compact bundle (problem, criteria, per-candidate diff,
summary metrics) and ask for a structured JSON verdict so we can render it
in the report deterministically.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from runners import RunResult


JUDGE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["winner", "ranking", "rationale"],
    "properties": {
        "winner": {
            "type": "string",
            "description": "Label of the winning candidate (e.g. 'claude/opus'), or 'none' if no candidate satisfied the criteria.",
        },
        "ranking": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["label", "score", "verdict", "notes"],
                "properties": {
                    "label": {"type": "string"},
                    "score": {
                        "type": "integer",
                        "minimum": 0,
                        "maximum": 100,
                        "description": "0-100 quality score for this candidate.",
                    },
                    "verdict": {
                        "type": "string",
                        "enum": ["pass", "partial", "fail", "no_change"],
                    },
                    "notes": {"type": "string"},
                },
            },
        },
        "rationale": {
            "type": "string",
            "description": "Concise summary of the reasoning behind the ranking.",
        },
    },
}


JUDGE_SYSTEM = """\
You are an impartial code review judge. You will be given a software task with \
explicit success criteria and several candidate diffs produced by different AI \
coding agents. For each candidate, decide whether the diff satisfies the \
criteria, then rank candidates from best to worst.

Reward correctness first, simplicity second. Penalise: candidates that produced \
no diff, candidates that broke unrelated code, candidates that solved the wrong \
problem. Output strictly conforms to the supplied JSON schema.\
"""


@dataclass
class Verdict:
    winner: str = "none"
    ranking: list[dict[str, Any]] = field(default_factory=list)
    rationale: str = ""
    raw: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "winner": self.winner,
            "ranking": self.ranking,
            "rationale": self.rationale,
        }


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    keep = limit - 200
    return text[:keep] + f"\n\n... [truncated {len(text) - keep} bytes] ...\n"


def _candidate_block(result: RunResult, diff_limit: int) -> str:
    diff_text = ""
    if result.diff_path:
        try:
            diff_text = Path(result.diff_path).read_text(errors="replace")
        except OSError:
            diff_text = ""
    diff_text = _truncate(diff_text, diff_limit)
    if not diff_text.strip():
        diff_text = "(no diff produced)"

    final = _truncate(result.final_message, 1500)
    return (
        f"### Candidate: {result.label}\n"
        f"- exit_code: {result.exit_code}\n"
        f"- timed_out: {result.timed_out}\n"
        f"- wall_seconds: {result.wall_seconds:.1f}\n"
        f"- files_changed: {result.files_changed} "
        f"(+{result.lines_added} / -{result.lines_deleted})\n"
        f"- error: {result.error or '(none)'}\n"
        f"- final_message: {final or '(empty)'}\n"
        f"\n```diff\n{diff_text}\n```\n"
    )


def judge(
    *,
    problem: str,
    results: list[RunResult],
    judge_model: str = "claude-opus-4-7",
    log_dir: Path,
    diff_budget_bytes: int = 60_000,
    timeout: int = 600,
) -> Verdict:
    """Invoke ``claude -p`` as a structured judge. Returns a parsed Verdict.
    On any failure we fall back to a Verdict whose ``rationale`` describes
    what went wrong, so the report can still render.
    """
    log_dir.mkdir(parents=True, exist_ok=True)
    if shutil.which("claude") is None:
        v = Verdict(rationale="claude CLI not found in PATH; skipping judge step")
        (log_dir / "judge_error.txt").write_text(v.rationale)
        return v
    if not results:
        return Verdict(rationale="no candidates to judge")

    # Split the diff budget evenly across candidates so big diffs from one
    # runner do not crowd out the others.
    per_candidate = max(2_000, diff_budget_bytes // max(1, len(results)))
    candidates_md = "\n".join(_candidate_block(r, per_candidate) for r in results)

    user_prompt = (
        "## Task\n\n"
        f"{problem}\n\n"
        "## Candidates\n\n"
        f"{candidates_md}\n\n"
        "Score every candidate, pick a winner, and emit JSON matching the schema."
    )

    schema_path = log_dir / "judge_schema.json"
    schema_path.write_text(json.dumps(JUDGE_SCHEMA))
    prompt_path = log_dir / "judge_prompt.md"
    prompt_path.write_text(user_prompt)

    cmd = [
        "claude",
        "-p",
        user_prompt,
        "--model",
        judge_model,
        "--output-format",
        "json",
        "--system-prompt",
        JUDGE_SYSTEM,
        "--json-schema",
        json.dumps(JUDGE_SCHEMA),
        "--no-session-persistence",
        "--dangerously-skip-permissions",
    ]

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return Verdict(rationale=f"judge timed out after {timeout}s")

    (log_dir / "judge_stdout.log").write_text(proc.stdout)
    (log_dir / "judge_stderr.log").write_text(proc.stderr)

    if proc.returncode != 0:
        return Verdict(
            rationale=f"judge exited with code {proc.returncode}; see judge_stderr.log",
            raw=proc.stdout,
        )

    try:
        envelope = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        return Verdict(rationale=f"judge stdout was not JSON: {exc}", raw=proc.stdout)

    # When --json-schema is supplied, claude returns the schema-conforming
    # object on the ``structured_output`` field; ``result`` holds the human
    # narrative. Fall back to parsing ``result`` for older claude builds.
    payload: Any = envelope.get("structured_output")
    raw_dump = json.dumps(payload) if payload is not None else (envelope.get("result") or "")
    if not isinstance(payload, dict):
        result_str = envelope.get("result")
        if isinstance(result_str, str):
            try:
                payload = json.loads(result_str)
            except json.JSONDecodeError as exc:
                return Verdict(
                    rationale=f"judge response was neither structured_output nor JSON in result: {exc}",
                    raw=result_str,
                )
        else:
            return Verdict(
                rationale="judge envelope missing structured_output and result",
                raw=proc.stdout,
            )

    v = Verdict(
        winner=str(payload.get("winner", "none")),
        ranking=list(payload.get("ranking") or []),
        rationale=str(payload.get("rationale", "")),
        raw=raw_dump,
    )
    (log_dir / "verdict.json").write_text(json.dumps(v.to_dict(), indent=2))
    return v
