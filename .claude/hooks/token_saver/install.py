"""Install the token saver globally, so every project gets it.

The project-local copy (.claude/settings.json in this repo) already works for
this repo. `--global` copies the package into ~/.claude/hooks and merges the
same four hook entries into ~/.claude/settings.json, leaving every other
setting untouched and writing a timestamped backup first.

    python3 .claude/hooks/token_saver/install.py --global
    python3 .claude/hooks/token_saver/install.py --status
    python3 .claude/hooks/token_saver/install.py --uninstall
"""

from __future__ import annotations

import json
import shutil
import sys
import time
from pathlib import Path
from typing import Any

#: Marker that identifies the entries this installer owns, so --uninstall can
#: remove exactly those and nothing a human added by hand.
MARKER = "token-saver-hook"

EVENTS = [
    ("SessionStart", None, "session-start"),
    ("PreToolUse", "Bash", "pre-bash"),
    ("PreToolUse", "Read", "pre-read"),
    ("PostToolUse", "Bash|Read|Grep|Glob|WebFetch", "post-tool"),
]


def home_claude() -> Path:
    return Path.home() / ".claude"


def source_root() -> Path:
    """.claude/hooks in the checkout this file lives in."""
    return Path(__file__).resolve().parent.parent


def _entry(target: Path, action: str, matcher: str | None) -> dict[str, Any]:
    hook = {"type": "command", "command": f'python3 "{target}" {action}', "timeout": 10}
    entry: dict[str, Any] = {"hooks": [hook]}
    if matcher:
        entry["matcher"] = matcher
    return entry


def install_global() -> int:
    destination = home_claude() / "hooks"
    destination.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source_root() / "token_saver", destination / "token_saver", dirs_exist_ok=True)
    for launcher in ("ctx", "token-saver-hook"):
        shutil.copy2(source_root() / launcher, destination / launcher)
        (destination / launcher).chmod(0o755)

    settings_path = home_claude() / "settings.json"
    settings: dict[str, Any] = {}
    if settings_path.exists():
        backup = settings_path.with_name(f"settings.json.bak-{int(time.time())}")
        shutil.copy2(settings_path, backup)
        try:
            settings = json.loads(settings_path.read_text(encoding="utf-8"))
        except ValueError:
            print(f"! {settings_path} is not valid JSON; left alone. Backup: {backup}", file=sys.stderr)
            return 1
        print(f"backup: {backup}")

    hooks = settings.setdefault("hooks", {})
    target = destination / "token-saver-hook"
    for event, matcher, action in EVENTS:
        bucket = hooks.setdefault(event, [])
        bucket[:] = [item for item in bucket if MARKER not in json.dumps(item) or item.get("matcher") != matcher]
        bucket.append(_entry(target, action, matcher))

    settings_path.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
    print(f"installed: {destination}")
    print(f"wired    : {settings_path}")
    print("Active in every project from the next Claude Code session. Disable any time with `ctx off`.")
    print(f"Optional: ln -sf {destination / 'ctx'} ~/.local/bin/ctx")
    return 0


def uninstall_global() -> int:
    settings_path = home_claude() / "settings.json"
    if settings_path.exists():
        try:
            settings = json.loads(settings_path.read_text(encoding="utf-8"))
        except ValueError:
            print(f"! {settings_path} is not valid JSON; nothing removed.", file=sys.stderr)
            return 1
        hooks = settings.get("hooks", {})
        for event in list(hooks):
            hooks[event] = [item for item in hooks[event] if MARKER not in json.dumps(item)]
            if not hooks[event]:
                del hooks[event]
        if not hooks:
            settings.pop("hooks", None)
        settings_path.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
        print(f"unwired: {settings_path}")
    shutil.rmtree(home_claude() / "hooks" / "token_saver", ignore_errors=True)
    for launcher in ("ctx", "token-saver-hook"):
        (home_claude() / "hooks" / launcher).unlink(missing_ok=True)
    print("removed global copy; project-local installs are untouched.")
    return 0


def status() -> int:
    settings_path = home_claude() / "settings.json"
    wired = settings_path.exists() and MARKER in settings_path.read_text(encoding="utf-8")
    print(f"global copy : {(home_claude() / 'hooks' / 'token_saver').exists()}")
    print(f"global hooks: {wired} ({settings_path})")
    print(f"this repo   : {(source_root().parent / 'settings.json').exists()}")
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--global" in argv:
        return install_global()
    if "--uninstall" in argv:
        return uninstall_global()
    if "--status" in argv:
        return status()
    print(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(main())
