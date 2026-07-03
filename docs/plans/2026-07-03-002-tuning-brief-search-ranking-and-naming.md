# Tuning Brief: search ranking + session naming quality

**For: a fresh Claude Code session on SONNET (not Fable).** This is iterative,
eval-driven tuning — loops multiply tokens, so run the loop on the cheaper
model. Escalate to Fable only if you hit a decision this brief doesn't cover.
Written 2026-07-03 at the end of the Agent Board build session
(`claude --resume dfbda258-7bab-4637-b416-502697d36ed8` for full history —
but this brief is designed so you don't need it).

## Context in three sentences

Agent Board (claude_browse/board/) ships live session state + auto-naming +
Slack alerts; search (claude_browse/fts.py) had two identity-vs-evidence bugs
fixed on 2026-07-03 (commits `add20a3`, `bb0afab` — read both messages).
Both fixes established the governing rule: **judge the match, not the
session**. Do not weaken it; 376+ tests and four regression tests encode it.

## Task 1 — Ranking quality for common single-term queries

**Problem:** `search_ranked` for a high-frequency single word (e.g. `cfo`)
returns 60+ sessions; many match only via boilerplate (CLAUDE.md/skills-list
text embedded in imported sessions' `first_msg`). Real work sessions can
rank below boilerplate echoes.

**Do:**
1. Add today's live failure as graded eval cases (see `eval/` harness +
   `tests/test_eval_from_log.py` for the format): query `cfo` → session
   `7c983da8-0f75-45da-9f31-1595f7c05d7a` should rank top-5 (it contains
   'CFO' 578 times, same-day). Add 2-3 more cases from the search log
   (`~/.claude/cache/claude-browse-search.log.jsonl`).
2. Investigate boilerplate pollution at INDEX time: imported sessions carry
   giant AGENTS.md instruction dumps in `first_msg`/`user_text`. Options to
   evaluate (pick by eval delta, not taste): strip known instruction-dump
   prefixes when building `sessions_fts` columns; or add a dedicated
   `boilerplate` column weight of ~0 (the column exists, weight tuning in
   `_DEFAULT_BM25_WEIGHTS`).
3. Gate: full test suite green + eval scores not regressed on ANY existing
   graded case. Run eval before and after; report the diff.

## Task 2 — Session naming quality (Agent Board)

**Problem:** auto-names (claude_browse/board/naming.py) still skew toward
recent micro-tasks for long threads. v3 (commit `eff0afd`) samples user turns
at 25/50/75% + opening + recent; better, but the live result for a 1,000+
message thread was "slack alert message verbosity and ranking tuning" when
the honest arc was "claude-browse agent board build and search fixes".

**Do:**
1. Build a tiny eval set first (5-8 real sessions from this machine with a
   human-judged "good name" each — ask Shamanth to grade once, in one
   message, not per-iteration).
2. Then iterate cheaply on: sampling (more mid-thread turns? assistant-turn
   inclusion? weight by turn length?), prompt wording, and model (Haiku is
   the default; do NOT move naming to a bigger model without eval proof it's
   needed).
3. Constraints that already exist and must hold: `_clean_name` validation
   (2-8 words, ≤60 chars, no preamble — regression-tested), prefill-based
   prompt shape, `_REFRESH_AFTER_MSGS` throttle, existing tests green.

## Guardrails (both tasks)

- Never suppress or penalize a session based on metadata-only cue matches —
  the evidence rule from `add20a3`/`bb0afab` is settled.
- `claude_browse/board/` hot paths (hook.py, statusline.py) stay
  network-free; naming/sync stay out-of-band. Don't move work into hooks.
- Commit per logical unit with the failure/fix story in the message; push to
  main when tests + eval gate pass.
