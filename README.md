# kratotatos

A multi-model coding-agent harness. Hand it a problem description and a
local repository, pick which agent CLIs to test, and it runs each one in
an isolated copy of the repo, captures every change as a diff, gathers
token / time / cost metrics, and asks claude to judge which solution best
satisfies the success criteria.

## Supported agents

| Provider   | CLI         | Sandbox                            |
|------------|-------------|------------------------------------|
| `claude`   | `claude -p` | `--dangerously-skip-permissions` + cwd |
| `codex`    | `codex exec`| `--sandbox workspace-write` (native) |
| `opencode` | `opencode run` | cwd via `--dir` (relies on opencode permissions) |

Each runner is invoked with the model's batch / non-interactive mode and
asked to emit JSON events so token usage and (when available) cost can be
read from stdout.

## Requirements

- Python 3.11+
- `git` on `$PATH`
- Whichever agent CLIs you want to use, already authenticated:
  `claude`, `codex`, `opencode`
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
config. For hard filesystem isolation, wrap `kratotatos.py` itself in
`bwrap` / `firejail` / a container.
# kratotatos
