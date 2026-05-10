"""Eval harness for the claude-browse ranker.

Code lives in the repo (generic). Labeled queries live per-user at
~/.claude/cache/claude-browse-eval/queries.json (or wherever
$CLAUDE_BROWSE_EVAL_QUERIES points), so client/personal terms in queries
never enter the public repo.
"""
