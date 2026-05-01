"""Subprocess wrappers for claude, codex, opencode.

Each runner spawns the underlying CLI in non-interactive mode, streams output
to a log directory, and parses the tool's structured output for token and
cost metrics. The wrappers share a common return shape (``RunResult``) so the
orchestrator and report layers do not need to special-case each provider.
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional


SYSTEM_PROMPT = """\
You are running inside a sandboxed copy of a software repository. Make changes \
only to files inside the current working directory. Do not access files \
outside this directory. Work autonomously: do not ask clarifying questions; \
make reasonable assumptions and proceed. The user's task and its success \
criteria are below. When you believe the task is complete, exit.\
"""


@dataclass
class RunResult:
    """Outcome of a single runner invocation."""

    provider: str
    model: str
    label: str  # human-friendly id, e.g. "claude/opus" or "codex/gpt-5"
    workdir: str
    log_dir: str
    exit_code: int = -1
    wall_seconds: float = 0.0
    timed_out: bool = False
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    reasoning_tokens: int = 0
    cost_usd: Optional[float] = None
    num_turns: int = 0
    error: Optional[str] = None
    # Filled in by orchestrator after the runner returns:
    files_changed: int = 0
    lines_added: int = 0
    lines_deleted: int = 0
    diff_path: Optional[str] = None
    final_message: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Shared subprocess helper
# ---------------------------------------------------------------------------

def _spawn(
    cmd: list[str],
    *,
    cwd: Path,
    log_dir: Path,
    timeout: int,
    env_extra: Optional[dict[str, str]] = None,
) -> tuple[int, bool, float, str]:
    """Run ``cmd``, streaming stdout/stderr to log files. Returns (exit_code,
    timed_out, wall_seconds, stdout_path).

    The child process is placed in its own process group so we can kill the
    entire tree if the timeout fires (subagent CLIs commonly fork helpers).
    """
    log_dir.mkdir(parents=True, exist_ok=True)
    stdout_path = log_dir / "stdout.log"
    stderr_path = log_dir / "stderr.log"
    cmd_path = log_dir / "command.txt"
    cmd_path.write_text(" \\\n  ".join(cmd) + "\n")

    env = os.environ.copy()
    if env_extra:
        env.update(env_extra)

    start = time.monotonic()
    timed_out = False
    with stdout_path.open("wb") as out, stderr_path.open("wb") as err:
        proc = subprocess.Popen(
            cmd,
            cwd=str(cwd),
            stdout=out,
            stderr=err,
            env=env,
            start_new_session=True,
        )
        try:
            exit_code = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                exit_code = proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                exit_code = proc.wait()
    return exit_code, timed_out, time.monotonic() - start, str(stdout_path)


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if not path.exists():
        return out
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


# ---------------------------------------------------------------------------
# Claude Code runner
# ---------------------------------------------------------------------------

def run_claude(
    *,
    repo_dir: Path,
    log_dir: Path,
    model: str,
    problem: str,
    timeout: int,
) -> RunResult:
    """Run ``claude -p`` with stream-json output, then parse the trailing
    result event for usage and cost.
    """
    label = f"claude/{model}"
    result = RunResult(
        provider="claude",
        model=model,
        label=label,
        workdir=str(repo_dir),
        log_dir=str(log_dir),
    )
    if shutil.which("claude") is None:
        result.error = "claude CLI not found in PATH"
        return result

    user_prompt = f"{SYSTEM_PROMPT}\n\n---\n\n{problem}"
    cmd = [
        "claude",
        "-p",
        user_prompt,
        "--model",
        model,
        "--output-format",
        "stream-json",
        "--verbose",
        "--include-partial-messages",
        "--dangerously-skip-permissions",
        "--no-session-persistence",
    ]

    exit_code, timed_out, wall, _ = _spawn(
        cmd, cwd=repo_dir, log_dir=log_dir, timeout=timeout
    )
    result.exit_code = exit_code
    result.timed_out = timed_out
    result.wall_seconds = wall

    events = _read_jsonl(log_dir / "stdout.log")
    for ev in reversed(events):
        if ev.get("type") == "result":
            usage = ev.get("usage") or {}
            result.input_tokens = int(usage.get("input_tokens") or 0)
            result.output_tokens = int(usage.get("output_tokens") or 0)
            result.cache_read_tokens = int(usage.get("cache_read_input_tokens") or 0)
            result.cache_creation_tokens = int(
                usage.get("cache_creation_input_tokens") or 0
            )
            cost = ev.get("total_cost_usd")
            if cost is not None:
                result.cost_usd = float(cost)
            result.num_turns = int(ev.get("num_turns") or 0)
            result.final_message = str(ev.get("result") or "")
            if ev.get("is_error"):
                result.error = result.final_message[:500] or "claude reported error"
            break

    if exit_code != 0 and not result.error:
        result.error = f"claude exited with code {exit_code}"
    if timed_out:
        result.error = (result.error or "") + " (timed out)"
    return result


# ---------------------------------------------------------------------------
# Codex runner
# ---------------------------------------------------------------------------

def run_codex(
    *,
    repo_dir: Path,
    log_dir: Path,
    model: str,
    problem: str,
    timeout: int,
) -> RunResult:
    """Run ``codex exec --json`` with workspace-write sandbox. Final message
    and token counts are parsed from the trailing JSONL events.
    """
    label = f"codex/{model}"
    result = RunResult(
        provider="codex",
        model=model,
        label=label,
        workdir=str(repo_dir),
        log_dir=str(log_dir),
    )
    if shutil.which("codex") is None:
        result.error = "codex CLI not found in PATH"
        return result

    user_prompt = f"{SYSTEM_PROMPT}\n\n---\n\n{problem}"
    last_msg_path = log_dir / "final_message.txt"
    cmd = [
        "codex",
        "exec",
        "-m",
        model,
        "-C",
        str(repo_dir),
        "--sandbox",
        "workspace-write",
        "--skip-git-repo-check",
        "--json",
        "-o",
        str(last_msg_path),
        user_prompt,
    ]

    exit_code, timed_out, wall, _ = _spawn(
        cmd, cwd=repo_dir, log_dir=log_dir, timeout=timeout
    )
    result.exit_code = exit_code
    result.timed_out = timed_out
    result.wall_seconds = wall

    # Codex emits many event types; token usage usually rides on a
    # ``token_count`` / ``token_usage`` event near the end. Be defensive about
    # field names since they shift across versions.
    events = _read_jsonl(log_dir / "stdout.log")
    turns = 0
    for ev in events:
        msg = ev.get("msg") if isinstance(ev.get("msg"), dict) else ev
        et = (msg.get("type") if isinstance(msg, dict) else None) or ev.get("type", "")
        if et in ("agent_message", "assistant_message", "message"):
            turns += 1
        usage = None
        if isinstance(msg, dict):
            usage = msg.get("token_usage") or msg.get("usage") or msg.get("info")
        if isinstance(usage, dict):
            inp = usage.get("input_tokens") or usage.get("prompt_tokens") or usage.get("input")
            out_t = usage.get("output_tokens") or usage.get("completion_tokens") or usage.get("output")
            cache_r = (
                usage.get("cached_input_tokens")
                or usage.get("cache_read_tokens")
                or usage.get("cached_tokens")
            )
            reason = usage.get("reasoning_tokens") or usage.get("reasoning_output_tokens")
            cost = usage.get("total_cost_usd") or usage.get("cost_usd")
            if inp is not None:
                result.input_tokens = max(result.input_tokens, int(inp))
            if out_t is not None:
                result.output_tokens = max(result.output_tokens, int(out_t))
            if cache_r is not None:
                result.cache_read_tokens = max(result.cache_read_tokens, int(cache_r))
            if reason is not None:
                result.reasoning_tokens = max(result.reasoning_tokens, int(reason))
            if cost is not None:
                try:
                    result.cost_usd = float(cost)
                except (TypeError, ValueError):
                    pass
    result.num_turns = turns
    if last_msg_path.exists():
        try:
            result.final_message = last_msg_path.read_text(errors="replace")
        except OSError:
            pass

    if exit_code != 0 and not result.error:
        result.error = f"codex exited with code {exit_code}"
    if timed_out:
        result.error = (result.error or "") + " (timed out)"
    return result


# ---------------------------------------------------------------------------
# Opencode runner
# ---------------------------------------------------------------------------

def run_opencode(
    *,
    repo_dir: Path,
    log_dir: Path,
    model: str,
    problem: str,
    timeout: int,
) -> RunResult:
    """Run ``opencode run --format json``. Token usage is collected from
    streamed JSON events.
    """
    label = f"opencode/{model}"
    result = RunResult(
        provider="opencode",
        model=model,
        label=label,
        workdir=str(repo_dir),
        log_dir=str(log_dir),
    )
    if shutil.which("opencode") is None:
        result.error = "opencode CLI not found in PATH"
        return result

    user_prompt = f"{SYSTEM_PROMPT}\n\n---\n\n{problem}"
    cmd = [
        "opencode",
        "run",
        "-m",
        model,
        "--format",
        "json",
        "--dir",
        str(repo_dir),
        user_prompt,
    ]

    exit_code, timed_out, wall, _ = _spawn(
        cmd, cwd=repo_dir, log_dir=log_dir, timeout=timeout
    )
    result.exit_code = exit_code
    result.timed_out = timed_out
    result.wall_seconds = wall

    events = _read_jsonl(log_dir / "stdout.log")
    turns = 0
    final_msg = ""
    for ev in events:
        et = ev.get("type") or ev.get("event") or ""
        if et in ("message", "assistant", "assistant_message"):
            turns += 1
            text = ev.get("text") or ev.get("content")
            if isinstance(text, str) and text:
                final_msg = text
        # Find usage block on any event that carries one.
        for key in ("usage", "tokens", "metrics"):
            usage = ev.get(key)
            if isinstance(usage, dict):
                inp = usage.get("input") or usage.get("input_tokens") or usage.get("prompt_tokens")
                out_t = usage.get("output") or usage.get("output_tokens") or usage.get("completion_tokens")
                cache_r = usage.get("cache_read") or usage.get("cache_read_tokens") or usage.get("cached")
                cache_c = usage.get("cache_write") or usage.get("cache_creation_tokens")
                reason = usage.get("reasoning") or usage.get("reasoning_tokens")
                cost = usage.get("cost") or usage.get("cost_usd") or usage.get("total_cost_usd")
                if inp is not None:
                    result.input_tokens = max(result.input_tokens, int(inp))
                if out_t is not None:
                    result.output_tokens = max(result.output_tokens, int(out_t))
                if cache_r is not None:
                    result.cache_read_tokens = max(result.cache_read_tokens, int(cache_r))
                if cache_c is not None:
                    result.cache_creation_tokens = max(result.cache_creation_tokens, int(cache_c))
                if reason is not None:
                    result.reasoning_tokens = max(result.reasoning_tokens, int(reason))
                if cost is not None:
                    try:
                        result.cost_usd = float(cost)
                    except (TypeError, ValueError):
                        pass
    result.num_turns = turns
    result.final_message = final_msg

    if exit_code != 0 and not result.error:
        result.error = f"opencode exited with code {exit_code}"
    if timed_out:
        result.error = (result.error or "") + " (timed out)"
    return result


RUNNERS = {
    "claude": run_claude,
    "codex": run_codex,
    "opencode": run_opencode,
}


def run_provider(provider: str, **kwargs: Any) -> RunResult:
    fn = RUNNERS.get(provider)
    if fn is None:
        raise ValueError(f"unknown provider: {provider}")
    return fn(**kwargs)
