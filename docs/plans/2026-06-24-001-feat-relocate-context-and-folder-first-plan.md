---
title: "feat: relocate context-on-demand + folder-first default ordering"
date: 2026-06-24
type: feat
depth: standard
status: planned
---

# feat: `--relocate` context-on-demand + folder-first default ordering

## Summary

Two bounded enhancements to `claude-browse`, building on the `--relocate` flag shipped in `f5364aa`:

- **Feature A — context on demand for `--relocate`.** A relocated thread currently starts in the new folder with only a compact brief (recent ~10 turns + work-state restart card). Make the *full* original transcript available **on demand**: add a provenance block naming the previous folder and the transcript file path, and grant the fresh session scoped **read** access to that transcript's directory via `--add-dir`. Do not force the whole transcript inline, and do not grant access to the original source/project folder.
- **Feature B — folder-first default ordering.** When `claude-browse` opens before any query is typed, threads from the **current directory** (or a subdirectory) float to the top, ordered by recency, with all other threads following by recency. Uses canonicalized path comparison so the `/Users/Shamanth` vs `/Users/shamanth` casing split does not hide matches.

Both are additive and must not regress native resume, normal cross-folder/cross-vendor handoff, `--here`, or `--all`.

---

## Problem Frame

`--relocate` resumes a thread in the current directory by grafting a compact brief into a fresh session through the handoff path (`_continue_in_provider` → `write_import_file` → `build_import_markdown`). This is deliberate — native `claude --resume <id>` is bound to the thread's original project directory and cannot cross folders. The tradeoff is that deep history is lost: the brief carries recent turns + a restart card, not the whole conversation. The original transcript `.jsonl` still exists on disk and the session row already carries its absolute `path`, so the fidelity is recoverable **on demand** without bloating the first turn.

Separately, the pre-search list is ordered by global last-activity. Someone who opens `claude-browse` inside a specific project (e.g. `~/personal-ops/family`) must scan or search to find that project's threads even though "the work I was just doing here" is the dominant case. Ordering current-folder threads first makes the common case zero-effort.

---

## Requirements

- **R1.** When (and only when) a thread is relocated, the import brief includes a provenance block stating the previous folder and the absolute path to the full original transcript, with guidance to read it for detail beyond the recent turns.
- **R2.** A relocated session is granted **read** access to the directory containing the original transcript (so it can actually open the file), via the existing `--add-dir` mechanism.
- **R3.** A relocated session is **not** auto-granted access to the original source/project folder (`cwd`) — that would re-attach read+write to the folder being left.
- **R4.** The inline recent-turns window for the relocate brief is widened from 10 to ~25 turns.
- **R5.** Non-relocate handoff (cross-vendor, cross-folder "open in target", topic re-entry) is unchanged: no provenance block, no extra `--add-dir`, original 10-turn window.
- **R6.** On open, before any query, current-directory threads (cwd equal to or under the launch directory) sort to the top by recency; all other threads follow by recency.
- **R7.** Folder matching is canonicalized on both sides (`canonicalize_path(stored_cwd)` vs `canonicalize_path(os.getcwd())`) so casing/host differences still match.
- **R8.** Folder-first ordering applies only to the initial unfiltered list. Once a query is typed, `fts.search_ranked` governs unchanged. `--here` (filter) and `--all` (limit) are not regressed.

---

## Key Technical Decisions

- **KTD1 — thread a `relocate` boolean through the handoff, do not branch on `source==target`.** The brief/`--add-dir` changes key off the user's `--relocate` intent, not provider identity. Pass `relocate` from `_open_in_target_provider` → `_continue_in_provider` → `write_import_file` → `build_import_markdown`. Keeps the normal handoff path (R5) byte-identical.
- **KTD2 — reuse `--add-dir` for transcript read access; extend `handoff_cmd` to take extra dirs.** `ProviderSpec.handoff_cmd` already emits `add_dir_flag` for the import temp dir. Generalize it to accept additional directories and pass the transcript's parent dir. `--add-dir` is the provider-native, scoped grant; no new mechanism. Providers without `add_dir_flag` (none today, but defensively) simply omit it and still get the inline provenance text.
- **KTD3 — only the transcript's *parent directory* is added, never `cwd`.** Satisfies R2 without R3's footgun. The transcript lives under `~/.claude/projects/<bucket>/`; adding that dir grants read of the history file, not the working tree being left.
- **KTD4 — folder-first ordering is a pure post-query reordering in `main()`, not a SQL/index change.** `fts.list_recent` already returns recency-ordered rows; partition that list into (under-cwd, others) preserving order, then concatenate. No schema/`SCHEMA_VERSION` change, no effect on search ranking. Implemented as a small pure helper for testability.
- **KTD5 — canonicalize before comparing (R7).** Use the existing `canonicalize_path` (providers/common.py), which already folds `/Users/<user>` and lowercase `/users/<user>` to `~`. A relocated/launched path under a different casing still matches its stored sessions.

---

## Implementation Units

### U1. Relocate provenance block + wider window in the import brief

**Goal:** When relocating, the handoff brief gains a provenance block (previous folder + full transcript path + read-it guidance) and uses a ~25-turn window. Normal handoff is unchanged.

**Requirements:** R1, R4, R5.

**Dependencies:** none.

**Files:**
- `claude_browse/core.py` — `build_import_markdown` (add optional `relocate: bool` param; emit provenance block + use widened `recent_limit` when set), `write_import_file` (thread `relocate` through).
- `claude_browse/browse.py` — `_continue_in_provider` and `_open_in_target_provider` (accept and forward `relocate`).
- `tests/test_core.py`, `tests/test_browse.py` — coverage.

**Approach:** Add `relocate: bool = False` (keyword-only) to `build_import_markdown` and `write_import_file`. When `relocate` is true and the session carries `path`/`cwd`, insert a block after the existing header lines:

```
## Resuming Here (relocated)
- Previous folder: `<cwd>`
- Full original transcript: `<path>` — read it if you need detail beyond the recent turns below.
```

When `relocate` is true, build work-state with `recent_limit=25` instead of 10. `_continue_in_provider` already builds the prompt that says "continue the work in this directory"; it forwards `relocate` to `write_import_file`/`build_import_markdown`. The existing `relocate` parameter on `_open_in_target_provider` (added in `f5364aa`) is forwarded into `_continue_in_provider`.

**Patterns to follow:** mirror the existing header-line construction in `build_import_markdown` (the `- Source app:` / `- Original folder:` block) and the `reenter_topic` keyword-threading already present.

**Test scenarios:**
- `build_import_markdown(session, "claude", relocate=True)` output contains the previous folder path and the absolute transcript path and the "read it" guidance string.
- `build_import_markdown(session, "claude", relocate=False)` (default) contains **no** provenance block and is unchanged from current output (golden-substring check on a couple of stable lines).
- With `relocate=True`, `build_work_state` is invoked with `recent_limit=25` (assert via a monkeypatched/captured call or by counting rendered recent turns when the session has >10 turns).
- A session missing `path` does not crash with `relocate=True` (provenance block omits the transcript line or is skipped gracefully).

### U2. Scoped transcript read access via `--add-dir`

**Goal:** A relocated session is launched with `--add-dir <transcript-dir>` so it can open the full transcript; the source/project `cwd` is **not** added.

**Requirements:** R2, R3.

**Dependencies:** U1 (shares the relocate thread-through).

**Files:**
- `claude_browse/providers/base.py` — `handoff_cmd` (accept extra directories alongside `import_dir`).
- `claude_browse/browse.py` — `_continue_in_provider` (when `relocate`, compute the transcript dir from `session["path"]` and pass it as an extra add-dir).
- `tests/test_providers.py`, `tests/test_browse.py` — coverage.

**Approach:** Extend `ProviderSpec.handoff_cmd(import_dir, prompt, yolo, extra_dirs=())` to emit one `add_dir_flag <dir>` pair per directory (import dir first, then each extra dir), de-duplicating and skipping falsy/empty entries. In `_continue_in_provider`, when `relocate` and `session.get("path")`, set `extra_dirs=[os.path.dirname(session["path"])]`; otherwise `extra_dirs=()`. Never include `cwd` (R3).

**Patterns to follow:** the current `handoff_cmd` add-dir emission (`if self.add_dir_flag and import_dir: cmd.extend([self.add_dir_flag, import_dir])`).

**Test scenarios:**
- `handoff_cmd(import_dir, prompt, yolo=False, extra_dirs=["/x/proj/.claude/projects/bucket"])` emits both the import dir and the extra dir as `--add-dir` pairs, import dir first.
- `handoff_cmd(..., extra_dirs=())` emits exactly the current single import-dir behavior (no regression, R5).
- `handoff_cmd` with a falsy/empty extra dir skips it (no empty `--add-dir`).
- `_continue_in_provider` with `relocate=True` and a session `path` of `~/.claude/projects/<bucket>/<id>.jsonl` results in the bucket directory being passed as an extra add-dir, and the session `cwd` is **not** among the added dirs (assert cwd absent — R3).
- `_continue_in_provider` with `relocate=False` passes no extra dirs.

### U3. Folder-first default ordering (pre-search)

**Goal:** On open, current-folder threads sort first by recency; others follow by recency. Search, `--here`, `--all` unaffected.

**Requirements:** R6, R7, R8.

**Dependencies:** none (independent of U1/U2).

**Files:**
- `claude_browse/browse.py` — `main()` initial-list construction (~line 1344, after `fts.list_recent(...)` and the existing `cwd_filter` block); add a small pure helper `_folder_first_order(sessions, current_cwd)`.
- `tests/test_browse.py` — coverage.

**Approach:** Add `_folder_first_order(sessions, current_cwd)`: canonicalize `current_cwd` once; partition `sessions` (already recency-ordered from `list_recent`) into `under_cwd` (canonicalized stored cwd equals or starts with canonicalized current cwd + `/`) and `rest`, preserving each partition's existing order; return `under_cwd + rest`. In `main()`, apply it to `initial` **only when `cwd_filter` is not set** (i.e. not `--here`) and a query has not been typed (the initial list is always pre-query). The `--here` path keeps its existing filter behavior. Search-time ordering (`fts.search_ranked`) is untouched.

**Patterns to follow:** the existing `--here` filter line `initial = [r for r in initial if (r.get("cwd") or "").startswith(cwd_filter)]` — same `r.get("cwd")` access, but canonicalized and partitioning instead of filtering.

**Test scenarios:**
- Given sessions in `~/personal-ops/family`, `~/team-operations`, and `~/personal-ops/family/sub`, with the launch cwd `~/personal-ops/family`: both family and family/sub sessions appear before team-operations, and within the family group recency order is preserved.
- Subdirectory threads count as "under" the current folder (the `family/sub` case above).
- Casing difference: stored cwd `/users/shamanth/personal-ops/family` (lowercase) matches launch cwd `/Users/Shamanth/personal-ops/family` (both canonicalize to `~/personal-ops/family`).
- Non-current-folder threads still appear (after the current-folder group), none dropped — output length equals input length.
- Empty/missing stored cwd rows are treated as "rest", not crashed on.
- `--here` still filters (folder-first helper not applied / no double-effect); `--all` still controls limit.

---

## Scope Boundaries

In scope: the three units above.

Out of scope / non-goals:
- Inlining the full transcript into the first turn (deliberately rejected — token blowup, context-window risk, defeats the compact brief).
- Auto-`--add-dir` of the original source/project folder (R3 footgun).
- Any change to search-time ranking (`fts.search_ranked`) or the FTS index schema.
- Changing native-resume behavior.

### Deferred to Follow-Up Work
- An in-picker keybinding for relocate (today `--relocate` is a launch flag); a per-thread "relocate here" fzf binding could come later.
- Making the relocate recent-turns window user-configurable (hardcoded ~25 for now).

---

## Risks & Dependencies

- **Upstream drift:** the dense-embeddings merge reshaped `fts.py`/`work_state.py`. Mitigation: U3 touches only `main()` ordering and a pure helper; U1 keys off `build_work_state`'s public `recent_limit` arg, which is stable. Run the full suite (260 tests) after each unit.
- **`handoff_cmd` signature change (U2):** other callers of `handoff_cmd` must keep working. Mitigation: `extra_dirs` is an optional kwarg defaulting to `()`; existing call sites are unaffected. Grep for all callers before landing.
- **`os.getcwd()` after chdir:** for relocate the process stays in the launch dir (the `f5364aa` change skips the chdir); folder-first ordering reads `os.getcwd()` at list-build time, before any chdir, so the two features do not interact.

---

## Verification

- New + existing tests green via the dev venv: `python3 -m venv /tmp/cbvenv_lfg && /tmp/cbvenv_lfg/bin/pip install -e ".[dev]" && /tmp/cbvenv_lfg/bin/python -m pytest -q`.
- `ruff check` clean on changed files.
- Manual sanity: `claude-browse --help` still renders; relocate help line intact.
- CE code review reaches ≥95% confidence per feature and overall before landing.
