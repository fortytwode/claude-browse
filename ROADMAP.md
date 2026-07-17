# Roadmap

This document captures what `claude-browse` is now, what is already shipped,
and the next product phases that move it from “browse old chats” to “resume
software work.”

## Product strategy in one paragraph

**`claude-browse` is a local-first resume-work tool, not a generic chat
history browser.** The CLI should help you recover the state of a task across
Claude, CodeX, Gemini, Copilot, and Cursor: what you were doing, where the
thread drifted, what the current repo looks like, and what the best next prompt
is. The open-source CLI stays local and trusted. If there is a paid business
later, it should sell shared work-state, sync, and team handoff layers on top
of that wedge.

---

## Current state (May 2026)

- Target-app browsers are shipped:
  - `claude-browse`
  - `codex-browse`
  - `gemini-browse`
  - `copilot-browse`
  - `cursor-browse`
- Built-in providers are shipped for Claude, CodeX, Gemini, Copilot, and Cursor
  (Cursor is target-only today).
- Provider adapters and an experimental external-provider seam are shipped.
- Cross-provider handoff is shipped and query-aware.
- Restart cards are shipped in the preview pane and handoff brief:
  - current task
  - opening topic when the thread drifted
  - current repo state
  - last meaningful ask
  - latest assistant answer
  - likely open question
  - suggested next prompt
- Suggested next prompts and restart cards can be printed directly from the browser.
- Exportable work artifacts are shipped from the browser:
  - restart card
  - suggested next prompt
  - reusable handoff brief
  - concise status update
- Query-active result rows now show trust/provenance tags like `primary subject`,
  `folder match`, `title match`, `opening match`, `mentioned later`,
  `feedback`, `critique`, `closeout`, and `drifted` before you open preview.
- Restart-card previews now include a `Why this surfaced` block:
  - match type
  - match timestamp
  - match confidence
  - best action (`Enter` vs `Ctrl-T`)
- Natural-language thread description is now the primary query model:
  - descriptive `find the thread where...` inputs
  - ranking by most recent relevant mention
  - query-anchored preview for drifted threads
  - explicit topic re-entry via a fresh matched-exchange handoff

---

## Immediate roadmap

These are the next product phases that can be implemented directly in this repo.

### Phase 1 — Restart quality
**Status:** shipped

- Reframe the product around `Resume Work`
- Replace raw preview with restart cards
- Include repo-state overlays in preview and handoff
- Keep cross-provider handoff grounded in end-of-thread state, not opening prompt

### Phase 2 — Exact thread recall
**Status:** shipped

Make `describe the thread` the default interaction instead of token search.

What shipped:
- Natural-language `Find thread >` prompt and help text
- Query parsing that drops recall filler and keeps names, phrases, brands, and folders
- Ranking by the most recent relevant mention, not just overall thread activity
- Query-anchored preview that surfaces the matched exchange first

Why it matters:
- The user wants the exact old thread, even when they forgot the folder and the thread later drifted

### Phase 3 — Mid-thread re-entry
**Status:** shipped

Support honest re-entry into topic A when the original thread later moved on to B/C/D.

What shipped:
- `Resume thread` remains the default action
- `Re-enter topic` starts a new session seeded from the matched exchange
- Handoff text distinguishes re-entry from native resume or cross-app resume

Why it matters:
- Cross-provider tools cannot universally rewind an original thread in place, but they can reliably start a new session from the earlier matched exchange

### Phase 4 — Query-first UI
**Status:** shipped

Make the visible affordance match the descriptive retrieval model that already exists.

What shipped:
- Prompt copy that reads like a sentence starter, not a token search box
- In-picker examples that encourage longer natural-language recall queries
- Low-confidence coaching when the query is too vague and needs one concrete anchor
- Interpreted-query feedback so the user can see which subject the tool is actually searching for
- A clearer distinction between plain entity lookups and descriptive thread recall

Why it matters:
- The product should visibly invite `where i was asking nevena about feedback`, not accidentally train users to type `nevena feedback`
- Better UI guidance is cheaper and safer than piling on backend complexity when the current failure is mostly affordance mismatch

### Phase 5 — Semantic reranking under the hood
**Status:** shipped

Keep one visible query model while improving retrieval depth behind the scenes.

What shipped:
- Stronger query understanding and typed lexical reranking on top of SQLite first
- Local semantic-proxy reranking for descriptive thread queries using intent cues
  like closeout, feedback, critique, and human-performance review
- Better fallback when the user remembers the idea but not the exact anchor words
- Still one visible mode: describe the thread you want

Why it matters:
- Natural-language recall gets stronger without making the UI more complicated
- This keeps the current local-first, no-daemon, zero-runtime-dependency product
  intact without forcing a cloud or external search backend

### Phase 6 — Work artifacts
**Status:** shipped

Turn session + repo state into reusable outputs.

What shipped:
- Restart brief / restart card
- Human-to-agent handoff brief
- Agent-to-human status update
- Suggested next prompt export

Why it matters:
- Session history becomes operationally useful outside the TUI

### Phase 7 — Search trust
**Status:** shipped

Make every result explain itself clearly enough that the user knows whether to
resume the latest thread state or re-enter an older matched topic.

What shipped:
- Row-level provenance and drift labels
- Preview/header provenance with match type, match time, and match confidence
- Explicit best-action guidance (`Enter` vs `Ctrl-T`) for drifted threads
- Eval-label support for storing a preferred action alongside each real query

Why it matters:
- Retrieval quality is only half the product. Users need to trust why a thread
  surfaced and which action is safest before they launch anything.

### Phase 8 — Shared work graph
**Status:** later

Move from local task recovery to shared team context.

What ships:
- Cross-device sync
- Shared task timelines
- Team handoff state
- Shared restart briefs and work artifacts

Why it matters:
- This is the first genuinely monetizable layer

---

## Execution cadence

This project should move phase-by-phase, not as one long uninterrupted build.

Rules:
- Ship one coherent phase at a time.
- Push after each phase so the product can be tested in the real terminal flow.
- Do not start the next high-risk phase until the current one has been manually tested.
- Prefer small reversible slices over large architectural jumps.

Current manual test gate:
- Test the restart card quality in the picker preview.
- Test cross-provider handoff quality using the imported restart brief.
- Test `Ctrl-Y` for suggested next prompt output.
- Test `Ctrl-B` for restart-card export output.

Next gated build after testing:
- better artifact formatting and downstream integrations
- richer result-list UI polish if row-level rationale still feels too opaque
- only after that, reconsider `Task View` if real usage proves thread clustering is the actual problem

---

## Known gaps (verified, queued)

- **Single-anchor ranking can bury a title match.** Observed live 2026-07-17:
  searching `maxrewards` ranked a thread titled "Upload MaxRewards testing
  tasks to Frame.io" (anchor in the title, most recent matching activity in
  the result set) at #33 of 39, below threads that mention the anchor only
  in passing. All candidates land in the same single-anchor evidence tier,
  so a discriminator ahead of the recency key misorders them. Fix must be
  eval-driven (`eval/run.py`) so other query shapes don't regress; the rule
  to encode: an anchor hit in the title should outrank passing-mention
  threads.
- **Session-folder attribution follows the launch directory.** A thread
  started from a repo root is filed under the root even if the work is about
  a subfolder/client -- so folder-scoped views (`--here`, web "this folder
  only", folder float) miss it. Possible improvement: also attribute
  sessions to folders whose paths appear heavily in the transcript.

---

## Product principles

- Optimize for restart quality before provider count.
- Prefer deterministic local heuristics before adding AI summarization.
- Treat “resume work” as a task-state problem, not a transcript problem.
- Keep native resume and cross-provider handoff explicit; do not pretend they are the same.
- Do not freeze a public plugin API until several real providers force a stable contract.

---

## Non-goals for now

- Editing session content
- A cloud dependency for the core CLI
- A public provider marketplace with compatibility guarantees
- Live collaborative sessions
- Generic memory for every app outside software work

---

## Changelog

- 2026-04-22: First roadmap draft.
- 2026-05-12: Reframed roadmap around resume-work, restart cards, task view, and shared work-state.
