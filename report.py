"""Markdown report writer."""
from __future__ import annotations

from pathlib import Path
from typing import Iterable

from judge import Verdict
from runners import RunResult


def _fmt_cost(cost: float | None) -> str:
    if cost is None:
        return "—"
    return f"${cost:.4f}"


def _fmt_int(n: int) -> str:
    return f"{n:,}" if n else "—"


def _fmt_secs(s: float) -> str:
    if s <= 0:
        return "—"
    if s < 90:
        return f"{s:.1f}s"
    m, sec = divmod(int(s), 60)
    return f"{m}m{sec:02d}s"


def _verdict_for(label: str, verdict: Verdict) -> tuple[str, str]:
    """Return (verdict_str, score_str) for a candidate label."""
    for entry in verdict.ranking:
        if entry.get("label") == label:
            score = entry.get("score")
            return (
                str(entry.get("verdict", "?")),
                str(score) if score is not None else "—",
            )
    return ("—", "—")


def _efficiency(result: RunResult) -> str:
    """Tokens-per-line-changed as a crude efficiency signal."""
    changes = result.lines_added + result.lines_deleted
    if changes == 0:
        return "—"
    total = result.input_tokens + result.output_tokens
    return f"{total / changes:.0f} tok/line"


def write_report(
    *,
    run_dir: Path,
    problem: str,
    results: list[RunResult],
    verdict: Verdict,
) -> Path:
    lines: list[str] = []
    lines.append("# kratotatos run report")
    lines.append("")
    lines.append(f"- Run dir: `{run_dir}`")
    lines.append(f"- Candidates: {len(results)}")
    lines.append(f"- Winner (judge): **{verdict.winner}**")
    lines.append("")
    lines.append("## Problem")
    lines.append("")
    lines.append("```")
    lines.append(problem.rstrip())
    lines.append("```")
    lines.append("")

    lines.append("## Metrics")
    lines.append("")
    header = (
        "| Candidate | Verdict | Score | Wall | Files | +/− | "
        "Input | Output | Cache | Reasoning | Cost | Efficiency | Turns |"
    )
    sep = "|" + "|".join(["---"] * 13) + "|"
    lines.append(header)
    lines.append(sep)

    # Sort: judge ranking order if available, else by label.
    if verdict.ranking:
        ranked_labels = [
            r.get("label", "")
            for r in sorted(
                verdict.ranking,
                key=lambda x: -(x.get("score") or 0),
            )
        ]
        ordered: list[RunResult] = []
        seen: set[str] = set()
        for lbl in ranked_labels:
            for r in results:
                if r.label == lbl and r.label not in seen:
                    ordered.append(r)
                    seen.add(r.label)
        for r in results:
            if r.label not in seen:
                ordered.append(r)
    else:
        ordered = sorted(results, key=lambda r: r.label)

    for r in ordered:
        v, score = _verdict_for(r.label, verdict)
        change_str = f"+{r.lines_added}/-{r.lines_deleted}"
        lines.append(
            "| "
            + " | ".join(
                [
                    f"`{r.label}`",
                    v,
                    score,
                    _fmt_secs(r.wall_seconds),
                    str(r.files_changed),
                    change_str,
                    _fmt_int(r.input_tokens),
                    _fmt_int(r.output_tokens),
                    _fmt_int(r.cache_read_tokens),
                    _fmt_int(r.reasoning_tokens),
                    _fmt_cost(r.cost_usd),
                    _efficiency(r),
                    str(r.num_turns),
                ]
            )
            + " |"
        )

    lines.append("")
    lines.append("## Judge rationale")
    lines.append("")
    lines.append(verdict.rationale or "_(empty)_")
    lines.append("")

    if verdict.ranking:
        lines.append("### Per-candidate notes")
        lines.append("")
        for entry in sorted(
            verdict.ranking, key=lambda x: -(x.get("score") or 0)
        ):
            lines.append(
                f"- **{entry.get('label')}** "
                f"({entry.get('verdict')}, score {entry.get('score')}): "
                f"{entry.get('notes', '')}"
            )
        lines.append("")

    lines.append("## Per-candidate logs")
    lines.append("")
    for r in ordered:
        lines.append(f"### `{r.label}`")
        lines.append("")
        lines.append(f"- workdir: `{r.workdir}`")
        lines.append(f"- log dir: `{r.log_dir}`")
        lines.append(f"- diff: `{r.diff_path or '—'}`")
        lines.append(f"- exit code: {r.exit_code}")
        if r.timed_out:
            lines.append("- **timed out**")
        if r.error:
            lines.append(f"- error: `{r.error}`")
        if r.final_message:
            snippet = r.final_message.strip().splitlines()[:8]
            lines.append("- final message (first lines):")
            lines.append("")
            lines.append("  ```")
            for s in snippet:
                lines.append(f"  {s}")
            lines.append("  ```")
        lines.append("")

    out = run_dir / "report.md"
    out.write_text("\n".join(lines))
    return out
