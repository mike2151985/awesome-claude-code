"""Shared plumbing: paths, the savings ledger, and per-session read memory.

Everything here is failure-tolerant on purpose. A hook that raises would
interrupt a real Claude Code session, so every public helper degrades to a
harmless default instead of propagating an exception.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from . import config

# --- Locations ----------------------------------------------------------------


def project_dir() -> Path:
    """The repo Claude Code is working in (CLAUDE_PROJECT_DIR when hooked)."""
    env = os.environ.get("CLAUDE_PROJECT_DIR")
    if env and Path(env).is_dir():
        return Path(env)
    here = Path.cwd().resolve()
    for candidate in (here, *here.parents):
        if (candidate / ".git").exists():
            return candidate
    return here


def state_dir() -> Path:
    """Cache/ledger root. Git-ignored; safe to delete at any time."""
    path = Path(os.environ.get("TOKEN_SAVER_STATE", project_dir() / ".claude" / ".token-saver"))
    try:
        path.mkdir(parents=True, exist_ok=True)
        (path / "sessions").mkdir(exist_ok=True)
        (path / "logs").mkdir(exist_ok=True)
        (path / "cache").mkdir(exist_ok=True)
    except OSError:
        pass
    return path


def ctx_command() -> str:
    """How to invoke the ctx launcher from a shell, wherever this is installed.

    Works for a project-local checkout and for a global ~/.claude install alike,
    because it resolves the launcher that sits next to this package.
    """
    launcher = Path(__file__).resolve().parent.parent / "ctx"
    if launcher.exists():
        return f'python3 "{launcher}"'
    return "python3 -m token_saver"


def enabled() -> bool:
    """False when the user switched the saver off (`ctx off`, or env=off/0)."""
    env = os.environ.get(config.DISABLE_ENV, "").strip().lower()
    if env in {"off", "0", "false", "no"}:
        return False
    return not (state_dir() / config.DISABLE_FLAG).exists()


# --- Small utilities ----------------------------------------------------------


def est_tokens(text: str | bytes | None) -> int:
    """Rough token count. Consistent across the ledger, which is what matters."""
    if not text:
        return 0
    if isinstance(text, bytes):
        length = len(text)
    else:
        length = len(text)
    return max(1, length // config.CHARS_PER_TOKEN)


def read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def write_json(path: Path, payload: Any) -> None:
    """Atomic-ish write: temp file + replace, so a crash never truncates state."""
    try:
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        pass


def fingerprint(path: Path) -> str | None:
    """Cheap identity for a file: size + mtime. No hashing of large files."""
    try:
        st = path.stat()
    except OSError:
        return None
    return f"{st.st_size}:{int(st.st_mtime_ns)}"


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:16]


def is_generated(path: str) -> bool:
    lowered = path.lower()
    return any(marker.lower() in lowered for marker in config.GENERATED_PATTERNS)


# --- Savings ledger -----------------------------------------------------------


def ledger_path() -> Path:
    return state_dir() / "ledger.jsonl"


def record(kind: str, saved_tokens: int = 0, spent_tokens: int = 0, detail: str = "") -> None:
    """Append one ledger line. `saved` is what an interception avoided sending."""
    try:
        line = json.dumps({
            "ts": int(time.time()),
            "kind": kind,
            "saved": int(saved_tokens),
            "spent": int(spent_tokens),
            "detail": detail[:200],
        })
        with ledger_path().open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except OSError:
        pass


@dataclass
class Totals:
    saved: int = 0
    spent: int = 0
    events: int = 0
    by_kind: dict[str, int] | None = None


def totals(since: int = 0) -> Totals:
    result = Totals(by_kind={})
    try:
        with ledger_path().open(encoding="utf-8") as handle:
            for line in handle:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if row.get("ts", 0) < since:
                    continue
                result.saved += int(row.get("saved", 0))
                result.spent += int(row.get("spent", 0))
                result.events += 1
                kind = str(row.get("kind", "?"))
                assert result.by_kind is not None
                result.by_kind[kind] = result.by_kind.get(kind, 0) + int(row.get("saved", 0))
    except OSError:
        pass
    return result


# --- Per-session memory of what was already read ------------------------------


class SessionMemory:
    """What this session has already pulled into context, and when.

    Used to intercept a byte-identical re-read. The window in config.py keeps
    it honest: past it we assume the content may have been compacted away and
    let the read through.
    """

    def __init__(self, session_id: str) -> None:
        safe = "".join(char for char in session_id if char.isalnum() or char in "-_")[:64] or "nosession"
        self.path = state_dir() / "sessions" / f"{safe}.json"
        self.data: dict[str, Any] = read_json(self.path, {"calls": 0, "entries": {}})

    # -- bookkeeping
    def bump(self) -> int:
        self.data["calls"] = int(self.data.get("calls", 0)) + 1
        return self.data["calls"]

    @property
    def calls(self) -> int:
        return int(self.data.get("calls", 0))

    def remember(self, key: str, *, fingerprints: dict[str, str | None], tokens: int) -> None:
        self.data.setdefault("entries", {})[key] = {
            "call": self.calls,
            "ts": int(time.time()),
            "fp": fingerprints,
            "tokens": tokens,
        }
        self.save()

    def lookup(self, key: str, fingerprints: dict[str, str | None]) -> dict[str, Any] | None:
        """Return the earlier identical delivery, or None if it can't be reused."""
        entry = self.data.get("entries", {}).get(key)
        if not entry:
            return None
        if self.calls - int(entry.get("call", 0)) > config.DEDUPE_WINDOW_CALLS:
            return None
        if time.time() - float(entry.get("ts", 0)) > config.DEDUPE_WINDOW_SECONDS:
            return None
        if entry.get("fp") != fingerprints:  # any referenced file changed
            return None
        return entry

    def save(self) -> None:
        write_json(self.path, self.data)


def touch_activity() -> None:
    """Heartbeat the daemon watches to decide when it may exit."""
    try:
        (state_dir() / "activity").write_text(str(int(time.time())), encoding="utf-8")
    except OSError:
        pass


def last_activity() -> float:
    try:
        return float((state_dir() / "activity").read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return 0.0


def existing_paths(tokens: Iterable[str]) -> list[Path]:
    """Filter shell words down to the ones that are real files in the repo."""
    found: list[Path] = []
    root = project_dir()
    for token in tokens:
        if not token or token.startswith("-"):
            continue
        candidate = Path(token) if Path(token).is_absolute() else root / token
        try:
            if candidate.is_file():
                found.append(candidate)
        except OSError:
            continue
    return found
