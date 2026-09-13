# Token Saver

A background token-economy layer for Claude Code. It runs from session start to
session end without being asked, and its one rule is:

> **Spend fewer tokens for the same information — never less information.**

Nothing is ever dropped silently. Every compressed output states what it left
out and prints the exact command that retrieves it; every blocked tool call
comes back with a replacement command that answers the same question.

## What it does

| Layer | Mechanism | Effect |
| --- | --- | --- |
| `PreToolUse` (Bash) | Rewrites nothing, but denies a token-expensive command and names an equivalent cheap one | `cat README.md` → `ctx read README.md`; `git log` → `git log --oneline -n 20` |
| `PreToolUse` (Bash/Read) | Re-read deduplication | The same file, unchanged, already read a few calls ago is not paid for twice |
| `PreToolUse` (Read) | Big/generated file guard | Outline + head first, then windowed `Read(offset, limit)` — which is never blocked |
| `PostToolUse` | Silent accounting | Sizes every tool result so the ledger and the dedupe entries are real numbers |
| `SessionStart` | Starts the daemon, injects a ~6-line toolbox reminder | The cheap commands get used instead of rediscovered |
| Daemon | Background maintenance every 30 s | Keeps the repo map warm, prunes the cache, trims the ledger, exits after 6 h idle |

## The `ctx` CLI

```
ctx read FILE [START END]   whole small file, or outline + head + "how to get the rest"
ctx outline FILE...         definitions only, for choosing what to read
ctx grep PATTERN [PATH]     ripgrep-backed, capped per file, --all for everything
ctx map [PATH]              directory shape with file counts, ignored dirs pruned
ctx diff [git args]         --stat first, lockfiles excluded, full patch saved to disk
ctx run -- CMD              runs it, keeps the full log on disk, prints only the signal
ctx json FILE               structure and sizes instead of megabytes of values
ctx stats | status          the savings ledger; hooks/daemon state
ctx on | off                master switch
ctx daemon start|stop       the background process
```

`ctx run` is the big one: on success it prints the tail, on failure the opening
lines plus every error neighbourhood plus the tail, with `⋮ (N lines omitted)`
markers and a path to the complete, unfiltered log.

## Install

Already active in this repo via `.claude/settings.json`. To get it everywhere:

```bash
make saver-install          # copies into ~/.claude/hooks, merges ~/.claude/settings.json (backed up first)
make saver                  # status + savings
make saver-uninstall        # removes exactly the entries it added
ln -sf ~/.claude/hooks/ctx ~/.local/bin/ctx   # optional: plain `ctx`
```

## Turning it off

* One command: prefix it with `TOKEN_SAVER=off ` and it runs untouched.
* One session: `TOKEN_SAVER=off` in the environment.
* Everywhere: `ctx off` (or `make saver-off`), undone by `ctx on`.

## Why it cannot cost you quality

* **Denials carry the replacement.** A blocked call is answered with a command
  that returns the same facts; the model just runs that one instead.
* **Truncation always announces itself** with counts and the retrieval command —
  the full text stays on disk in `.claude/.token-saver/`.
* **Thresholds are conservative.** Files under 400 lines / 24 KB, bounded
  pipelines (`| head`, `| grep`, `> file`) and windowed `Read` calls are never
  touched, so ordinary work sees no interception at all.
* **Dedup has a window.** A repeat read is only intercepted within 40 tool calls
  and 45 minutes; past that the content may have been compacted away, so the
  re-read goes through.
* **The hooks cannot break a session.** Every entry point swallows its own
  errors and exits 0; the worst case is that nothing is saved.

## Tuning

Any threshold in `config.py` can be overridden from the environment:

```bash
TOKEN_SAVER_BIG_FILE_LINES=800 TOKEN_SAVER_DEDUPE_WINDOW_CALLS=20 claude
```

## Tests

```bash
make test                                  # includes tests/test_token_saver.py
python3 -m pytest tests/test_token_saver.py -q
```

State (ledger, logs, cached map, per-session memory) lives in
`.claude/.token-saver/` — git-ignored and safe to delete at any time.
