"""Offline, fail-closed admission for connector commits (never strategy input)."""
import datetime as dt
import os
from pathlib import Path
import re
import subprocess

TRIGGERS = {s: f".automation-triggers/{s}.txt" for s in ("morning", "closing")}


def admitted(session, before, after, now=None):
    if session not in TRIGGERS or not all(re.fullmatch(r"[0-9a-f]{40}", x or "")
                                          and x != "0" * 40 for x in (before, after)):
        return False
    # Inspect the complete push, not the truncated event.commits file list.
    changed = subprocess.check_output(
        ["git", "diff", "--name-status", "--no-renames", before, after], text=True)
    if changed.strip() != f"M\t{TRIGGERS[session]}":
        return False  # bootstrap, deletion, both triggers, or any code change
    content = subprocess.check_output(
        ["git", "show", f"{after}:{TRIGGERS[session]}"], text=True)
    if not re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,6})?(?:Z|\+00:00)\n?", content):
        return False
    try:
        stamp = dt.datetime.fromisoformat(content.strip().replace("Z", "+00:00"))
    except ValueError:
        return False
    age = ((now or dt.datetime.now(dt.timezone.utc)) - stamp).total_seconds()
    return -60 <= age <= 20 * 60


def main():
    allowed = admitted(os.environ["SESSION"], os.environ["BEFORE"], os.environ["AFTER"])
    with Path(os.environ["GITHUB_OUTPUT"]).open("a", encoding="utf-8") as out:
        out.write(f"admitted={str(allowed).lower()}\n")
    print(f"Automation trigger admitted: {allowed}")


if __name__ == "__main__":
    main()
