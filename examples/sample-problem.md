# Sample problem: add a `--reverse` flag to greet.py

## Background

The repository contains a tiny Python script `greet.py` that prints a
greeting. Today it always prints the greeting forwards.

## Task

Add a `--reverse` command-line flag to `greet.py`. When `--reverse` is
passed, the greeting must be printed character-reversed. Without the flag,
behaviour must not change.

## Constraints

- Only modify files inside the current working directory.
- Do not add new dependencies.
- Keep the change minimal — no refactor of unrelated code.

## Success criteria

1. `python greet.py World` prints `Hello, World!` exactly as before.
2. `python greet.py --reverse World` prints `!dlroW ,olleH`.
3. Running `python -m unittest test_greet.py` passes if a test file exists;
   if there is no test file, no tests need to be added.
4. The diff touches only `greet.py` (and at most one test file).
