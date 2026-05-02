#!/usr/bin/env bash
# Replicates the five-model jerboa-shell cohort run against continue.md
# (the ,aws / ,pssm feature-gating task) using kratotatos.
#
# Override any of these via the environment:
#   SOURCE_REPO   path to a clean jerboa-shell checkout      (default: ~/mine/jerboa-shell)
#   BASELINE_REF  commit to start every model from           (default: 5a20f56)
#   WORK_REPO    scratch dir kratotatos copies into runs/   (default: /tmp/jerboa-shell-base)
#   PROBLEM       path to the task brief                     (default: continue.md from claude branch)
#   TIMEOUT       per-agent wall-clock seconds               (default: 1800)
#   LABEL         appended to the runs/ subdirectory name    (default: aws-gating)
set -euo pipefail

SOURCE_REPO="${SOURCE_REPO:-$HOME/mine/jerboa-shell}"
BASELINE_REF="${BASELINE_REF:-5a20f56}"
WORK_REPO="${WORK_REPO:-/tmp/jerboa-shell-base}"
PROBLEM="${PROBLEM:-$HOME/mine/test/claude/jerboa-shell/continue.md}"
TIMEOUT="${TIMEOUT:-1800}"
LABEL="${LABEL:-aws-gating}"

HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
KRATOTATOS="$HERE/../kratotatos.py"

[[ -d "$SOURCE_REPO/.git" ]] || { echo "source repo not a git checkout: $SOURCE_REPO" >&2; exit 1; }
[[ -f "$PROBLEM" ]]          || { echo "problem file missing: $PROBLEM" >&2; exit 1; }
[[ -x "$KRATOTATOS" ]]       || { echo "kratotatos.py not executable at $KRATOTATOS" >&2; exit 1; }

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

echo "[jerboa-aws-cohort] baseline: $(git -C "$WORK_REPO" rev-parse --short HEAD)"
echo "[jerboa-aws-cohort] problem:  $PROBLEM"
echo "[jerboa-aws-cohort] timeout:  ${TIMEOUT}s/agent"

exec "$KRATOTATOS" \
    --problem "$PROBLEM" \
    --repo    "$WORK_REPO" \
    --models  claude:claude-opus-4-7 \
              codex:gpt-5 \
              gemini:gemini-3-pro-preview \
              opencode:deepseek/deepseek-v4-pro \
              opencode:openrouter/z-ai/glm-4.7 \
    --timeout "$TIMEOUT" \
    --label   "$LABEL" \
    "$@"
