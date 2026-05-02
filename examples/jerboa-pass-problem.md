# jerboa-shell: `,pass` + `,unlock`-gated password manager

## Background

jerboa-shell (`jsh`) is a Chez-Scheme shell. Two existing primitives are
load-bearing for this task — read them before writing any code:

1. **`//embed/` virtual filesystem** (`src/jsh/embed.sls`) — files baked
   into the binary at build time, optionally encrypted with
   ChaCha20-Poly1305 when `JSH_EMBED_ENCRYPT=1`. A **writable overlay**
   at `~/.jsh/embed/` lets `jsh` write new files at runtime, encrypted
   with the same scheme; reads check the overlay first, then fall back
   to the baked-in data. Use `embed-file-ref`, `embed-overlay-write!`,
   `embed-file-exists?` — all already exported.
2. **`,unlock`** — comma command that prompts for a passphrase on
   `/dev/tty` (no echo), derives the ChaCha20-Poly1305 key via the
   C-side `ffi-embed-prompt-and-derive` (the passphrase never enters
   the Scheme heap), and unlocks both the embed and the overlay. Unlock
   state is in-memory only; it dies with the process. The
   `(embed-unlocked?)` predicate reports current state.

You will use these existing primitives. **Do not rebuild encryption,
key derivation, or passphrase prompting.** Reuse `embed-overlay-write!`
to store secrets and `embed-file-ref` to fetch them.

## Goal

Add a password manager exposed as two new comma commands, gated on
`,unlock` having succeeded.

### `,pass-store` (new)

Stores a secret. Behavior:

1. Prompts for **name** WITH echo: `Secret name: `
2. Prompts for **password** with NO echo: `Password: `, then a
   confirmation prompt `Confirm: `. Both inputs must match; if not,
   exit non-zero with `,pass-store: passwords do not match` and store
   nothing.
3. Verifies `(embed-unlocked?)`. If false, exit non-zero with:
   `,pass-store: embed locked — run ,unlock first`
4. Writes the password to the overlay at `//embed/passwords/<name>`
   via `embed-overlay-write!` (which already encrypts with the
   unlocked key).
5. Prints `Stored secret: <name>` (DO NOT print the password).
6. Zeros the password bytevector in memory after writing (use
   `bytevector-fill!` — same pattern already in use elsewhere in the
   codebase).

### `,pass` (new)

Fetches a stored secret to the OS clipboard. Behavior:

1. Prompts for **name** with NO echo: `Secret name: ` — per the user's
   threat model, the name itself is sensitive (defense against
   shoulder-surfing; assume an observer who knows you ran `,pass`
   should learn nothing about which secret you accessed).
2. Verifies `(embed-unlocked?)`. If false, exit non-zero with:
   `,pass: embed locked — run ,unlock first`
3. Reads `//embed/passwords/<name>` via `embed-file-ref`. If the file
   does not exist, exit non-zero with:
   `,pass: no secret named <name>`
4. Pipes the decrypted password to the OS clipboard using the
   platform-appropriate tool (see Platform support). The password must
   reach the clipboard tool **over stdin only — never as a
   command-line argument** (process listings would leak it).
5. Prints `Copied secret <name> to clipboard (will not be re-displayed)`
   and exits 0.

The password must NEVER be written to stdout, stderr, the shell history,
a temp file, or any process argv.

## Platform support

The clipboard write must work on all four platforms jerboa-shell builds
for. Detect at **runtime**, not at build time — the same binary should
work on whichever clipboard backend is present.

| Platform        | Clipboard primitive                                                                  | Detection                                              |
|-----------------|--------------------------------------------------------------------------------------|--------------------------------------------------------|
| macOS           | `pbcopy` (stdin → clipboard)                                                         | `(machine-type)` contains `osx`, OR `pbcopy` on PATH   |
| Linux           | First on PATH of: `wl-copy` (Wayland), `xclip -selection clipboard`, `xsel --clipboard --input` | PATH probe; prefer `wl-copy` if `XDG_SESSION_TYPE=wayland` |
| FreeBSD         | Same chain as Linux                                                                  | PATH probe                                             |
| Termux/Android  | `termux-clipboard-set`                                                               | `(machine-type)` ends in `android`, OR `termux-clipboard-set` on PATH |

If no clipboard tool is available, exit non-zero with:
`,pass: no clipboard tool found (tried: pbcopy, wl-copy, xclip, xsel, termux-clipboard-set)`
**Do not fall back to printing the password.**

## Files in scope

- `src/jsh/pass.sls` — new module: both commands + the clipboard
  helper. Self-contained; imports `(jsh embed)` and Chez stdlib only.
- `src/jsh/main.sls` (or wherever the existing `,unlock` is registered
  — find it by grepping `",unlock"` in `src/jsh/`) — register `,pass`
  and `,pass-store` in the same place, behind the same feature-flag
  pattern used by `,aws` from the previous task.
- `features.def` — add a `pass` feature with `commands` `("pass"
  "pass-store")`, modules `("pass")`. Mirror the shape of the existing
  `aws` feature.
- `build-jsh-macos.ss`, `build-jsh-musl.ss`, `build-jsh-freebsd.ss`,
  `build-jsh-android.ss` — wire the `pass` feature through to the
  boot-file inclusion. Mirror the `aws.sls` wiring exactly.
- `test/test-pass.sh` — integration tests (see Tests below).
- `test/fixtures/embed-data.sls` — minimal test fixture (see Test
  fixture below).

## Files out of scope (do NOT touch)

- `src/jsh/embed-data.sls` — production secrets. The test fixture
  replaces it during testing only.
- `src/jsh/embed.sls` — encryption format is fixed; do not change it.
- Any C / Rust source. Use existing FFI only.
- `aws.sls`, `pssm.sls`, or any other unrelated feature.

## Test fixture

The user's real `embed-data.sls` is unavailable to you. Build
`test/fixtures/embed-data.sls` from scratch with:

- A deterministic test salt (32 bytes of `#x42`) — reproducible across
  runs.
- A minimal encrypted payload that the test passphrase
  `test-passphrase-do-not-use-in-prod` unlocks. The payload's contents
  do not matter for the password-manager tests; the `,unlock` path
  just needs to succeed so `(embed-unlocked?)` becomes true.
- The same Argon2id parameters and ChaCha20-Poly1305 nonce/tag layout
  the real `embed.sls` expects (read `src/jsh/embed.sls` to discover —
  the constants are at the top of the file).

`test/test-pass.sh` symlinks this fixture into place
(`src/jsh/embed-data.sls` → `test/fixtures/embed-data.sls`) before the
build, then restores the original symlink/file afterward (use a `trap`
on EXIT so the cleanup runs even if a subtest fails).

## Driving `,unlock` non-interactively

The tests need to feed a passphrase to `,unlock` without a real TTY.
Two acceptable approaches — pick whichever is cleaner:

1. **Test-only env var**: Add `JSH_TEST_PASSPHRASE` support inside
   `ffi-embed-prompt-and-derive` (or its Scheme caller) such that, if
   the env var is set AND `JSH_TEST_MODE=1` is also set, the prompt is
   bypassed. Both env vars must be required so production builds
   cannot be tricked into accepting an env-var passphrase.
2. **Expect-style PTY driver**: A small helper (`test/drive-tty.sh`
   using `script` on macOS or `expect` on Linux) that opens a PTY,
   feeds the passphrase, then runs the comma command.

Approach (1) is simpler and self-contained — strongly preferred.

## Tests (`test/test-pass.sh`)

A POSIX `bash` script that the project's existing test harness can run.

- Exits 0 on success, non-zero on failure.
- Exits 77 (the autoconf "skipped" convention) if the host's build
  tools are missing.
- Each subtest prints `PASS: <name>` or `FAIL: <name>` and a one-line
  reason on failure.

### Required subtests

```bash
# T1: ,pass-store before ,unlock errors with "embed locked"
out=$(printf 'name\npw\npw\n' | ./jsh-macos -c ',pass-store' 2>&1)
echo "$out" | grep -q 'embed locked' \
  || { echo "FAIL T1: ,pass-store did not require unlock; got: $out"; exit 1; }

# T2: ,pass before ,unlock errors with "embed locked"
out=$(printf 'name\n' | ./jsh-macos -c ',pass' 2>&1)
echo "$out" | grep -q 'embed locked' \
  || { echo "FAIL T2: ,pass did not require unlock"; exit 1; }

# T3: After ,unlock with test passphrase, ,pass-store of "github" + password
# "hunter2" succeeds; ,pass for "github" copies "hunter2" to the clipboard.
JSH_TEST_MODE=1 JSH_TEST_PASSPHRASE='test-passphrase-do-not-use-in-prod' \
  ./jsh-macos -c ',unlock; ,pass-store; ,pass' \
  <<< $'github\nhunter2\nhunter2\ngithub\n' >/tmp/test-out 2>&1
# verify clipboard
test "$(pbpaste 2>/dev/null || xclip -selection clipboard -o 2>/dev/null \
       || wl-paste 2>/dev/null || termux-clipboard-get 2>/dev/null)" = 'hunter2' \
  || { echo "FAIL T3: clipboard did not receive password"; exit 1; }
# verify password not in stdout/stderr
grep -q 'hunter2' /tmp/test-out \
  && { echo "FAIL T3: password leaked to stdout/stderr"; exit 1; }

# T4: ,pass for an unknown secret prints "no secret named bogus"
JSH_TEST_MODE=1 JSH_TEST_PASSPHRASE='test-passphrase-do-not-use-in-prod' \
  ./jsh-macos -c ',unlock; ,pass' <<< $'bogus\n' 2>&1 \
  | grep -q 'no secret named bogus' \
  || { echo "FAIL T4: missing secret error wrong"; exit 1; }

# T5: With NO clipboard tool on PATH, ,pass errors clearly and does NOT
# print the password.
PATH=/var/empty JSH_TEST_MODE=1 JSH_TEST_PASSPHRASE='test-passphrase-do-not-use-in-prod' \
  ./jsh-macos -c ',unlock; ,pass-store; ,pass' \
  <<< $'leak\nshould-not-appear\nshould-not-appear\nleak\n' >/tmp/test-out 2>&1
grep -q 'no clipboard tool found' /tmp/test-out \
  || { echo "FAIL T5: missing-tool error wrong"; exit 1; }
grep -q 'should-not-appear' /tmp/test-out \
  && { echo "FAIL T5: password leaked when clipboard tool missing"; exit 1; }
```

## Acceptance criteria

1. `JSH_FEATURES=pass make jsh-macos-full` builds clean.
2. `JSH_FEATURES=pass make jsh-musl-full` builds clean (cross or
   native, whichever the host supports).
3. `JSH_FEATURES=pass make jsh-freebsd-full` builds clean.
4. `JSH_FEATURES=pass make jsh-android-full` builds clean.
5. `bash test/test-pass.sh` exits 0 on the host with all five subtests
   passing.
6. `bash test/test-features-commands.sh` (from the prior cohort task,
   if present) still passes; both `,pass` and `,pass-store` appear
   in its enumeration when `JSH_FEATURES=pass` is set.
7. With `JSH_FEATURES=none`, both commands report
   `not available in this build` (same shape as `,aws` in the locked
   case from the prior task).

## Constraints

- **Use the highest-quality crypto already present.** ChaCha20-Poly1305
  + Argon2id is already in `embed.sls`. Do not introduce AES, do not
  invent a new format, do not roll your own KDF.
- **Password material never leaves controlled memory.** No temp files,
  no command-line args, no logging. The bytevector holding the
  password gets `bytevector-fill!`-zeroed after the clipboard pipe
  completes.
- **Do not add network calls.** This is offline storage only.
- **Do not modify any platform's build script except to wire the
  `pass` feature through.** Same scope rule as the prior cohort task.
- **Work autonomously.** Do not ask clarifying questions; make
  reasonable assumptions and document them in a `final.md` at the
  repo root summarising what you did, what you skipped, and any
  decisions you made under ambiguity. The judge reads `final.md`.

## Notes

- Clipboard contents persist after `jsh` exits — that's expected for a
  password manager. No auto-clear in this task.
- List/delete/rename of secrets is out of scope; future work.
- If your build hits a Chez-10.4 multiple-definitions error similar to
  the one from the prior `,aws` task (`base64-encode`, `bytevector-append`),
  fix it the same way — patch the conflicting `(library ...)` form to
  not re-export the builtin.
