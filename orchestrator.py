"""Workspace preparation, parallel execution, and post-run diff capture.

For each (provider, model) selection we get an isolated copy of the source
repository, a baseline git commit (so we can always produce a clean diff),
and a per-runner log directory. Runs execute concurrently; each is bounded
by a wall-clock timeout enforced inside the runner subprocess.
"""
from __future__ import annotations

import concurrent.futures as cf
import json
import shutil
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from runners import RunResult, run_provider


BASELINE_TAG = "kratotatos-baseline"


@dataclass
class Selection:
    """A single (provider, model) the user picked from the TUI / CLI."""

    provider: str
    model: str

    @property
    def slug(self) -> str:
        # Filesystem-safe directory name. Models like "anthropic/claude-sonnet-4-5"
        # contain slashes; flatten them.
        safe_model = self.model.replace("/", "_").replace(":", "_")
        return f"{self.provider}__{safe_model}"


# ---------------------------------------------------------------------------
# Workspace preparation
# ---------------------------------------------------------------------------

def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=False,
    )


def make_run_dir(base: Path, label: str | None = None) -> Path:
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    name = f"{ts}" if not label else f"{ts}-{label}"
    run_dir = base / name
    run_dir.mkdir(parents=True, exist_ok=False)
    return run_dir


def prepare_workspace(
    *, source_repo: Path, target: Path
) -> str:
    """Copy ``source_repo`` into ``target`` and return the baseline commit
    sha. If the source isn't a git repo we initialise one inside the copy so
    that a diff can always be produced.
    """
    if not source_repo.exists():
        raise FileNotFoundError(f"source repo not found: {source_repo}")
    if target.exists():
        raise FileExistsError(f"workspace already exists: {target}")

    shutil.copytree(
        source_repo,
        target,
        symlinks=True,
        ignore=shutil.ignore_patterns(
            "node_modules", ".venv", "venv", "__pycache__", ".tox"
        ),
    )

    git_dir = target / ".git"
    if not git_dir.exists():
        _git(["init", "-q"], target)
        _git(["config", "user.email", "kratotatos@local"], target)
        _git(["config", "user.name", "kratotatos"], target)
        _git(["add", "-A"], target)
        _git(
            [
                "-c",
                "commit.gpgsign=false",
                "commit",
                "-q",
                "--allow-empty",
                "-m",
                "kratotatos baseline",
            ],
            target,
        )
    else:
        # Source was already a git repo; freeze whatever it was on as
        # baseline. If there are uncommitted changes, snapshot them so the
        # diff is meaningful.
        status = _git(["status", "--porcelain"], target).stdout.strip()
        if status:
            _git(["add", "-A"], target)
            _git(
                [
                    "-c",
                    "commit.gpgsign=false",
                    "-c",
                    "user.email=kratotatos@local",
                    "-c",
                    "user.name=kratotatos",
                    "commit",
                    "-q",
                    "--allow-empty",
                    "-m",
                    "kratotatos baseline (uncommitted snapshot)",
                ],
                target,
            )
    sha = _git(["rev-parse", "HEAD"], target).stdout.strip()
    _git(["tag", "-f", BASELINE_TAG], target)
    return sha


def capture_diff(workspace: Path, log_dir: Path) -> tuple[int, int, int, str]:
    """After a runner completes, stage everything and produce a diff against
    the baseline tag. Returns (files_changed, lines_added, lines_deleted,
    diff_path).
    """
    _git(["add", "-A"], workspace)
    diff_path = log_dir / "diff.patch"
    diff = _git(["diff", "--no-color", BASELINE_TAG], workspace)
    diff_path.write_text(diff.stdout)

    stat = _git(["diff", "--numstat", BASELINE_TAG], workspace).stdout.strip()
    files = 0
    added = 0
    deleted = 0
    for line in stat.splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        a, d, _path = parts[0], parts[1], parts[2]
        files += 1
        if a.isdigit():
            added += int(a)
        if d.isdigit():
            deleted += int(d)
    return files, added, deleted, str(diff_path)


# ---------------------------------------------------------------------------
# Parallel execution
# ---------------------------------------------------------------------------

def run_all(
    *,
    selections: list[Selection],
    source_repo: Path,
    problem: str,
    run_dir: Path,
    timeout: int,
) -> list[RunResult]:
    """Prepare workspaces and execute every selection concurrently."""
    workspaces: dict[str, Path] = {}
    log_dirs: dict[str, Path] = {}
    baselines: dict[str, str] = {}

    for sel in selections:
        ws = run_dir / sel.slug / "repo"
        ld = run_dir / sel.slug
        ld.mkdir(parents=True, exist_ok=True)
        sha = prepare_workspace(source_repo=source_repo, target=ws)
        workspaces[sel.slug] = ws
        log_dirs[sel.slug] = ld
        baselines[sel.slug] = sha

    results: list[RunResult] = []
    started = time.monotonic()

    def _worker(sel: Selection) -> RunResult:
        return run_provider(
            sel.provider,
            repo_dir=workspaces[sel.slug],
            log_dir=log_dirs[sel.slug],
            model=sel.model,
            problem=problem,
            timeout=timeout,
        )

    print(
        f"[orchestrator] starting {len(selections)} run(s) "
        f"(timeout {timeout}s each)"
    )
    with cf.ThreadPoolExecutor(max_workers=len(selections)) as pool:
        futures = {pool.submit(_worker, sel): sel for sel in selections}
        for fut in cf.as_completed(futures):
            sel = futures[fut]
            try:
                res = fut.result()
            except Exception as exc:  # noqa: BLE001 - surface any worker error
                res = RunResult(
                    provider=sel.provider,
                    model=sel.model,
                    label=f"{sel.provider}/{sel.model}",
                    workdir=str(workspaces[sel.slug]),
                    log_dir=str(log_dirs[sel.slug]),
                    error=f"worker exception: {exc!r}",
                )
            files, added, deleted, dpath = capture_diff(
                workspaces[sel.slug], log_dirs[sel.slug]
            )
            res.files_changed = files
            res.lines_added = added
            res.lines_deleted = deleted
            res.diff_path = dpath
            (log_dirs[sel.slug] / "metrics.json").write_text(
                json.dumps(res.to_dict(), indent=2)
            )
            print(
                f"[orchestrator] done {res.label} "
                f"({res.wall_seconds:.1f}s, "
                f"{res.files_changed} files, "
                f"{res.lines_added}+/{res.lines_deleted}-)"
            )
            results.append(res)
    print(
        f"[orchestrator] all runs finished in {time.monotonic() - started:.1f}s"
    )

    write_manifest(run_dir, selections, baselines)
    return results


def write_manifest(
    run_dir: Path,
    selections: list[Selection],
    baselines: dict[str, str],
) -> None:
    manifest = {
        "created": datetime.now(timezone.utc).isoformat(),
        "run_dir": str(run_dir),
        "selections": [
            {
                "provider": s.provider,
                "model": s.model,
                "slug": s.slug,
                "baseline": baselines.get(s.slug, ""),
            }
            for s in selections
        ],
    }
    (run_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))


def slugify(text: str, max_len: int = 30) -> str:
    safe = "".join(ch if ch.isalnum() else "-" for ch in text.lower())
    safe = "-".join(p for p in safe.split("-") if p)
    return safe[:max_len].strip("-") or "run"
