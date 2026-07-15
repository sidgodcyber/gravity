# Exocortex — notes for Claude Code sessions

**Start every session by reading `memory/`** — one lesson per file, corrections
and environment quirks live there. Update it when you learn something durable;
delete notes that prove wrong. **Then read `memory/HANDOFF.md`** — the living
snapshot of project state, in-flight work, and what's next. Near the end of
any long session — or whenever a task completes and context feels heavy —
UPDATE HANDOFF.md to the new state: one screen max, dates on claims, overwrite
freely (it's a snapshot, not a log), never leave stale claims standing.

Build/test basics:
- venv: `.venv` (uv, Python 3.14). Run tests: `.venv\Scripts\python.exe -m pytest -q`
  (suite takes ~3 min on this laptop; firewall tests spawn `mklink` subprocesses).
- The work firewall (`exocortex/daemon/filter.py` + `tests/test_firewall.py`) is
  safety-critical. Any change to capture/filter code requires running the
  adversarial tests, and new capture sources MUST submit through
  `Pipeline.submit` — never write to `life_stream` directly.
- All LLM calls go through `exocortex/router/llm.py:complete(tier=...)` — never
  call litellm or a provider SDK directly, never hardcode a model name outside
  config/prices. The daemon must never import litellm.
- Timestamps in the db are unix epoch milliseconds UTC (`state/db.py:now_ms`).
- Schema changes = new numbered file in `exocortex/state/migrations/` (synced db)
  or `migrations_local/` (Plane B). Never edit an existing migration.

TWO CAPTURE PLANES (Phase 2, safety-critical):
- Plane A (code/git): read ONLY via `daemon/allowlist.py` from
  `capture_code_from`. Fails closed; resolved-path matching (symlinks can't
  widen it). Lives in `exocortex.db`, SYNCS.
- Plane B (research trail): ALL daemon capture writes to `local.db`
  (`db.local_connect`), which is NEVER bundled by sync. `sync push` runs
  `split_plane_b` then refuses if non-manual rows remain in the synced db.
  Anything touching capture, sync, or `concept_evidence` must keep this true
  — see `tests/test_planes.py` (run its adversarial cases on any change).
- Concept engine (`curriculum/engine.py`): LLM-over-trail; every gap's
  evidence is validated VERBATIM against the material (`validate_gaps`) —
  ungrounded gaps are dropped. Thin trail → honest "not enough trail", no LLM
  call. `why` prose is trail-derived → stored in `local.db` concept_evidence,
  NOT in the synced `concepts` table.
- upsert_node(bump=False) in curriculum flows: the curriculum manages
  skill-graph confidence itself (re-lookups down, graduation up) — a default
  +0.1 bump would cancel it.
- Spec for the whole system: see README (user-facing) and ROADMAP (arc).
