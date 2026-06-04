"""Interactive session browser. SQLite FTS5 search + preview pane over fzf.

fzf is used purely as a picker (--disabled). All query matching is done by
SQLite FTS5 via a keystroke-driven reload binding. This gives us proper
token-level search with phrase queries, instead of fzf's character-level
fuzzy match (which floods short queries like "sca2" with false positives).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Mapping

from . import fts, search_log
from .core import (
    build_handoff_markdown,
    build_import_markdown,
    display_cwd,
    folder_name,
    format_date,
    provider_display_name,
    write_import_file,
)
from .providers import get_provider, provider_entries, provider_ids
from .query import QueryPlan, build_query_plan
from .work_state import (
    build_work_state,
    render_restart_card_terminal,
    render_status_update_terminal,
)

DEFAULT_LIMIT = 100
ROW_META_SEP = "\x1f"
COACH_SESSION_ID = "__coach__"
COACH_PROVIDER = "coach"
_UI_CRITIQUE_TERMS = frozenset(
    {
        "critique",
        "critical",
        "evidence",
        "forced",
        "opportunities",
        "opportunity",
        "question",
        "questioned",
        "questioning",
        "skeptic",
        "skeptical",
    }
)
_UI_FEEDBACK_TERMS = frozenset(
    {
        "comment",
        "comments",
        "feedback",
        "note",
        "notes",
        "review",
        "reviews",
    }
)
_UI_REVIEW_TERMS = frozenset({"performance"})


def _encode_row_metadata(
    visible: str,
    sid: str,
    cwd: str,
    provider: str,
    *,
    match_label: str = "",
    match_timestamp: str = "",
    match_confidence: str = "",
) -> str:
    parts = (
        visible,
        _sanitize_row_meta_value(sid),
        _sanitize_row_meta_value(cwd),
        _sanitize_row_meta_value(provider),
        _sanitize_row_meta_value(match_label),
        _sanitize_row_meta_value(match_timestamp),
        _sanitize_row_meta_value(match_confidence),
    )
    return ROW_META_SEP.join(parts)


def _sanitize_row_meta_value(value: object) -> str:
    return (
        str(value or "")
        .replace(ROW_META_SEP, " ")
        .replace("\n", " ")
        .replace("\r", " ")
        .strip()
    )


def _decode_row_metadata(line: str) -> dict[str, str] | None:
    if ROW_META_SEP not in line:
        return None
    parts = line.rsplit(ROW_META_SEP, 6)
    if len(parts) == 4:
        visible, sid, cwd, provider = parts
        match_label = ""
        match_timestamp = ""
        match_confidence = ""
    elif len(parts) == 7:
        (
            visible,
            sid,
            cwd,
            provider,
            match_label,
            match_timestamp,
            match_confidence,
        ) = parts
    else:
        return None
    return {
        "visible": visible,
        "session_id": sid.strip(),
        "cwd": cwd.strip(),
        "provider": provider.strip(),
        "match_label": match_label.strip(),
        "match_timestamp": match_timestamp.strip(),
        "match_confidence": match_confidence.strip(),
    }


def _split_row_metadata(line: str) -> tuple[str, str, str, str] | None:
    data = _decode_row_metadata(line)
    if not data:
        return None
    return (
        data["visible"],
        data["session_id"],
        data["cwd"],
        data["provider"],
    )


def _query_feedback_terms(plan: QueryPlan) -> list[str]:
    terms = list(plan.anchor_terms[:3])
    if plan.wants_closeout and "closeout" not in terms:
        terms.append("closeout")
    elif plan.wants_recent and "latest" not in terms:
        terms.append("latest")
    return terms[:4]


def _is_drifted_match(info: Mapping[str, object]) -> bool:
    match_timestamp = str(info.get("match_timestamp") or "").strip()
    last_timestamp = str(info.get("last_timestamp") or info.get("timestamp") or "").strip()
    return bool(match_timestamp and last_timestamp and match_timestamp != last_timestamp)


def _match_provenance(info: Mapping[str, object], query: str) -> dict[str, str]:
    if not query.strip():
        return {
            "match_label": "",
            "match_confidence": "",
        }

    plan = build_query_plan(query)
    anchor_terms = [str(term).strip(".*").lower() for term in plan.anchor_terms if term]
    normalized = set(plan.normalized_terms)
    drifted = _is_drifted_match(info)
    metadata_anchor_score = float(info.get("_metadata_anchor_score") or 0.0)
    quality_score = float(info.get("_quality_score") or 0.0)
    match_term_count = int(info.get("match_term_count") or 0)
    title_text = str(info.get("name") or "").lower()
    opening_text = str(info.get("first_msg") or "").lower()
    cwd_segments = _normalized_path_segments(info.get("cwd"))

    match_label = ""
    match_confidence = "medium"

    if info.get("phrase_fallback"):
        return {
            "match_label": "near phrase",
            "match_confidence": "medium",
        }
    if info.get("prefix_fallback"):
        return {
            "match_label": "prefix match",
            "match_confidence": "medium",
        }

    if len(anchor_terms) == 1:
        anchor = anchor_terms[0]
        if anchor and anchor == (cwd_segments[-1] if cwd_segments else None):
            match_label = "folder match"
            match_confidence = "high"
        elif anchor and anchor in title_text:
            match_label = "title match"
            match_confidence = "high" if not drifted else "medium"
        elif anchor and anchor in opening_text:
            match_label = "opening match"
            match_confidence = "medium"
        elif anchor:
            match_label = "mentioned later"
            match_confidence = "low" if drifted else "medium"
    else:
        opening_anchor_hits = sum(1 for anchor in anchor_terms if anchor and anchor in opening_text)
        if opening_anchor_hits >= max(1, min(2, len(anchor_terms))):
            match_label = "opening match"
            match_confidence = "high" if not drifted else "medium"
        elif metadata_anchor_score >= 4.5 or (
            match_term_count >= max(2, min(3, len(anchor_terms)))
            and quality_score >= 6.0
        ):
            match_label = "primary subject"
            match_confidence = "high" if quality_score >= 7.5 else "medium"
        else:
            match_label = "mentioned later"
            match_confidence = "low" if drifted else "medium"

    if not match_label and plan.wants_closeout and float(info.get("match_lifecycle_score") or 0.0) > 0:
        match_label = "primary subject"
        match_confidence = "medium"
    elif not match_label and normalized:
        match_label = "mentioned later"
        match_confidence = "low" if drifted else "medium"

    return {
        "match_label": match_label,
        "match_confidence": match_confidence,
    }


def _query_coach_summary(plan: QueryPlan) -> str:
    if plan.low_confidence:
        return "Add one anchor: person, client, brand, or folder"

    terms = _query_feedback_terms(plan)
    if len(terms) == 1 and not plan.descriptive:
        return f"Looking for threads about: {terms[0]}"
    if terms:
        return f"Looking for: {' + '.join(terms)}"
    return "Describe one thread with a person, client, brand, or folder"


def format_query_coach_row(query: str) -> str | None:
    if not query.strip():
        return None
    plan = build_query_plan(query)
    summary = _query_coach_summary(plan)
    visible = (
        "\033[2mTip\033[0m      "
        "\033[2mcoach\033[0m "
        f"{'':<15} \033[1;36m{summary}\033[0m  "
    )
    return _encode_row_metadata(
        visible,
        COACH_SESSION_ID,
        "",
        COACH_PROVIDER,
    )


def render_query_coach_preview(query: str) -> str:
    plan = build_query_plan(query)
    stripped = query.strip()
    if not stripped:
        return (
            "Query coaching\n\n"
            "Describe the thread you want in a short sentence.\n\n"
            "Examples:\n"
            "  where i was asking nevena about feedback\n"
            "  last closeout session for musopia\n"
            "  pokpok brief where we questioned the opportunities"
        )

    lines = ["Query coaching", "", _query_coach_summary(plan)]
    if plan.low_confidence:
        lines.extend(
            [
                "",
                "Add one concrete anchor so the search can stop guessing.",
                "Good anchors: person, client, brand, folder, or exact phrase.",
                "",
                "Try something like:",
                "  nevena feedback",
                "  musopia closeout",
                "  pokpok opportunities",
            ]
        )
        return "\n".join(lines)

    if plan.anchor_terms:
        lines.append("")
        lines.append(f"Anchors: {', '.join(plan.anchor_terms)}")
    if plan.exact_phrase_terms:
        if plan.descriptive:
            lines.append(f"Phrase boost: {', '.join(plan.exact_phrase_terms)}")
        else:
            lines.append(f"Exact phrase boost: {', '.join(plan.exact_phrase_terms)}")
    if plan.phrase_fallback_terms and not plan.descriptive:
        lines.append(
            "No-hit fallback: "
            f"{' + '.join(plan.phrase_fallback_terms)}"
        )

    intents: list[str] = []
    if plan.wants_recent:
        intents.append("latest / last")
    if plan.wants_closeout:
        intents.append("closeout / wrap-up")
    if intents:
        lines.append(f"Intent: {', '.join(intents)}")

    lines.extend(
        [
            "",
            (
                "Sentence-style query detected. The ranker will prefer these anchors "
                "over filler words."
                if plan.descriptive
                else "Exact anchor search. Add more context if the results still feel broad."
            ),
        ]
    )
    return "\n".join(lines)


def _folder_prefixes() -> tuple[str, ...]:
    """Optional prefix list for shortening folder display.

    Example: if CLAUDE_BROWSE_FOLDER_PREFIXES="monorepo/apps/:monorepo/lib/",
    a cwd of ~/monorepo/apps/checkout shows as "checkout" not "monorepo".
    """
    raw = os.environ.get("CLAUDE_BROWSE_FOLDER_PREFIXES", "")
    return tuple(p.strip() for p in raw.split(":") if p.strip())


def _normalized_path_segments(path: str | None) -> list[str]:
    if not path:
        return []
    segments: list[str] = []
    for part in str(path).split("/"):
        cleaned = "".join(ch for ch in part.lower() if ch.isalnum())
        if cleaned:
            segments.append(cleaned)
    return segments


def _match_reason_tags(
    info: dict,
    query: str,
    match_date: str,
    thread_date: str,
) -> list[str]:
    if not query.strip():
        return []

    plan = build_query_plan(query)
    tags: list[str] = []
    normalized = set(plan.normalized_terms)
    intent_score = float(info.get("match_intent_score") or 0.0) - float(
        info.get("match_mismatch_penalty") or 0.0
    )
    provenance = _match_provenance(info, query)
    if provenance["match_label"]:
        tags.append(provenance["match_label"])

    if plan.wants_closeout and float(info.get("match_lifecycle_score") or 0.0) > 0:
        tags.append("closeout")
    elif intent_score > 0 and normalized & _UI_FEEDBACK_TERMS:
        tags.append("feedback")
    elif intent_score > 0 and normalized & _UI_CRITIQUE_TERMS:
        tags.append("critique")
    elif (
        intent_score > 0
        and normalized & _UI_REVIEW_TERMS
        and any(term not in _UI_REVIEW_TERMS for term in plan.anchor_terms)
    ):
        tags.append("review")

    if _is_drifted_match(info):
        tags.append("drifted")

    deduped: list[str] = []
    for tag in tags:
        if tag not in deduped:
            deduped.append(tag)
    return deduped[:3]


def format_row(
    info: dict, query: str = "", prefixes: tuple[str, ...] = ()
) -> str:
    """Render one session as a fzf row line.

    Layout:
      `{visible}{ROW_META_SEP}{sid}{ROW_META_SEP}{cwd}{ROW_META_SEP}{provider}`

    The suffix is FTS5's matched-context snippet when a query is active, or a
    topic-drift hint (latest user message) when the title looks stale.
    """
    query_active = bool(query.strip())
    thread_date = format_date(info.get("last_timestamp") or info.get("timestamp"))
    match_date = format_date(info.get("match_timestamp"))
    date = thread_date
    provider = (info.get("provider") or "claude").lower()
    cwd = info.get("cwd")
    fname = folder_name(cwd, prefixes)
    title = (
        ((info.get("name") or info.get("first_msg") or "")[:60])
        .replace("\n", " ")
        .replace(ROW_META_SEP, " ")
    )
    sid = info.get("session_id") or "?"
    ffolder = display_cwd(cwd)
    provenance = _match_provenance(info, query)

    suffix_parts: list[str] = []
    if query_active and _is_drifted_match(info):
        suffix_parts.append(f"\033[2mmatched {match_date}\033[0m")

    if query_active and info.get("context"):
        # FTS5 snippet: \x01 wraps matched terms, \x02 ends the wrap. Render
        # them as bold yellow inside dim grey text so matched terms pop.
        raw_context = info["context"]
        # FTS5 centers the snippet on the match (~12 tokens of lead), so in a
        # narrow list pane the highlighted term is truncated off-screen and the
        # row shows only leading filler. Trim the lead so the first matched term
        # leads the snippet and stays visible while scanning.
        first_hit = raw_context.find("\x01")
        if first_hit > 8:
            raw_context = "…" + raw_context[first_hit:]
        snippet = (
            raw_context
            .replace("\x01", "\033[0m\033[1;33m")
            .replace("\x02", "\033[0m\033[2m")
            .replace("\n", " ")
            .replace("\r", " ")
            .replace("\t", " ")
            .replace(ROW_META_SEP, " ")
        )
        body = f"\033[2m→ {snippet}\033[0m"
        reason_tags = _match_reason_tags(info, query, date, thread_date)
        meta_parts: list[str] = []
        if reason_tags:
            meta_parts.append(" · ".join(reason_tags))
        if title:
            meta_parts.append(title[:30])
        if query_active and _is_drifted_match(info):
            meta_parts.append(f"matched {match_date}")
        meta = (
            f"  \033[2m[{ ' · '.join(meta_parts) }]\033[0m"
            if meta_parts
            else ""
        )
        visible = f"{date:<8} {provider:<6} {fname:<15} {body}{meta}  "
        return _encode_row_metadata(
            visible,
            sid,
            ffolder,
            provider,
            match_label=provenance["match_label"],
            match_timestamp=str(info.get("match_timestamp") or ""),
            match_confidence=provenance["match_confidence"],
        )
    else:
        msgs = f"{info.get('msg_count', 0)}msg"
        last = (
            (info.get("last_msg") or "")
            .strip()
            .replace("\n", " ")
            .replace("\r", " ")
            .replace("\t", " ")
            .replace(ROW_META_SEP, " ")
        )
        title_words = {w for w in title.lower().split() if len(w) >= 4}
        last_words = {w for w in last.lower().split() if len(w) >= 4}
        if last and last_words and len(title_words & last_words) <= 1:
            suffix_parts.append(f"\033[2m→ {last[:70]}\033[0m")

    suffix = f"  {' · '.join(suffix_parts)}" if suffix_parts else ""

    visible = f"{date:<8} {provider:<6} {fname:<15} {msgs:<7} {title}{suffix}  "
    return _encode_row_metadata(
        visible,
        sid,
        ffolder,
        provider,
        match_label=provenance["match_label"],
        match_timestamp=str(info.get("match_timestamp") or ""),
        match_confidence=provenance["match_confidence"],
    )


def _write_preview_script(
    script_path: str, db_path: str, package_dir: str
) -> None:
    """Write a helper script fzf calls to render session previews."""
    script = f"""#!/usr/bin/env python3
import os
import sqlite3
import sys

sys.path.insert(0, {package_dir!r})

from claude_browse.core import (
    extract_query_terms,
    highlight_terms,
    provider_display_name,
)
from claude_browse.browse import COACH_SESSION_ID, _decode_row_metadata, render_query_coach_preview
from claude_browse.work_state import build_work_state, render_restart_card_terminal

DB_PATH = {db_path!r}
ROW_META_SEP = {ROW_META_SEP!r}


def _lookup_session(session_id):
    conn = sqlite3.connect(DB_PATH)
    try:
        return conn.execute(
            '''
            SELECT provider, path, cwd, timestamp, last_timestamp, title,
                   first_msg, last_msg, msg_count
            FROM sessions
            WHERE sid = ?
            ''',
            (session_id,),
        ).fetchone()
    finally:
        conn.close()


def get_preview(row_meta, query=""):
    session_id = row_meta.get("session_id", "")
    if session_id == COACH_SESSION_ID:
        print(render_query_coach_preview(query))
        return

    session = _lookup_session(session_id)
    if not session:
        print("Session not found.")
        return

    (
        provider,
        path,
        cwd,
        timestamp,
        last_timestamp,
        name,
        first_msg,
        last_msg,
        msg_count,
    ) = session
    state = build_work_state(
        {{
            "provider": provider,
            "path": path,
            "cwd": cwd,
            "timestamp": timestamp,
            "last_timestamp": last_timestamp,
            "name": name,
            "first_msg": first_msg,
            "last_msg": last_msg,
            "msg_count": msg_count,
            "session_id": session_id,
            "match_label": row_meta.get("match_label", ""),
            "match_timestamp": row_meta.get("match_timestamp", ""),
            "match_confidence": row_meta.get("match_confidence", ""),
        }},
        query,
    )
    terms = extract_query_terms(query)

    def hl(text):
        return highlight_terms(text, terms) if terms else text

    # Lead with the matched snippet so scanning results shows WHERE the query
    # hit before the metadata/restart-card header pushes it below the fold.
    lead = state.get("matched_exchange") or state.get("matching_turns") or []
    if query.strip() and lead:
        conf = str(state.get("match_confidence") or "")
        bits = [
            b
            for b in (
                str(state.get("match_label") or ""),
                (conf + " confidence") if conf else "",
            )
            if b
        ]
        suffix = (" (" + " · ".join(bits) + ")") if bits else ""
        print(hl('Matches "' + query + '"' + suffix + ":"))
        print()
        for role, text in lead:
            label = "User" if role == "user" else "Assistant"
            print(hl("  " + label + ": " + text))
        if state.get("thread_continued_after_match"):
            print()
            print("  ↪ Thread moved on afterward; newer turns are further down.")
        print()
        print("─" * 40)
        print()

    print(f"Source:  {{provider_display_name(provider)}}")
    if name:
        print(f"Session: {{hl(name)}}")
    if cwd:
        home = os.path.expanduser("~")
        if cwd.startswith(home):
            cwd = "~" + cwd[len(home):]
        print(f"Folder:  {{cwd}}")
    if timestamp:
        print(f"Started: {{timestamp[:19].replace('T', ' ')}}")
    if last_timestamp and last_timestamp != timestamp:
        print(f"Last activity: {{last_timestamp[:19].replace('T', ' ')}}")
    print(f"Messages: {{msg_count or 0}}")
    print()
    print(hl(render_restart_card_terminal(state, show_match_block=not (query.strip() and lead))))


if __name__ == "__main__":
    line = sys.argv[1] if len(sys.argv) > 1 else ""
    query = os.environ.get("FZF_QUERY", "")
    row_meta = _decode_row_metadata(line)
    if row_meta and row_meta.get("session_id"):
        get_preview(row_meta, query)
"""

    with open(script_path, "w") as f:
        f.write(script)
    os.chmod(script_path, 0o755)


def _write_search_script(
    script_path: str,
    db_path: str,
    package_dir: str,
    cwd_filter: str | None,
    current_cwd: str | None,
    limit: int,
) -> None:
    """Write the keystroke-driven search helper invoked by fzf change:reload."""
    script = f"""#!/usr/bin/env python3
import os
import sys
import time
sys.path.insert(0, {package_dir!r})

from claude_browse import fts, search_log
from claude_browse.browse import format_query_coach_row, format_row

DB_PATH = {db_path!r}
CWD_FILTER = {cwd_filter!r}
CURRENT_CWD = {current_cwd!r}
LIMIT = {limit}

q = os.environ.get("FZF_QUERY", "")
conn = fts.open_db(DB_PATH)
ranker = "recent"
start = time.perf_counter()
if q.strip():
    # ranker_v1: multi-column BM25 + exp-decay recency. See fts.search_ranked.
    # Set CLAUDE_BROWSE_RANKER=current to fall back to recency-only.
    import os as _os
    if _os.environ.get("CLAUDE_BROWSE_RANKER") == "current":
        ranker = "current"
        results = fts.search(conn, q, limit=LIMIT)
    else:
        ranker = "v1"
        results = fts.search_ranked(conn, q, limit=LIMIT, current_cwd=CURRENT_CWD)
else:
    results = fts.list_recent(conn, limit=LIMIT)

if CWD_FILTER:
    results = [r for r in results if (r.get("cwd") or "").startswith(CWD_FILTER)]

search_log.log_search(
    q,
    results,
    ranker=ranker,
    cwd_filter=CWD_FILTER,
    current_cwd=CURRENT_CWD,
    limit=LIMIT,
    elapsed_ms=(time.perf_counter() - start) * 1000,
)

coach_row = format_query_coach_row(q)
if coach_row:
    print(coach_row)

for r in results:
    print(format_row(r, q))
"""
    with open(script_path, "w") as f:
        f.write(script)
    os.chmod(script_path, 0o755)


def _write_enter_guard_script(script_path: str, state_path: str) -> None:
    """Write a helper that guards Enter right after a query-changing paste.

    fzf treats carriage return as accept. Multi-line terminal pastes can
    therefore auto-open the current top result before the user has a chance
    to inspect the list. The tricky part is that fzf can dispatch the paste's
    newline-triggered Enter before our change hook finishes updating state.
    So we keep both a timed guard and an explicit one-shot confirmation for
    paste-like query jumps.
    """
    script = f"""#!/usr/bin/env python3
import json
import os
import sys
import time

STATE_PATH = {state_path!r}
THRESHOLD_MS = 250
PASTE_JUMP_CHARS = 16
PASTE_GUARD_MS = 1500


def _read_state():
    try:
        with open(STATE_PATH) as f:
            data = json.load(f)
            if isinstance(data, dict):
                return data
    except Exception:
        pass
    return {{
        "last_change_ms": 0,
        "last_query": "",
        "paste_guard_until_ms": 0,
        "guard_pending_query": "",
    }}


def _write_state(state):
    os.makedirs(os.path.dirname(STATE_PATH) or ".", exist_ok=True)
    with open(STATE_PATH, "w") as f:
        json.dump(state, f)


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    if mode == "note-change":
        now_ms = int(time.time() * 1000)
        state = _read_state()
        query = os.environ.get("FZF_QUERY", "")
        prev_query = state.get("last_query", "")
        jump_chars = abs(len(query) - len(prev_query))
        paste_guard_until_ms = int(state.get("paste_guard_until_ms", 0) or 0)
        if jump_chars >= PASTE_JUMP_CHARS:
            paste_guard_until_ms = max(paste_guard_until_ms, now_ms + PASTE_GUARD_MS)
        elif paste_guard_until_ms <= now_ms:
            paste_guard_until_ms = 0
        guard_pending_query = state.get("guard_pending_query", "")
        if query != prev_query:
            guard_pending_query = ""
        _write_state(
            {{
                "last_change_ms": now_ms,
                "last_query": query,
                "paste_guard_until_ms": paste_guard_until_ms,
                "guard_pending_query": guard_pending_query,
            }}
        )
        return
    if mode == "maybe-accept":
        now_ms = int(time.time() * 1000)
        state = _read_state()
        query = os.environ.get("FZF_QUERY", "")
        last_query = state.get("last_query", "")
        guard_pending_query = state.get("guard_pending_query", "")
        paste_guard_until_ms = int(state.get("paste_guard_until_ms", 0) or 0)
        jump_chars = abs(len(query) - len(last_query))
        paste_like_jump = jump_chars >= PASTE_JUMP_CHARS
        if guard_pending_query == query and query:
            print("accept")
            return
        if paste_guard_until_ms > now_ms or paste_like_jump:
            _write_state(
                {{
                    "last_change_ms": max(
                        int(state.get("last_change_ms", 0) or 0),
                        now_ms,
                    ),
                    "last_query": query,
                    "paste_guard_until_ms": paste_guard_until_ms,
                    "guard_pending_query": query,
                }}
            )
            print(
                "ignore+change-header(Pasted query detected. Review the list, then press Enter again to open. Ctrl-O opens immediately.)"
            )
            return
        age_ms = now_ms - int(state.get("last_change_ms", 0) or 0)
        if age_ms >= THRESHOLD_MS:
            print("accept")
        else:
            print(
                "ignore+change-header(Type or paste your query, review the list, then press Enter to open. Ctrl-O opens immediately.)"
            )


if __name__ == "__main__":
    main()
"""
    with open(script_path, "w") as f:
        f.write(script)
    os.chmod(script_path, 0o755)


def _build_fzf_cmd(
    target_name: str,
    search_script_path: str,
    preview_script_path: str,
    enter_guard_script_path: str,
) -> list[str]:
    return [
        "fzf",
        "--ansi",
        "--no-sort",
        "--reverse",
        "--height=90%",
        "--border=rounded",
        "--prompt=Find thread where... ",
        (
            "--header="
            'Type a sentence, not just keywords. Try: "where i was asking nevena about feedback" | '
            '"last closeout session for musopia" | '
            '"pokpok brief where we questioned the opportunities". '
            f"Enter: resume in {target_name} (yolo) | "
            f"Ctrl-O: resume in {target_name} immediately | "
            f"Ctrl-T: re-enter matched topic in {target_name} | "
            f"Ctrl-S: open in {target_name} (safe) | "
            "Ctrl-Y: next prompt | Ctrl-B: restart card | "
            "Ctrl-H: handoff brief | Ctrl-U: status update | "
            "Esc: quit | Shift-Up/Down: scroll preview"
        ),
        "--header-first",
        f"--delimiter={ROW_META_SEP}",
        "--with-nth=1",
        "--print-query",
        "--disabled",
        f"--bind=change:execute-silent(python3 {enter_guard_script_path} note-change)+reload(python3 {search_script_path})",
        f"--preview=python3 {preview_script_path} {{}}",
        "--preview-window=right:45%:wrap",
        "--bind=shift-up:preview-up,shift-down:preview-down",
        f"--bind=enter:transform(python3 {enter_guard_script_path} maybe-accept)",
        "--bind=ctrl-o:accept",
        "--bind=ctrl-s:print(SAFE:)+accept",
        "--bind=ctrl-t:print(TOPIC:)+accept",
        "--bind=ctrl-y:print(PROMPT:)+accept",
        "--bind=ctrl-b:print(BRIEF:)+accept",
        "--bind=ctrl-h:print(HANDOFF:)+accept",
        "--bind=ctrl-u:print(STATUS:)+accept",
    ]


def _check_fzf() -> None:
    if shutil.which("fzf"):
        return
    print("Error: fzf is required but not installed.", file=sys.stderr)
    print(file=sys.stderr)
    print("Install it with:", file=sys.stderr)
    if sys.platform == "darwin":
        print("  brew install fzf", file=sys.stderr)
    elif sys.platform.startswith("linux"):
        print("  apt install fzf        # Debian / Ubuntu", file=sys.stderr)
        print("  dnf install fzf        # Fedora / RHEL", file=sys.stderr)
        print("  pacman -S fzf          # Arch", file=sys.stderr)
    else:
        print("  https://github.com/junegunn/fzf#installation", file=sys.stderr)
    sys.exit(1)


def _default_target_provider(argv0: str) -> str:
    program = os.path.basename(argv0 or "claude-browse")
    if program.endswith("-browse"):
        provider = program[:-len("-browse")].lower()
        if provider in provider_ids(target_capable=True):
            return provider
    return "claude"


def _parse_target_provider(args: list[str], argv0: str) -> tuple[str, list[str]]:
    target_provider = _default_target_provider(argv0)
    valid_targets = provider_ids(target_capable=True)
    remaining: list[str] = []
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--target":
            if i + 1 >= len(args):
                print("Missing value for --target", file=sys.stderr)
                sys.exit(2)
            target_provider = args[i + 1].strip().lower()
            i += 2
            continue
        if arg.startswith("--target="):
            target_provider = arg.split("=", 1)[1].strip().lower()
            i += 1
            continue
        remaining.append(arg)
        i += 1

    if target_provider not in valid_targets:
        print(
            f"Unknown target provider: {target_provider}. "
            f"Expected one of: {', '.join(valid_targets)}",
            file=sys.stderr,
        )
        sys.exit(2)
    return target_provider, remaining


def _print_usage(argv0: str, target_provider: str) -> None:
    program = os.path.basename(argv0 or "claude-browse")
    target_name = provider_display_name(target_provider)
    valid_targets = "`, `".join(provider_ids(target_capable=True))
    print(
        f"Usage: {program} [options]\n"
        "\n"
        "Options:\n"
        "  --all                 Include every session, not just the most recent 100\n"
        "  --here                Only sessions started in the current directory\n"
        "  --list-providers      Show built-in and external provider availability\n"
        f"  --target PROVIDER     Override launch target (`{valid_targets}`)\n"
        "  -h, --help            Show this help\n"
        "\n"
        "Type a sentence inside the picker, not just one or two words:\n"
        "  pokpok brief where we questioned the opportunities\n"
        "  where i was asking nevena about feedback\n"
        "  last closeout session for musopia\n"
        '  "runna sca2"          Exact phrase when you know the words already\n'
        "  runna*                Prefix match: runna, runnathon, runna2026, ...\n"
        "  Longer descriptive queries are reduced to the most specific anchors.\n"
        "\n"
        "Keys while browsing:\n"
        f"  Enter                 Resume the selected thread in {target_name} (yolo)\n"
        f"  Ctrl-O                Resume immediately in {target_name} (bypasses paste guard)\n"
        f"  Ctrl-T                Re-enter the matched topic in a new {target_name} session (yolo)\n"
        f"  Ctrl-S                Open in {target_name} (safe)\n"
        "  Ctrl-Y                Print the suggested next prompt for the selection\n"
        "  Ctrl-B                Print the restart card for the selection\n"
        "  Ctrl-H                Print a reusable handoff brief\n"
        "  Ctrl-U                Print a concise status update\n"
        "  Shift-Up / Shift-Down Scroll the preview pane\n"
        "  Esc                   Quit\n"
        "\n"
        "Environment:\n"
        "  CLAUDE_BROWSE_PATH_ALIASES      src=dst[:src2=dst2...] custom cwd aliases\n"
        "  CLAUDE_BROWSE_FOLDER_PREFIXES   colon-separated prefixes for short folder names"
    )


def _join_with_or(values: list[str]) -> str:
    if not values:
        return ""
    if len(values) == 1:
        return values[0]
    if len(values) == 2:
        return f"{values[0]} or {values[1]}"
    return f"{', '.join(values[:-1])}, or {values[-1]}"


def _require_binary(provider: str) -> None:
    if _use_codex_mobile_mode(provider):
        return
    binary = get_provider(provider).binary
    if shutil.which(binary):
        return
    print(f"Target app not found on PATH: {binary}", file=sys.stderr)
    sys.exit(1)


def _codex_mobile_binary() -> str:
    return shutil.which("codexmobile") or os.path.expanduser("~/.local/bin/codexmobile")


def _use_codex_mobile_mode(provider: str) -> bool:
    binary = _codex_mobile_binary()
    return (
        provider == "codex"
        and bool(os.environ.get("SSH_CONNECTION"))
        and not os.environ.get("CODEX_BROWSE_MOBILE_DISABLE")
        and sys.stdin.isatty()
        and sys.stdout.isatty()
        and os.path.exists(binary)
        and os.access(binary, os.X_OK)
    )


def _codex_mobile_cmd(*args: str, yolo: bool) -> list[str]:
    cmd = [_codex_mobile_binary()]
    if yolo:
        cmd.append("--yolo")
    cmd.extend(args)
    return cmd


def _native_resume(
    session: dict,
    provider: str,
    session_id: str,
    cwd: str,
    prefixes: tuple[str, ...],
    yolo: bool,
) -> None:
    _require_binary(provider)
    spec = get_provider(provider)
    if _use_codex_mobile_mode(provider):
        cmd = _codex_mobile_cmd("resume", session_id, yolo=yolo)
        mode = " (yolo)" if yolo else ""
        print(
            f"Resuming{mode} in {spec.display_name} mobile mode "
            f"({folder_name(cwd, prefixes)})..."
        )
        os.execvp(cmd[0], cmd)
        return

    cmd = spec.native_resume_cmd(session_id, yolo)
    mode = " (yolo)" if yolo else ""
    print(f"Resuming{mode} in {spec.display_name} ({folder_name(cwd, prefixes)})...")
    os.execvp(spec.binary, cmd)


def _continue_in_provider(
    session: dict,
    source_provider: str,
    target_provider: str,
    cwd: str,
    prefixes: tuple[str, ...],
    yolo: bool = True,
    selection_query: str = "",
    *,
    reenter_topic: bool = False,
) -> None:
    target_spec = get_provider(target_provider)
    target_name = target_spec.display_name

    _require_binary(target_provider)

    if target_spec.handoff_via_file:
        try:
            import_path = write_import_file(
                session,
                target_provider,
                selection_query,
                reenter_topic=reenter_topic,
            )
        except OSError as exc:
            print(f"Could not write import brief: {exc}", file=sys.stderr)
            sys.exit(1)

        import_dir = os.path.dirname(import_path) or cwd
        if reenter_topic:
            prompt = (
                f"Continue the imported {provider_display_name(source_provider)} session "
                f"context from {import_path}. Treat it as prior conversation state, "
                "read that file first, use the Reopen Intent section as the reason "
                "this thread was selected, re-enter the earlier matched topic "
                "instead of resuming the full thread from the end, use later turns "
                "only as context about what happened afterward, then continue the "
                "work in this directory."
            )
        else:
            prompt = (
                f"Continue the imported {provider_display_name(source_provider)} session "
                f"context from {import_path}. Treat it as prior conversation state, "
                "read that file first, use the Reopen Intent section as the reason "
                "this thread was selected, prioritize the end-of-thread state and "
                "most recent turns over the original opening prompt, then continue "
                "the work in this directory."
            )
    else:
        import_dir = None
        import_markdown = build_import_markdown(
            session,
            target_provider,
            selection_query,
            reenter_topic=reenter_topic,
        )
        if reenter_topic:
            prompt = (
                f"Continue the imported {provider_display_name(source_provider)} session "
                "context below. Treat it as prior conversation state, use the "
                "Reopen Intent section as the reason this thread was selected, "
                "re-enter the earlier matched topic instead of resuming the "
                "full thread from the end, use later turns only as context "
                "about what happened afterward, then continue the work in "
                f"this directory.\n\n{import_markdown}"
            )
        else:
            prompt = (
                f"Continue the imported {provider_display_name(source_provider)} session "
                "context below. Treat it as prior conversation state, use the "
                "Reopen Intent section as the reason this thread was selected, "
                "prioritize the end-of-thread state and most recent turns over "
                "the original opening prompt, then continue the work in this "
                f"directory.\n\n{import_markdown}"
            )
    if _use_codex_mobile_mode(target_provider):
        cmd = _codex_mobile_cmd("start", prompt, yolo=yolo)
    else:
        cmd = target_spec.handoff_cmd(import_dir, prompt, yolo)
    mode = " (yolo)" if yolo else ""
    action = "Re-entering topic" if reenter_topic else "Continuing"
    print(f"{action}{mode} in {target_name} from {folder_name(cwd, prefixes)}...")
    exec_binary = cmd[0] if _use_codex_mobile_mode(target_provider) else target_spec.binary
    os.execvp(exec_binary, cmd)


def _open_in_target_provider(
    session: dict,
    source_provider: str,
    target_provider: str,
    session_id: str,
    cwd: str,
    prefixes: tuple[str, ...],
    yolo: bool,
    selection_query: str = "",
    *,
    reenter_topic: bool = False,
) -> None:
    if source_provider == target_provider and not reenter_topic:
        _native_resume(session, source_provider, session_id, cwd, prefixes, yolo)
        return
    _continue_in_provider(
        session,
        source_provider,
        target_provider,
        cwd,
        prefixes,
        yolo,
        selection_query,
        reenter_topic=reenter_topic,
    )


def _parse_fzf_output(
    output: str,
    target_provider: str,
) -> tuple[str, str, str, str] | None:
    text = output.strip()
    if not text:
        return None

    lines = [line for line in text.split("\n") if line]
    if not lines:
        return None

    # fzf --print-query prints the query first. Control-key actions add a
    # separate marker line via print(...)+accept before the selected row.
    selection_query = lines[0]
    marker = ""
    row_lines = lines[1:]

    if row_lines and row_lines[0] == "SAFE:":
        marker = row_lines.pop(0)
    elif row_lines and row_lines[0] == "TOPIC:":
        marker = row_lines.pop(0)
    elif row_lines and row_lines[0] == "PROMPT:":
        marker = row_lines.pop(0)
    elif row_lines and row_lines[0] == "BRIEF:":
        marker = row_lines.pop(0)
    elif row_lines and row_lines[0] == "HANDOFF:":
        marker = row_lines.pop(0)
    elif row_lines and row_lines[0] == "STATUS:":
        marker = row_lines.pop(0)
    elif selection_query.startswith("SAFE:"):
        original = selection_query
        marker = selection_query[:5]
        selection_query = ""
        row_lines = [original[5:]] + row_lines

    selected_target = target_provider
    action = "open_yolo"
    if marker == "SAFE:":
        action = "open_safe"
    elif marker == "TOPIC:":
        action = "reenter_topic"
    elif marker == "PROMPT:":
        action = "print_prompt"
    elif marker == "BRIEF:":
        action = "print_brief"
    elif marker == "HANDOFF:":
        action = "print_handoff"
    elif marker == "STATUS:":
        action = "print_status"

    row = next((line for line in reversed(row_lines) if ROW_META_SEP in line), "")
    if not row and ROW_META_SEP in selection_query:
        row = selection_query
        selection_query = ""

    if not row or ROW_META_SEP not in row:
        return None
    return row, selected_target, action, selection_query


def _providers_with_local_state() -> list[str]:
    return [
        provider
        for provider in provider_ids(source_capable=True)
        if get_provider(provider).has_local_state()
    ]


def _source_provider_descriptions() -> tuple[str, str]:
    provider_names = [
        get_provider(provider).display_name
        for provider in provider_ids(source_capable=True)
    ]
    provider_binaries = [
        f"`{get_provider(provider).binary}`"
        for provider in provider_ids(source_capable=True)
    ]
    return _join_with_or(provider_names), _join_with_or(provider_binaries)


def _print_provider_list() -> None:
    print(
        f"{'provider':<10} {'type':<8} {'src':<3} {'dst':<3} "
        f"{'avail':<5} {'exp':<3} {'binary':<16} auth"
    )
    for entry in provider_entries():
        spec = entry.spec
        auth = spec.auth_status() or "-"
        print(
            f"{spec.provider_id:<10} {entry.source_type:<8} "
            f"{'yes' if spec.source_capable else 'no':<3} "
            f"{'yes' if spec.target_capable else 'no':<3} "
            f"{'yes' if spec.is_available() else 'no':<5} "
            f"{'yes' if spec.experimental else 'no':<3} "
            f"{spec.binary:<16} {auth}"
        )
        if entry.source_type != "builtin":
            print(f"  origin: {entry.origin}")


def _load_session_by_id(session_id: str) -> dict | None:
    conn = fts.open_db()
    try:
        return fts.get_by_sid(conn, session_id)
    finally:
        conn.close()


def main() -> None:
    target_provider, args = _parse_target_provider(sys.argv[1:], sys.argv[0])

    if "-h" in args or "--help" in args:
        _print_usage(sys.argv[0], target_provider)
        return

    show_all = "--all" in args
    if show_all:
        args.remove("--all")

    list_providers = "--list-providers" in args
    if list_providers:
        args.remove("--list-providers")

    cwd_filter: str | None = None
    if "--here" in args:
        args.remove("--here")
        cwd_filter = os.getcwd()

    # --no-canonicalize is a legacy flag; canonicalization now happens at
    # index time, not at display time. Accept and ignore for compat.
    if "--no-canonicalize" in args:
        args.remove("--no-canonicalize")

    if args:
        print(f"Unknown argument: {args[0]}", file=sys.stderr)
        _print_usage(sys.argv[0], target_provider)
        sys.exit(2)

    if list_providers:
        _print_provider_list()
        return

    _check_fzf()

    available_providers = _providers_with_local_state()
    if not available_providers:
        provider_names, provider_binaries = _source_provider_descriptions()
        print(f"No local {provider_names} sessions found.")
        print(
            f"Run {provider_binaries} at least once to create session history."
        )
        sys.exit(1)

    conn = fts.open_db()
    total_pre = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
    if total_pre == 0:
        print("Indexing sessions for the first time...", file=sys.stderr)
    added, updated, removed = fts.reindex(conn)
    if added + updated + removed > 0 and total_pre == 0:
        print(f"  indexed {added} sessions", file=sys.stderr)
    conn.close()

    prefixes = _folder_prefixes()
    limit = 999 if show_all else DEFAULT_LIMIT

    conn = fts.open_db()
    initial = fts.list_recent(conn, limit=limit)
    if cwd_filter:
        initial = [r for r in initial if (r.get("cwd") or "").startswith(cwd_filter)]
    conn.close()

    if not initial:
        print("No sessions found.")
        sys.exit(1)

    initial_lines = [format_row(r, "", prefixes) for r in initial]
    package_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    preview_script = tempfile.NamedTemporaryFile(
        mode="w", suffix=".py", delete=False, prefix="claude_browse_preview_"
    )
    preview_script.close()
    _write_preview_script(preview_script.name, fts.DB_PATH, package_dir)

    search_script = tempfile.NamedTemporaryFile(
        mode="w", suffix=".py", delete=False, prefix="claude_browse_search_"
    )
    search_script.close()
    _write_search_script(
        search_script.name, fts.DB_PATH, package_dir, cwd_filter, os.getcwd(), limit
    )
    enter_guard_script = tempfile.NamedTemporaryFile(
        mode="w", suffix=".py", delete=False, prefix="claude_browse_enter_guard_"
    )
    enter_guard_script.close()
    enter_guard_state = tempfile.NamedTemporaryFile(
        mode="w", suffix=".txt", delete=False, prefix="claude_browse_enter_state_"
    )
    enter_guard_state.close()
    _write_enter_guard_script(
        enter_guard_script.name,
        enter_guard_state.name,
    )

    try:
        target_name = provider_display_name(target_provider)
        fzf_cmd = _build_fzf_cmd(
            target_name,
            search_script.name,
            preview_script.name,
            enter_guard_script.name,
        )

        result = subprocess.run(
            fzf_cmd,
            input="\n".join(initial_lines),
            capture_output=True,
            text=True,
        )

        if result.returncode != 0:
            sys.exit(0)

        parsed = _parse_fzf_output(result.stdout, target_provider)
        if not parsed:
            sys.exit(0)

        output, selected_target, action, selection_query = parsed

        row_meta = _decode_row_metadata(output)
        if not row_meta:
            print("Could not parse selected session.", file=sys.stderr)
            sys.exit(1)
        session_id = row_meta["session_id"]
        provider = row_meta["provider"]

        if session_id == COACH_SESSION_ID:
            print(render_query_coach_preview(selection_query))
            return

        session = _load_session_by_id(session_id)
        if not session:
            print(f"Session not found: {session_id}", file=sys.stderr)
            sys.exit(1)
        session = {
            **session,
            "match_label": row_meta.get("match_label", ""),
            "match_timestamp": row_meta.get("match_timestamp", ""),
            "match_confidence": row_meta.get("match_confidence", ""),
        }
        search_log.log_selection(
            selection_query,
            action=action,
            target_provider=selected_target,
            session=session,
        )

        state = None
        if action in {"print_prompt", "print_brief", "print_status", "reenter_topic"}:
            state = build_work_state(session, selection_query)

        if action == "print_prompt":
            print(state["suggested_next_prompt"])
            return

        if action == "print_brief":
            print(render_restart_card_terminal(state))
            return

        if action == "print_handoff":
            print(build_handoff_markdown(session, selection_query))
            return

        if action == "print_status":
            print(render_status_update_terminal(state))
            return

        if action == "reenter_topic":
            if not selection_query.strip():
                print(
                    "Topic re-entry needs a descriptive query. Search for the earlier topic first, then use Ctrl-T.",
                    file=sys.stderr,
                )
                sys.exit(1)
            if not state or not state.get("matched_exchange"):
                print(
                    "No matching exchange found for this query in the selected thread. Refine the query or resume the thread normally.",
                    file=sys.stderr,
                )
                sys.exit(1)

        cwd = session.get("cwd")
        if not cwd or not os.path.isdir(cwd):
            print(f"Original folder no longer exists: {cwd}", file=sys.stderr)
            suggestion = get_provider(provider).native_resume_cmd(session_id, False)
            print(f"Try: {' '.join(suggestion)}", file=sys.stderr)
            sys.exit(1)

        os.chdir(cwd)
        _open_in_target_provider(
            session,
            provider,
            selected_target,
            session_id,
            cwd,
            prefixes,
            action == "open_yolo",
            selection_query,
            reenter_topic=(action == "reenter_topic"),
        )

    finally:
        for path in (
            preview_script.name,
            search_script.name,
            enter_guard_script.name,
            enter_guard_state.name,
        ):
            try:
                os.unlink(path)
            except OSError:
                pass


if __name__ == "__main__":
    main()
