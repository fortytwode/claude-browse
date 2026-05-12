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

---

## Immediate roadmap

These are the next product phases that can be implemented directly in this repo.

### Phase 1 — Restart quality
**Status:** shipped

- Reframe the product around `Resume Work`
- Replace raw preview with restart cards
- Include repo-state overlays in preview and handoff
- Keep cross-provider handoff grounded in end-of-thread state, not opening prompt

### Phase 2 — Task view
**Status:** next

Group related sessions into one task instead of forcing the user to think in
provider-native thread IDs.

What ships:
- Heuristic task clustering by cwd, time proximity, title similarity, and key terms
- Task list mode in addition to raw session mode
- “This task spans Claude + CodeX + Gemini” presentation

Why it matters:
- The user wants to reopen `the Pokpok brief work`, not `session abc123`

### Phase 3 — Best next prompt
**Status:** in progress

Generate a deterministic continuation prompt from local state.

What ships:
- Suggested next prompt in preview
- Copy/export flows for that prompt
- Target-specific phrasing for Claude, CodeX, Gemini, Copilot, and Cursor

Why it matters:
- This is the first output that turns search into forward motion

### Phase 4 — Work artifacts
**Status:** later

Turn session + repo state into reusable outputs.

What ships:
- Restart brief
- Human-to-agent handoff brief
- Agent-to-human summary
- Standup / “what changed and why” export

Why it matters:
- Session history becomes operationally useful outside the TUI

### Phase 5 — Shared work graph
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
