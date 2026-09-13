"""Claude Code hook entry points.

Contract with the session: a hook either stays completely silent (exit 0, no
output — zero tokens) or denies one tool call with a message naming the exact
cheaper command. It never asks the user anything, never mutates the tool input,
and any internal error is swallowed so a bug here can never break a session.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from . import config, core, rules

#: Bash programs whose output depends only on the files they name, so an
#: identical repeat with identical files is an identical answer.
PURE_READERS = frozenset({
    "cat", "head", "tail", "sed", "awk", "nl", "wc", "grep", "rg", "jq",
    "cut", "sort", "uniq", "md5sum", "sha1sum", "sha256sum", "file", "stat",
})

#: Read is also used for images/PDF/notebooks, where our advice does not apply.
TEXTUAL_SUFFIXES_EXCLUDED = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp", ".pdf", ".ipynb", ".svg"})


def _ago(calls: int) -> str:
    return "1 tool call ago" if calls == 1 else f"{calls} tool calls ago"


def _deny(reason: str) -> None:
    """Block one tool call and tell Claude precisely what to run instead."""
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }))


def _payload() -> dict[str, Any]:
    try:
        return json.loads(sys.stdin.read() or "{}")
    except ValueError:
        return {}


def _memory(event: dict[str, Any]) -> core.SessionMemory:
    return core.SessionMemory(str(event.get("session_id", "")))


# --- PreToolUse: Bash ---------------------------------------------------------


def pre_bash(event: dict[str, Any]) -> None:
    command = str(event.get("tool_input", {}).get("command", "")).strip()
    if not command:
        return
    memory = _memory(event)
    memory.bump()

    # 1. Byte-identical repeat of a read-only command over unchanged files.
    program = Path(command.split()[0]).name
    key = f"bash:{command}"
    fingerprints: dict[str, str | None] = {}
    if program in PURE_READERS:
        files = core.existing_paths(command.replace("|", " ").split())
        fingerprints = {str(path): core.fingerprint(path) for path in files}
        previous = memory.lookup(key, fingerprints) if fingerprints else None
        if previous:
            core.record("dedupe.bash", saved_tokens=int(previous.get("tokens", 0)), detail=command)
            memory.save()
            _deny(
                f"[token-saver:repeat] You ran this exact command {_ago(memory.calls - int(previous['call']))} "
                "and none of the files changed since, so the output is already in "
                "this conversation — scroll up instead of paying for it twice.\n"
                "If it has since been compacted out of your context, re-run it as "
                f"`TOKEN_SAVER=off {command}` and it will go through untouched."
            )
            return

    # 2. A cheaper, information-equivalent form of an expensive command.
    redirect = rules.analyze(command)
    if redirect:
        core.record(f"rule.{redirect.rule}", saved_tokens=redirect.saved_tokens, detail=command)
        memory.save()
        _deny(redirect.message())
        return

    # Allowed: remember it, so a later identical repeat can be recognised. Only
    # commands that actually ran are remembered — a denied one delivered nothing.
    if fingerprints:
        memory.remember(key, fingerprints=fingerprints, tokens=0)
    else:
        memory.save()


# --- PreToolUse: Read ---------------------------------------------------------


def pre_read(event: dict[str, Any]) -> None:
    tool_input = event.get("tool_input", {})
    raw_path = str(tool_input.get("file_path", ""))
    if not raw_path:
        return
    path = Path(raw_path)
    if path.suffix.lower() in TEXTUAL_SUFFIXES_EXCLUDED or not path.is_file():
        return

    memory = _memory(event)
    memory.bump()
    offset, limit = tool_input.get("offset"), tool_input.get("limit")
    fingerprints = {str(path): core.fingerprint(path)}

    # 1. Same file, same window, unchanged bytes, recent: already in context.
    key = f"read:{path}:{offset}:{limit}"
    previous = memory.lookup(key, fingerprints)
    if previous:
        core.record("dedupe.read", saved_tokens=int(previous.get("tokens", 0)), detail=str(path))
        memory.save()
        _deny(
            f"[token-saver:repeat] {path.name} was read {_ago(memory.calls - int(previous['call']))} "
            "and has not changed since — that content is still in your context.\n"
            "If you genuinely need it again (e.g. after a compaction), fetch it through "
            f"Bash instead: `sed -n '1,200p' {path}`."
        )
        return

    # 2. Whole-file Read of a big or generated file: outline first, then windows.
    if offset is None and limit is None:
        try:
            size = path.stat().st_size
            lines = sum(1 for _ in path.open("rb"))
        except OSError:
            return
        if core.is_generated(str(path)) or size > config.BIG_FILE_BYTES or lines > config.BIG_FILE_LINES:
            core.record("rule.big-file-read", saved_tokens=max(0, core.est_tokens("x" * size) - 900), detail=str(path))
            _deny(
                f"[token-saver:big-file] {path.name} is {lines} lines / {size} bytes. Reading it whole "
                "spends a large slice of the context window on text you mostly will not use.\n"
                "Get the same information in two cheap steps:\n"
                f"    {core.ctx_command()} read {path}\n"
                "  (prints the outline + head, and tells you the exact command for any range)\n"
                "Then Read with offset/limit for the part you actually need — that form is never blocked."
            )
            return

    memory.remember(key, fingerprints=fingerprints, tokens=0)


# --- PostToolUse: accounting --------------------------------------------------


def post_tool(event: dict[str, Any]) -> None:
    """Silent. Records what the call actually cost and sizes the dedupe entries."""
    tool = str(event.get("tool_name", ""))
    response = event.get("tool_response")
    text = response if isinstance(response, str) else json.dumps(response, default=str)
    tokens = core.est_tokens(text)
    core.record(f"spent.{tool}", spent_tokens=tokens)
    core.touch_activity()

    memory = _memory(event)
    tool_input = event.get("tool_input", {})
    if tool == "Read" and tool_input.get("file_path"):
        key = f"read:{tool_input['file_path']}:{tool_input.get('offset')}:{tool_input.get('limit')}"
    elif tool == "Bash" and tool_input.get("command"):
        key = f"bash:{str(tool_input['command']).strip()}"
    else:
        return
    entry = memory.data.get("entries", {}).get(key)
    if entry is not None:
        entry["tokens"] = tokens
        memory.save()


# --- SessionStart: bring the background up ------------------------------------


def session_start(event: dict[str, Any]) -> None:
    from . import daemon

    core.touch_activity()
    daemon.ensure_running()
    _memory(event).save()

    ctx = core.ctx_command()
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": (
                "Token saver active (hooks + background daemon). Cheap equivalents, all of which "
                "state what they omitted and how to get it:\n"
                f"  {ctx} read FILE [START END] | outline FILE | grep PAT [PATH] | map | diff | json FILE\n"
                f"  {ctx} run -- <build/test cmd>   # full log on disk, only failures printed\n"
                f"  {ctx} stats | status | off      # savings ledger; `off` disables everything\n"
                "A blocked call always names the exact replacement command; prefix any command with "
                "`TOKEN_SAVER=off ` to bypass."
            ),
        }
    }))


HANDLERS = {
    "pre-bash": pre_bash,
    "pre-read": pre_read,
    "post-tool": post_tool,
    "session-start": session_start,
}


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] not in HANDLERS:
        return 0
    event = _payload()
    try:
        if not core.enabled():
            return 0
        HANDLERS[argv[0]](event)
    except Exception:  # noqa: BLE001 - a hook must never break the session
        try:
            core.record("hook.error", detail=repr(sys.exc_info()[1]))
        except Exception:  # noqa: BLE001
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
