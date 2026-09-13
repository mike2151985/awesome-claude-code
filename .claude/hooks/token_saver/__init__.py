"""Token Saver — a background token-economy layer for Claude Code.

The package has three moving parts, all of which run without being asked:

  hook.py    Claude Code hooks (PreToolUse / PostToolUse / SessionStart). They
             redirect token-expensive tool calls to exactly-equivalent cheap
             ones and keep the savings ledger.
  ctx.py     The `ctx` CLI: deterministic, loss-announcing compressors for the
             output that actually eats context (file dumps, greps, test runs).
  daemon.py  A tiny always-on background process that keeps the repo map warm
             and garbage-collects the cache.

Design rule for every line in here: *never lose information silently*. Output
is compressed only when the full data stays one documented command away, and
every truncation prints where the rest is.
"""

__all__ = ["config", "core", "rules", "compress", "hook", "ctx", "daemon"]
__version__ = "1.0.0"
