#!/usr/bin/env bash
# Runs the password-manager (,pass / ,pass-store) task across the same five
# models used by jerboa-aws-cohort.sh. Each model gets up to four hours.
#
# Override any of these via the environment:
#   SOURCE_REPO    path to a clean jerboa-shell checkout       (default: ~/mine/jerboa-shell)
#   BASELINE_REF   commit to start every model from            (default: HEAD of SOURCE_REPO)
#   WORK_REPO      scratch dir kratotatos copies into runs/    (default: /tmp/jerboa-shell-pass-base)
#   PROBLEM        path to the task brief                      (default: jerboa-pass-problem.md beside this script)
#   TIMEOUT        per-agent wall-clock seconds                (default: 14400 = 4h)
#   JUDGE_TIMEOUT  judge step wall-clock seconds               (default: 1800 = 30m)
#   LABEL          appended to the runs/ subdirectory name     (default: pass-manager)
set -euo pipefail

HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
KRATOTATOS="$HERE/../kratotatos.py"

SOURCE_REPO="${SOURCE_REPO:-$HOME/mine/jerboa-shell}"
WORK_REPO="${WORK_REPO:-/tmp/jerboa-shell-pass-base}"
PROBLEM="${PROBLEM:-$HERE/jerboa-pass-problem.md}"
TIMEOUT="${TIMEOUT:-14400}"
JUDGE_TIMEOUT="${JUDGE_TIMEOUT:-1800}"
LABEL="${LABEL:-pass-manager}"

[[ -d "$SOURCE_REPO/.git" ]] || { echo "source repo not a git checkout: $SOURCE_REPO" >&2; exit 1; }
[[ -f "$PROBLEM" ]]          || { echo "problem file missing: $PROBLEM" >&2; exit 1; }
[[ -x "$KRATOTATOS" ]]       || { echo "kratotatos.py not executable at $KRATOTATOS" >&2; exit 1; }

BASELINE_REF="${BASELINE_REF:-$(git -C "$SOURCE_REPO" rev-parse HEAD)}"

# Materialise a clean baseline at $BASELINE_REF. kratotatos copies the
# on-disk state of $WORK_REPO into each per-model workspace, so this needs
# to be at the exact commit the cohort started from.
if [[ -d "$WORK_REPO/.git" ]]; then
    git -C "$WORK_REPO" fetch --quiet origin || true
    git -C "$WORK_REPO" reset --hard --quiet "$BASELINE_REF"
    git -C "$WORK_REPO" clean -fdx --quiet
else
    rm -rf "$WORK_REPO"
    git clone --quiet "$SOURCE_REPO" "$WORK_REPO"
    git -C "$WORK_REPO" checkout --quiet "$BASELINE_REF"
fi

echo "[jerboa-pass-cohort] baseline:      $(git -C "$WORK_REPO" rev-parse --short HEAD)"
echo "[jerboa-pass-cohort] problem:       $PROBLEM"
echo "[jerboa-pass-cohort] timeout:       ${TIMEOUT}s/agent ($((TIMEOUT / 3600))h)"
echo "[jerboa-pass-cohort] judge timeout: ${JUDGE_TIMEOUT}s"

exec "$KRATOTATOS" \
    --problem       "$PROBLEM" \
    --repo          "$WORK_REPO" \
    --models        claude:claude-opus-4-7 \
                    codex:gpt-5 \
                    gemini:gemini-3-pro-preview \
                    opencode:deepseek/deepseek-v4-pro \
                    opencode:openrouter/z-ai/glm-4.7 \
    --timeout       "$TIMEOUT" \
    --judge-timeout "$JUDGE_TIMEOUT" \
    --label         "$LABEL" \
    "$@"
