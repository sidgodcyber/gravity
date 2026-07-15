# Settled Phase-1 design decisions (don't re-litigate without the user)

- One SQLite db, WAL; embeddings as BLOBs in-db; FTS5 external-content tables
  with sync triggers. Migrations = numbered SQL files + schema_migrations.
- Firewall: single choke point (filter.decide → Pipeline.submit). Work-mode
  semantics: checked at submit AND flush, queue purged on toggle-on, forced
  hosts list. Fail closed on weird paths. Case-insensitive matching everywhere
  (accepted small false-positive risk on Linux).
- Daemon never imports litellm; bot lazy-imports on first /ask. Bot and daemon
  are separate processes.
- T1_free calls are recorded in usage_ledger at $0 by design (they're chosen
  because free); paid tiers use litellm.completion_cost with price-table
  fallback.
- Budget advice is rule-based, no LLM calls.
- Clipboard text is captured (redacted) but NEVER fed into LLM prompts by the
  nightly summarizer — only titles/domains/files/reflections go to T1.
- User chose: local fastembed embeddings (privacy) over API; live end-to-end
  verification at the end; has Anthropic+Gemini+Groq+OpenRouter keys.
