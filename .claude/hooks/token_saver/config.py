"""Tunables for the token saver.

Every threshold here is deliberately conservative: below them nothing is
touched, so small reads and short commands never pay an interception cost.
Override any value from the environment with TOKEN_SAVER_<NAME>, e.g.
TOKEN_SAVER_BIG_FILE_LINES=800.
"""

from __future__ import annotations

import os

# --- General -----------------------------------------------------------------

#: Characters per token. Rough but stable enough for a savings ledger.
CHARS_PER_TOKEN = 4

#: Kill switch. `ctx off` writes the flag file; `ctx on` removes it.
DISABLE_ENV = "TOKEN_SAVER"
DISABLE_FLAG = "disabled"

# --- What counts as "too big to dump" ----------------------------------------

#: A file longer than this is outlined + windowed instead of dumped whole.
BIG_FILE_LINES = 400

#: ...or bigger than this many bytes (covers minified single-line files).
BIG_FILE_BYTES = 24_000

#: Lines shown from the head of a big file before the outline takes over.
HEAD_LINES = 60

#: Lines shown from the tail of a successful command's log.
TAIL_LINES = 25

#: Grep: matches kept per file before the rest are counted instead of printed.
GREP_MATCHES_PER_FILE = 8

#: Grep/read: a single line longer than this is clipped (with a marker).
MAX_LINE_CHARS = 400

#: Directory map depth.
MAP_DEPTH = 3

# --- Re-read deduplication ----------------------------------------------------

#: A repeat read is only intercepted if the earlier identical read happened
#: within this many tool calls — far enough back and it may have been
#: compacted out of the context window, so re-reading is legitimate.
DEDUPE_WINDOW_CALLS = 40

#: ...and within this many seconds.
DEDUPE_WINDOW_SECONDS = 45 * 60

# --- Daemon -------------------------------------------------------------------

#: Seconds between background maintenance passes.
DAEMON_INTERVAL = 30

#: The daemon exits after this long without a live session touching the cache.
DAEMON_IDLE_EXIT = 6 * 60 * 60

#: Cached artifacts older than this are deleted by the daemon.
CACHE_TTL_SECONDS = 7 * 24 * 60 * 60

# --- Noise that is never worth a token ---------------------------------------

#: Paths that are generated, vendored or minified: never dumped in full.
GENERATED_PATTERNS = (
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock",
    "Cargo.lock", "composer.lock", "go.sum", "Gemfile.lock", "uv.lock",
    ".min.js", ".min.css", ".map", ".bundle.js", ".snap",
)

#: Directories that are never worth walking into.
SKIP_DIRS = frozenset({
    ".git", "node_modules", "venv", ".venv", "__pycache__", ".mypy_cache",
    ".pytest_cache", ".ruff_cache", "dist", "build", ".next", ".nuxt",
    "coverage", "htmlcov", ".tox", "target", ".gradle", ".idea", ".vscode",
    "vendor", ".terraform", ".cache",
})

#: Build/test chatter that carries no information for the model.
NOISE_PREFIXES = (
    "Requirement already satisfied:",
    "npm WARN deprecated",
    "npm warn deprecated",
    "warning \" > ",
    "Downloading ",
    "Collecting ",
    "Using cached ",
    "info There appears to be trouble",
    "Fetching ",
)


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(f"TOKEN_SAVER_{name}")
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


# Apply environment overrides for the numeric knobs.
for _name in (
    "CHARS_PER_TOKEN", "BIG_FILE_LINES", "BIG_FILE_BYTES", "HEAD_LINES",
    "TAIL_LINES", "GREP_MATCHES_PER_FILE", "MAX_LINE_CHARS", "MAP_DEPTH",
    "DEDUPE_WINDOW_CALLS", "DEDUPE_WINDOW_SECONDS", "DAEMON_INTERVAL",
    "DAEMON_IDLE_EXIT", "CACHE_TTL_SECONDS",
):
    globals()[_name] = _int_env(_name, globals()[_name])
del _name
