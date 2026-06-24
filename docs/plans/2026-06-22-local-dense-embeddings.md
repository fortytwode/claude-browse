# Local Dense Embeddings for Session Recall

Status: implemented
Confidence: 95%

## Problem

`claude-browse` and `codex-browse` should find prior threads from natural
descriptions such as "the discussion about a teammate's performance" even when the
exact phrasing differs from the old transcript. The current local sparse
semantic window index improves this without network calls, but it cannot fully
capture paraphrase-level meaning.

The user also needs exact URL/page ID recall to remain reliable. Dense semantic
search must not replace exact identifier matching.

## Constraints

- Personal and local-first: vectors are stored in the local SQLite cache.
- Default behavior stays offline: no network, no accounts, no API calls unless
  explicitly enabled.
- No hosted vector store or OpenAI File Search. Storage and retrieval remain
  local.
- No hard-coded user terms, names, URLs, or one-off query phrases in production
  code.
- No required runtime dependency. Optional embedding API calls use stdlib HTTP.
- Interactive fzf search calls the ranker on every query change, so dense query
  embedding must be opt-in and cached.

## Architecture

Dense embeddings are a side cache over `semantic_windows`.

Index-time:

- Build transcript windows exactly as today.
- When `CLAUDE_BROWSE_DENSE_EMBEDDINGS=1` and `OPENAI_API_KEY` are set, embed
  missing or stale windows.
- Store vectors in SQLite as float32 blobs keyed by `semantic_windows.rowid`.
- Store model, dimensions, content hash, norm, and timestamp for invalidation.

Query-time:

- Exact URL/page ID matching runs first.
- FTS and local sparse semantic search continue to run as today.
- Dense search runs only when enabled, the query is descriptive enough, and
  local dense vectors already exist.
- Query embeddings are cached in SQLite by model, dimensions, and query hash.
- Dense scores contribute to the same match metadata/ranking surface as sparse
  semantic windows.

## Configuration

Required to enable:

```bash
export CLAUDE_BROWSE_DENSE_EMBEDDINGS=1
export OPENAI_API_KEY=...
```

Optional:

```bash
export CLAUDE_BROWSE_EMBEDDING_MODEL=text-embedding-3-small
export CLAUDE_BROWSE_EMBEDDING_DIMENSIONS=256
export CLAUDE_BROWSE_EMBEDDING_BATCH_SIZE=64
export CLAUDE_BROWSE_DENSE_MIN_SCORE=0.25
```

## Cost Model

Using local storage means there is no hosted retrieval or vector-storage cost.
The only paid operation is embedding text sent to the embedding API when the
optional dense feature is enabled.

For the current corpus measured on June 22, 2026:

- Raw segments: about 2.2M estimated tokens.
- Current overlapping semantic windows: about 10.75M estimated tokens.
- `text-embedding-3-small`: about $0.04 for raw segments or $0.21 for current
  windows at $0.02 per 1M tokens.
- `text-embedding-3-large`: about $0.29 for raw segments or $1.40 for current
  windows at $0.13 per 1M tokens.

The implementation embeds windows to maximize recall quality, defaults to
`text-embedding-3-small`, and uses reduced dimensions to keep local disk and
brute-force retrieval cheap.

## Verification

- Unit tests prove the default path does not call embeddings.
- Unit tests mock the embedding API and prove dense results can surface a
  paraphrase match.
- Unit tests prove reindex does not re-embed unchanged windows.
- Full `pytest` and `ruff` pass.
- Manual smoke checks confirm exact Notion URL/page ID recall still wins.
