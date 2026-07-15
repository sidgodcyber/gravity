# Claude Code transcript parsing (verified 2026-07-05)

- Location documented at code.claude.com/docs/en/claude-directory:
  `~/.claude/projects/<project-slug>/<session-uuid>.jsonl`, plus
  `<session>/subagents/*.jsonl`. Auto-deleted after `cleanupPeriodDays`
  (default 30) — usage estimates only cover that window.
- Assistant lines: `message.model`, `message.usage` (input_tokens,
  output_tokens, cache_read_input_tokens, cache_creation_input_tokens),
  ISO `timestamp`, `requestId`.
- CRITICAL: Claude Code writes one line per content block and repeats the
  same usage/requestId — MUST dedupe on requestId or counts multiply
  (parser: exocortex/router/claude_code.py).
- docs.claude.com/en/docs/claude-code/* now 301s to code.claude.com/docs/en/*.
