# kratotatos

A multi-model coding-agent harness. Hand it a problem description and a
local repository, pick which agent CLIs to test, and it runs each one in
an isolated copy of the repo, captures every change as a diff, gathers
token / time / cost metrics, and asks claude to judge which solution best
satisfies the success criteria.

## Supported agents

| Provider   | CLI            | Used for                       | Sandbox                                              |
|------------|----------------|--------------------------------|------------------------------------------------------|
| `claude`   | `claude -p`    | Anthropic (Opus / Sonnet)      | `--dangerously-skip-permissions` + cwd               |
| `codex`    | `codex exec`   | OpenAI (gpt-5 family)          | `--sandbox workspace-write` (native)                 |
| `gemini`   | `gemini -p`    | Google (Gemini 2.5 / 3 family) | `--yolo --skip-trust` + cwd                          |
| `opencode` | `opencode run` | DeepSeek, z-ai (GLM), others   | cwd via `--dir` (relies on opencode permissions)     |

Each runner is invoked with the model's batch / non-interactive mode and
asked to emit JSON events so token usage and (when available) cost can be
read from stdout.

## Requirements

- Python 3.11+
- `git` on `$PATH`
- Whichever agent CLIs you want to use, already authenticated:
  `claude`, `codex`, `gemini`, `opencode`
- Optional: `bwrap` if you ever extend this with hard isolation

## Usage

Interactive — TUI prompts for which (provider, model) combos to run:

```sh
./kratotatos.py --problem examples/sample-problem.md --repo /path/to/repo
```

Non-interactive — explicit selections:

```sh
./kratotatos.py \
    --problem examples/sample-problem.md \
    --repo /path/to/repo \
    --models claude:claude-sonnet-4-6 codex:gpt-5 opencode:anthropic/claude-sonnet-4-5 \
    --timeout 1800
```

Useful flags:

- `--timeout SECONDS` — wall-clock cap per agent (default 1800)
- `--judge-model MODEL` — model used by the judge step (default `claude-opus-4-7`)
- `--no-judge` — skip judge, metrics-only report
- `--label NAME` — appended to the run directory name
- `--models-file PATH` — alternate model list (default `models.json`)

## Output layout

```
runs/<UTC-timestamp>-<label>/
  manifest.json                       # selections + baseline shas
  report.md                           # markdown comparison + judge verdict
  judge_prompt.md, judge_stdout.log   # judge inputs/outputs
  verdict.json
  <provider>__<model>/
    repo/                             # isolated working copy
    command.txt                       # exact CLI invocation
    stdout.log, stderr.log            # raw agent output
    diff.patch                        # git diff vs. kratotatos-baseline tag
    metrics.json                      # tokens, cost, wall time, files
    final_message.txt                 # codex only — last assistant message
```

## Adding / customising models

Edit `models.json` to add models the TUI presents by default. Anything not
listed there can still be used via `--models provider:model` or by pressing
`c` in the TUI to add a custom entry inline.

## TUI keys

`↑/↓` (or `j`/`k`) — move · `SPACE` — toggle · `a` — toggle all in current
provider · `c` — add custom entry · `ENTER` — run · `q` — cancel

## Notes on isolation

Each agent runs with its CWD set to its private workspace (`runs/.../repo/`).
codex uses its built-in `--sandbox workspace-write` policy. claude is run
with `--dangerously-skip-permissions` (no prompts) and trusts the prompt
constraint to stay in cwd. opencode uses `--dir` and its own permission
config. By default these are *cwd-by-convention* — a misbehaving agent
could still read `~/.ssh`, `~/.aws`, `~/.config/gh`, etc.

### Hard sandboxing (`KRATOTATOS_SANDBOX=1`)

Set `KRATOTATOS_SANDBOX=1` to wrap every runner subprocess in an OS-level
sandbox:

- **macOS**: `sandbox-exec -f <profile.sb>` (Apple Seatbelt — same primitive
  codex uses internally for `workspace-write`)
- **Linux**: `bwrap` with `--tmpfs $HOME` + bind mounts for the workspace
  and the per-CLI config dir; `--unshare-pid --unshare-uts --unshare-ipc
  --new-session --die-with-parent --share-net`

The sandbox allows:
- read+write inside the per-run workspace
- read+write of the CLI's own auth/config dir (`~/.claude`, `~/.codex`,
  `~/.gemini`, `~/.config/opencode`, `~/.local/share/opencode`,
  `~/.cache/opencode`, `~/Library/Application Support/claude`)
- read-only of `~/.gitconfig` and `~/.config/git` so the agent can run
  `git`
- full network (agents need to reach their API endpoints)

The sandbox denies (by default-deny on macOS, by tmpfs-over-`$HOME` on
Linux): `~/.ssh`, `~/.aws`, `~/.config/gh`, `~/.netrc`, `~/.gnupg`, browser
data, the macOS Keychain, every other CLI's tokens, every other directory
under `$HOME`. On Linux the env is also scrubbed: only a small allowlist
of `PATH`/`HOME`/`*_API_KEY` variables propagates into the sandbox.

If `KRATOTATOS_SANDBOX=1` is set but `sandbox-exec` / `bwrap` is missing,
runs fail loudly (`SandboxError`) rather than silently falling back to
unsandboxed execution. Prereqs:

```sh
# macOS — already installed (sandbox-exec ships with the OS)
# Linux
sudo apt install bubblewrap   # Debian / Ubuntu
sudo dnf install bubblewrap   # Fedora / RHEL
```

The generated profile (macOS) is written to each run's
`<provider>__<model>/sandbox.sb` for inspection.
# kratotatos
