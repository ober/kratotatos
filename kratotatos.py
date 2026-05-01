#!/usr/bin/env python3
"""kratotatos — multi-model coding-agent harness.

Given a problem description and a local source repository, kratotatos
spawns one isolated copy of the repo per (provider, model) pair, runs each
agent CLI in batch mode against that copy, captures every change as a
git diff, gathers token / time / cost metrics, then asks claude to judge
which solution best meets the success criteria.

Usage examples:

    # Interactive (TUI picker for models):
    ./kratotatos.py --problem problem.md --repo ../my-project

    # Non-interactive (explicit selections):
    ./kratotatos.py \\
        --problem problem.md --repo ../my-project \\
        --models claude:claude-sonnet-4-6 codex:gpt-5 \\
        --timeout 1800

The runs/ directory will hold one timestamped subdirectory per invocation
with isolated workspaces, raw logs, diffs, metrics, and a final report.md.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from judge import judge as run_judge  # noqa: E402
from orchestrator import (  # noqa: E402
    Selection,
    make_run_dir,
    run_all,
    slugify,
)
from report import write_report  # noqa: E402
from runners import RUNNERS  # noqa: E402
from tui import Entry, pick_models  # noqa: E402


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="kratotatos",
        description="Run multiple coding-agent CLIs against the same problem.",
    )
    p.add_argument(
        "--problem",
        required=True,
        type=Path,
        help="Path to a file containing the problem description and success criteria.",
    )
    p.add_argument(
        "--repo",
        required=True,
        type=Path,
        help="Path to the source repository to clone for each agent.",
    )
    p.add_argument(
        "--models",
        nargs="+",
        default=None,
        help="Explicit selections, each as 'provider:model' "
        "(e.g. claude:claude-sonnet-4-6). If omitted, a TUI prompts you.",
    )
    p.add_argument(
        "--timeout",
        type=int,
        default=1800,
        help="Wall-clock timeout per agent run, in seconds (default 1800 = 30m).",
    )
    p.add_argument(
        "--judge-model",
        default="claude-opus-4-7",
        help="Model used by claude for the judge step (default claude-opus-4-7).",
    )
    p.add_argument(
        "--no-judge",
        action="store_true",
        help="Skip the judge step; report metrics only.",
    )
    p.add_argument(
        "--runs-dir",
        type=Path,
        default=HERE / "runs",
        help="Directory in which to create the per-run output folder.",
    )
    p.add_argument(
        "--models-file",
        type=Path,
        default=HERE / "models.json",
        help="JSON file with default model lists per provider.",
    )
    p.add_argument(
        "--label",
        default=None,
        help="Optional label appended to the run directory name.",
    )
    return p.parse_args(argv)


def load_default_entries(models_file: Path) -> list[Entry]:
    """Build the default entry list for the TUI from ``models.json``. Only
    providers we actually have a runner for are surfaced."""
    if not models_file.exists():
        raise FileNotFoundError(f"models file not found: {models_file}")
    data = json.loads(models_file.read_text())
    entries: list[Entry] = []
    for provider in sorted(RUNNERS.keys()):
        for model in data.get(provider, []):
            entries.append(Entry(provider=provider, model=model))
    if not entries:
        raise ValueError(f"no entries derived from {models_file}")
    return entries


def parse_explicit(specs: list[str]) -> list[Selection]:
    out: list[Selection] = []
    for spec in specs:
        if ":" not in spec:
            raise SystemExit(
                f"--models entry must be 'provider:model', got: {spec!r}"
            )
        provider, _, model = spec.partition(":")
        provider = provider.strip()
        model = model.strip()
        if provider not in RUNNERS:
            raise SystemExit(
                f"unknown provider {provider!r}; supported: {sorted(RUNNERS)}"
            )
        if not model:
            raise SystemExit(f"empty model in --models entry: {spec!r}")
        out.append(Selection(provider=provider, model=model))
    return out


def selections_from_tui(models_file: Path) -> list[Selection]:
    entries = load_default_entries(models_file)
    picked = pick_models(entries)
    if picked is None:
        raise SystemExit("cancelled")
    return [
        Selection(provider=e.provider, model=e.model) for e in picked if e.selected
    ]


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    problem_path: Path = args.problem.expanduser().resolve()
    if not problem_path.is_file():
        raise SystemExit(f"problem file not found: {problem_path}")
    problem = problem_path.read_text()

    repo_path: Path = args.repo.expanduser().resolve()
    if not repo_path.is_dir():
        raise SystemExit(f"repo path is not a directory: {repo_path}")

    if args.models:
        selections = parse_explicit(args.models)
    else:
        if not sys.stdout.isatty() or not sys.stdin.isatty():
            raise SystemExit(
                "--models is required when stdout/stdin is not a TTY"
            )
        selections = selections_from_tui(args.models_file)
    if not selections:
        raise SystemExit("no models selected; nothing to do")

    args.runs_dir.mkdir(parents=True, exist_ok=True)
    label = args.label or slugify(problem_path.stem)
    run_dir = make_run_dir(args.runs_dir, label=label)
    print(f"[kratotatos] run dir: {run_dir}")
    print(
        "[kratotatos] selections: "
        + ", ".join(f"{s.provider}:{s.model}" for s in selections)
    )

    results = run_all(
        selections=selections,
        source_repo=repo_path,
        problem=problem,
        run_dir=run_dir,
        timeout=args.timeout,
    )

    if args.no_judge:
        from judge import Verdict

        verdict = Verdict(rationale="judge step skipped (--no-judge)")
    else:
        print("[kratotatos] running judge…")
        verdict = run_judge(
            problem=problem,
            results=results,
            judge_model=args.judge_model,
            log_dir=run_dir,
        )
        print(f"[kratotatos] judge winner: {verdict.winner}")

    report_path = write_report(
        run_dir=run_dir,
        problem=problem,
        results=results,
        verdict=verdict,
    )
    print(f"[kratotatos] report: {report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
