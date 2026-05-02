"""External OS-level sandboxing for runner subprocesses.

The default kratotatos run gives every agent CLI full access to the user's
home directory (``~/.ssh``, ``~/.aws``, browser cookie jars, etc.). Setting
``KRATOTATOS_SANDBOX=1`` wraps every runner subprocess in:

  - macOS: ``sandbox-exec -f <profile.sb>`` (Apple's Seatbelt mechanism — the
    same primitive codex uses internally for ``--sandbox workspace-write``)
  - Linux: ``bwrap`` with a tmpfs over ``$HOME`` and explicit bind mounts for
    the workspace and the CLI's own config dir

Both sandboxes:

  - allow read+write only inside the per-run workspace
  - allow read+write for the CLI's own auth/config dir (``~/.claude`` etc.)
  - allow read-only for ``~/.gitconfig`` / ``~/.config/git`` so ``git`` works
  - allow full network (agents need to reach their API endpoints)
  - **deny** everything else under ``$HOME`` — credentials, ssh keys, gh
    tokens, browser data, other CLIs' tokens

If the sandbox is requested but the wrapper binary (``sandbox-exec`` /
``bwrap``) is missing, ``wrap_command`` raises ``SandboxError`` so the run
fails loud rather than silently dropping to unsandboxed execution.
"""
from __future__ import annotations

import os
import platform
import shutil
from pathlib import Path
from typing import Iterable, Optional


class SandboxError(RuntimeError):
    """Raised when sandboxing was requested but cannot be applied."""


def is_enabled() -> bool:
    """``KRATOTATOS_SANDBOX=1`` (or any non-empty/non-zero value) opts in."""
    val = os.environ.get("KRATOTATOS_SANDBOX", "").strip().lower()
    return val not in ("", "0", "false", "no", "off")


def _platform() -> str:
    s = platform.system().lower()
    if s == "darwin":
        return "macos"
    if s == "linux":
        return "linux"
    return s


def is_available() -> tuple[bool, str]:
    """Return (available, reason). ``reason`` is empty on success, else a
    human-readable explanation of why sandboxing cannot be applied."""
    p = _platform()
    if p == "macos":
        if shutil.which("sandbox-exec") is None:
            return False, "sandbox-exec not found in PATH (expected on macOS)"
        return True, ""
    if p == "linux":
        if shutil.which("bwrap") is None:
            return (
                False,
                "bwrap (bubblewrap) not installed — apt install bubblewrap / "
                "dnf install bubblewrap",
            )
        return True, ""
    return False, f"no sandbox implementation for platform {p!r}"


# ---------------------------------------------------------------------------
# macOS: sandbox-exec profile
# ---------------------------------------------------------------------------

# System-level paths every binary needs read access to. Kept as concrete
# subpaths so the profile is fully self-contained (no ``param`` indirection).
_MACOS_SYSTEM_READ = (
    "/usr",
    "/System",
    "/Library",
    "/opt",                         # /opt/homebrew, /opt/local
    "/bin",
    "/sbin",
    "/private/etc",
    "/private/var/db",              # timezone, SystemPolicy, etc.
    "/private/var/folders",         # per-user tmp / cache (TemporaryItems)
    "/dev",
)


def _sb_quote(path: str) -> str:
    """Quote a path for inclusion in a Seatbelt profile string literal.
    Profile syntax is Scheme-like; escape backslashes and double quotes."""
    return path.replace("\\", "\\\\").replace('"', '\\"')


def _macos_profile(*, workspace: Path, cli_paths: Iterable[Path]) -> str:
    home = Path.home()
    git_ro = [home / ".gitconfig", home / ".config" / "git"]

    lines: list[str] = [
        "(version 1)",
        ";; default-deny: anything not explicitly allowed below is blocked.",
        "(deny default)",
        "",
        ";; --- process / signal / IPC primitives every binary needs ---",
        "(allow process-fork)",
        "(allow process-exec)",
        "(allow signal)",
        "(allow mach-lookup)",
        "(allow ipc-posix-shm)",
        "(allow ipc-posix-sem)",
        "(allow sysctl-read)",
        "(allow system-socket)",
        "(allow system-fsctl)",
        "",
        ";; --- network: agents need to reach their API endpoints ---",
        "(allow network*)",
        "",
        ";; --- stat anywhere so directory walks succeed (does NOT read file contents) ---",
        "(allow file-read-metadata)",
        "",
        ";; --- system read paths ---",
    ]
    for p in _MACOS_SYSTEM_READ:
        lines.append(f'(allow file-read* (subpath "{_sb_quote(p)}"))')
    # Allow reading the root directory itself (readdir on /).
    lines.append('(allow file-read* (literal "/"))')
    lines.append('(allow file-read* (literal "/private"))')

    lines += ["", ";; --- workspace: read + write + exec ---"]
    lines.append(f'(allow file* (subpath "{_sb_quote(str(workspace))}"))')
    lines.append('(allow file* (subpath "/tmp"))')
    lines.append('(allow file* (subpath "/private/tmp"))')
    lines.append('(allow file* (subpath "/private/var/folders"))')

    lines += ["", ";; --- CLI auth/config dirs: read + write so sessions persist ---"]
    seen: set[str] = set()
    for p in cli_paths:
        sp = str(p)
        if sp in seen or not sp:
            continue
        seen.add(sp)
        lines.append(f'(allow file* (subpath "{_sb_quote(sp)}"))')

    lines += ["", ";; --- git config: read-only so the agent can run git ---"]
    for p in git_ro:
        lines.append(f'(allow file-read* (subpath "{_sb_quote(str(p))}"))')

    lines.append("")
    return "\n".join(lines)


def _wrap_macos(
    cmd: list[str],
    *,
    workspace: Path,
    cli_paths: list[Path],
    log_dir: Path,
) -> list[str]:
    profile = _macos_profile(workspace=workspace, cli_paths=cli_paths)
    profile_path = log_dir / "sandbox.sb"
    profile_path.parent.mkdir(parents=True, exist_ok=True)
    profile_path.write_text(profile)
    return ["sandbox-exec", "-f", str(profile_path), *cmd]


# ---------------------------------------------------------------------------
# Linux: bwrap arg list
# ---------------------------------------------------------------------------

# Top-level system dirs to bind read-only into the sandbox.
_LINUX_RO_BIND = (
    "/usr",
    "/lib",
    "/lib64",
    "/lib32",
    "/bin",
    "/sbin",
    "/etc",
    "/opt",
)

# Devices to expose individually — full /dev would re-expose the host.
_LINUX_DEV_BIND = (
    "/dev/null",
    "/dev/zero",
    "/dev/random",
    "/dev/urandom",
    "/dev/tty",
)


def _wrap_linux(
    cmd: list[str],
    *,
    workspace: Path,
    cli_paths: list[Path],
    log_dir: Path,
) -> list[str]:
    home = Path.home()
    bw: list[str] = ["bwrap"]

    # System read-only binds (only mount paths that actually exist).
    for path in _LINUX_RO_BIND:
        if Path(path).exists():
            bw += ["--ro-bind", path, path]

    # /proc and a fresh /dev with only the safe device files.
    bw += ["--proc", "/proc"]
    bw += ["--dev", "/dev"]
    for dev in _LINUX_DEV_BIND:
        if Path(dev).exists():
            bw += ["--dev-bind-try", dev, dev]

    # Fresh tmpfs over /tmp and over $HOME so we start from nothing under HOME.
    bw += ["--tmpfs", "/tmp"]
    bw += ["--tmpfs", str(home)]

    # Workspace: read+write.
    bw += ["--bind", str(workspace), str(workspace)]

    # CLI auth/config dirs: read+write (so session state, telemetry, etc.
    # work). Each must exist on the host or bwrap errors; --bind-try is
    # tolerant of missing paths.
    seen: set[str] = set()
    for p in cli_paths:
        sp = str(p)
        if sp in seen or not sp:
            continue
        seen.add(sp)
        bw += ["--bind-try", sp, sp]

    # Git config so ``git`` inside the sandbox works.
    for ro in (home / ".gitconfig", home / ".config" / "git"):
        bw += ["--ro-bind-try", str(ro), str(ro)]

    # Isolation knobs.
    bw += [
        "--unshare-pid",
        "--unshare-uts",
        "--unshare-ipc",
        "--unshare-cgroup-try",
        "--new-session",
        "--die-with-parent",
        "--share-net",                  # keep host network; agents need it
        "--clearenv",
    ]

    # Forward only the env vars the agent actually needs. Anything else
    # (AWS_*, GITHUB_TOKEN, etc.) is dropped at this point.
    keep_env = (
        "PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "TERM",
        "SHELL", "TMPDIR",
        # CLI-specific tokens — let the runner inject these via env_extra
        # if needed; otherwise the CLI reads from its config dir.
        "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY",
        "GOOGLE_API_KEY", "GROQ_API_KEY", "DEEPSEEK_API_KEY",
        "OPENROUTER_API_KEY", "Z_AI_API_KEY",
        # Our own opt-in marker (used for nested invocations / debugging).
        "KRATOTATOS_SANDBOX",
        "GEMINI_CLI_TRUST_WORKSPACE",
    )
    for k in keep_env:
        v = os.environ.get(k)
        if v is not None:
            bw += ["--setenv", k, v]

    bw += ["--chdir", str(workspace), "--"]
    bw += cmd
    return bw


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def wrap_command(
    cmd: list[str],
    *,
    workspace: Path,
    cli_paths: Optional[Iterable[Path]] = None,
    log_dir: Path,
) -> list[str]:
    """If sandboxing is enabled and available, return ``cmd`` wrapped in the
    platform's sandbox launcher. Otherwise return ``cmd`` unchanged.

    Raises ``SandboxError`` when sandboxing was requested via the env var
    but the underlying tool is missing or the platform is unsupported.
    """
    if not is_enabled():
        return cmd
    ok, reason = is_available()
    if not ok:
        raise SandboxError(
            f"KRATOTATOS_SANDBOX is set but sandboxing is unavailable: {reason}"
        )
    cli_list = [Path(p) for p in (cli_paths or [])]
    if _platform() == "macos":
        return _wrap_macos(
            cmd, workspace=workspace, cli_paths=cli_list, log_dir=log_dir
        )
    return _wrap_linux(
        cmd, workspace=workspace, cli_paths=cli_list, log_dir=log_dir
    )
