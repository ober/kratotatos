"""Curses-based multi-select model picker.

The TUI presents a flat list of (provider, model) entries grouped visually
by provider. Arrow keys / j-k move the cursor, space toggles, ``a`` toggles
all entries within the current provider, enter accepts. ``c`` opens a small
inline prompt to add a custom ``provider:model`` entry; ``q`` cancels.
"""
from __future__ import annotations

import curses
from dataclasses import dataclass
from typing import Optional


@dataclass
class Entry:
    provider: str
    model: str
    selected: bool = False

    @property
    def label(self) -> str:
        return f"{self.provider}/{self.model}"


def _draw(stdscr, entries: list[Entry], cursor: int, message: str) -> None:
    stdscr.erase()
    h, w = stdscr.getmaxyx()
    title = "kratotatos — select models to test"
    stdscr.addnstr(0, 0, title, w - 1, curses.A_BOLD)
    legend = (
        "↑/↓ move  SPACE toggle  a toggle-provider  c custom  ENTER run  q quit"
    )
    stdscr.addnstr(1, 0, legend[: w - 1], w - 1, curses.A_DIM)

    last_provider = None
    row = 3
    for idx, e in enumerate(entries):
        if row >= h - 2:
            break
        if e.provider != last_provider:
            stdscr.addnstr(row, 0, f"── {e.provider} ──", w - 1, curses.A_DIM)
            row += 1
            last_provider = e.provider
        marker = "[x]" if e.selected else "[ ]"
        line = f"  {marker} {e.model}"
        attr = curses.A_REVERSE if idx == cursor else curses.A_NORMAL
        stdscr.addnstr(row, 0, line[: w - 1], w - 1, attr)
        row += 1

    sel_count = sum(1 for e in entries if e.selected)
    footer = f"Selected: {sel_count} / {len(entries)}"
    stdscr.addnstr(h - 2, 0, footer, w - 1, curses.A_BOLD)
    if message:
        stdscr.addnstr(h - 1, 0, message[: w - 1], w - 1, curses.A_DIM)
    stdscr.refresh()


def _prompt_custom(stdscr) -> Optional[tuple[str, str]]:
    h, w = stdscr.getmaxyx()
    win = curses.newwin(5, max(40, min(w - 4, 80)), h // 2 - 2, 2)
    win.box()
    win.addnstr(0, 2, " add custom provider:model (ESC cancels) ", w - 4)
    win.addnstr(2, 2, "> ", w - 4)
    win.refresh()
    curses.echo()
    curses.curs_set(1)
    try:
        s = win.getstr(2, 4, 120).decode("utf-8", errors="replace").strip()
    finally:
        curses.noecho()
        curses.curs_set(0)
    if not s or ":" not in s:
        return None
    provider, _, model = s.partition(":")
    provider = provider.strip()
    model = model.strip()
    if not provider or not model:
        return None
    return provider, model


def _run(stdscr, entries: list[Entry]) -> Optional[list[Entry]]:
    curses.curs_set(0)
    cursor = 0
    message = ""
    while True:
        _draw(stdscr, entries, cursor, message)
        message = ""
        ch = stdscr.getch()
        if ch in (curses.KEY_UP, ord("k")):
            cursor = (cursor - 1) % len(entries)
        elif ch in (curses.KEY_DOWN, ord("j")):
            cursor = (cursor + 1) % len(entries)
        elif ch == ord(" "):
            entries[cursor].selected = not entries[cursor].selected
        elif ch == ord("a"):
            provider = entries[cursor].provider
            same = [e for e in entries if e.provider == provider]
            new_state = not all(e.selected for e in same)
            for e in same:
                e.selected = new_state
        elif ch == ord("c"):
            res = _prompt_custom(stdscr)
            if res:
                provider, model = res
                # Insert just after the last entry of that provider; if the
                # provider is new, append.
                last = -1
                for i, e in enumerate(entries):
                    if e.provider == provider:
                        last = i
                new = Entry(provider=provider, model=model, selected=True)
                if last >= 0:
                    entries.insert(last + 1, new)
                    cursor = last + 1
                else:
                    entries.append(new)
                    cursor = len(entries) - 1
                message = f"added {new.label}"
            else:
                message = "custom entry must be 'provider:model'"
        elif ch in (10, 13, curses.KEY_ENTER):
            if not any(e.selected for e in entries):
                message = "select at least one model (space) before pressing enter"
                continue
            return entries
        elif ch in (27, ord("q")):
            return None


def pick_models(default_entries: list[Entry]) -> Optional[list[Entry]]:
    """Run the curses picker; returns the list with ``selected`` flags set,
    or ``None`` if the user cancelled."""
    if not default_entries:
        raise ValueError("no model entries provided to picker")
    return curses.wrapper(_run, default_entries)
