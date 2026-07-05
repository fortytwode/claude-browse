# Lessons Learned

Hard-won knowledge from building and debugging claude-browse search.

## fzf field semantics are not what you think

### --with-nth blocks search on hidden fields
`--with-nth=1` transforms the line for display AND search. Fields hidden by `--with-nth` are completely unsearchable. The man page says it explicitly: "fzf doesn't allow searching against the hidden fields."

If you need to search data that shouldn't be visible, you CANNOT hide it with `--with-nth`. You need a different architecture.

### --with-nth + --nth interact on the TRANSFORMED line
When you combine `--with-nth=1` with `--nth=1,3`, the `--nth` field indices apply to the TRANSFORMED output (after `--with-nth`), not the original input. So `--nth=3` references a field that no longer exists after transformation.

### --tabstop=9999 hides content visually but fzf auto-scrolls to matches
Putting search data after a tab with `--tabstop=9999` makes it invisible when the user isn't searching. But when fzf highlights a match in the hidden area, it auto-scrolls horizontally to reveal it. The user sees the raw corpus text. Not usable.

### --exact vs fuzzy: hyphens are always literal
In both fuzzy and exact mode, hyphens in the query are treated as literal characters. `claude-browse` searches for the substring "claude-browse", not "claude" AND "browse". There is no built-in way to change this. Handle it in your own search logic.

### --filter mode differs from interactive mode
`fzf --filter` is non-interactive and skips many bindings (e.g., `change:transform-query` never fires). Tests passing with `--filter` do not guarantee interactive behavior. Always test interactively for interactive features.

## The right architecture: --disabled + change:reload

When fzf's built-in search doesn't fit your needs, bypass it entirely:

```
fzf --disabled --bind 'change:reload:python3 search.py {q}'
```

- `--disabled` turns off fzf's matching. All lines are shown as-is.
- `change:reload` calls your script on every keystroke with the current query.
- Your script handles ALL search logic (AND matching, hyphen-to-space, quote stripping, phrase matching).
- fzf only handles display, selection, preview, and keybindings.

This cleanly separates concerns and avoids every field/display/search interaction bug above.

## Corpus design: words vs phrases vs truncation

### Unique words lose phrase order
Extracting unique words from messages (`set()` + `sorted()`) gives keyword coverage but destroys word order. "Claude browse" becomes `browse ... claude ...` (alphabetical). Phrase matching fails.

### Solution: snippets + words
Keep short snippets of original messages (preserves phrases) AND unique words (keyword coverage):
```python
snippets = [text[:100].lower() for text in messages[-50:]]
words = sorted(unique_words)
corpus = " | ".join(snippets) + " | " + " ".join(words)
```

### Truncation cuts late-session topics
A session with 76 messages where "browse" appears at message 61: any fixed truncation (1000, 2000, 3000 chars) risks cutting the relevant content. Using unique words avoids this since every word from every message is captured regardless of position.

## printf in bash is a format string
`printf "line1\n" "line2\n" "line3\n"` does NOT print three lines. The first argument is the format string; the rest are arguments to substitute. Without `%s`, extra arguments are ignored. Use `echo -e` or a heredoc for multi-line output.

## Test what the user actually sees
Non-interactive tests (`--filter`, Python simulations) can pass while interactive behavior fails. When debugging UI tools, build minimal interactive test scripts and have the user run them. One screenshot is worth a thousand `--filter` tests.

## "Newest" means most recently active, not most recently started

The original sort in `list_recent` ordered by session start time on purpose — the comment defended it as "users still think of newest as most recently started." That assumption was wrong in practice. The dominant browse use case is "where was I working recently," and a thread started weeks ago that you resumed today is more relevant than a fresh thread you abandoned an hour in. When in doubt about a sort key for a "recent" view, default to last activity (mtime, last message timestamp, etc.), not creation time. Creation time only wins when stability matters more than recency (e.g., "find that thing I started around April 10").

## Use SCHEMA_VERSION + drop-and-rebuild for derived caches

Adding a column to the FTS index DB could have meant writing a real ALTER TABLE migration. It didn't need to. The DB at `~/.claude/cache/claude-browse-index.db` is pure derived state — every row can be regenerated from the JSONL files in seconds. The cleanest pattern:

1. Bump `SCHEMA_VERSION` in code.
2. On open, compare the version stored in the `schema_version` table.
3. If it doesn't match, `DROP TABLE` everything and let `reindex()` rebuild from source.

This is not "cheating around migrations" — it's the right pattern for caches. Real ALTER TABLE migrations are for source-of-truth data where rebuilding is expensive or impossible. For 300 sessions, the rebuild took ~7 seconds. Don't write migration scaffolding you don't need.

## "It's just a disposable cache" does not excuse skipping concurrency design

The corollary the previous lesson hid: because the index was rebuildable, every failure got a *recovery* patch (self-heal on corruption, tolerate locks, don't rebuild on locks) and never a *prevention* fix. The result was a week with two corruption incidents, a ~1 GB write-ahead log nobody checkpointed, silent window deaths under concurrent launches, and a rebuild feedback loop where recovery itself was the heaviest writer. What ended the cycle was ordinary concurrency engineering, all cheap: a flock single-writer election on a sidecar lockfile (auto-released on SIGKILL), `wal_checkpoint(TRUNCATE)` after each write burst, `synchronous=NORMAL`, quarantine-by-rename instead of deleting a live WAL, and a per-host cache filename so file-sync between machines cannot interleave two hosts' pages into one SQLite file. If several processes open one database read-write on the hot path, design the writer topology on day one — "we can always rebuild" bounds the damage but also *hides* the bug until users hit it as slowness and vanished windows. And test contention with real subprocesses and real SIGKILLs: monkeypatched lock exceptions verified the message strings while the actual races corrupted the file.

## When tracking first AND last of something, watch the guard clause

The original `core.py` captured the session start timestamp with:

```python
if not timestamp and data.get("timestamp"):
    timestamp = data.get("timestamp")
```

Adding "last timestamp" required splitting this into two cases — the first-only guard for `timestamp`, plus an unconditional overwrite for `last_timestamp`:

```python
if data.get("timestamp"):
    if not timestamp:
        timestamp = data.get("timestamp")
    last_timestamp = data.get("timestamp")
```

Easy to write the second one wrong (e.g., gating it behind the same `if not` guard, leaving last_timestamp == timestamp forever). When refactoring "first X" into "first and last X," explicitly verify the last variable updates on every iteration, not just the first.
