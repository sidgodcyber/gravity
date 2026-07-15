# Roadmap

## Phase 1 — foundation (this build, July 2026)
State store + migrations · capture daemon with work firewall · Telegram bot
(/log /ask /brief /budget /work /spent) · tiered router + usage ledger +
budget governor · Claude Code plan-usage parser · nightly cycle + morning
brief · local CPU embeddings + style corpus · encrypted sync + restore drill.

## Phase 2 — auto-curriculum (DONE, July 2026)
Two capture planes (Plane A code/allowlist syncs; Plane B research trail
local-only) · general activity capture (browser, searches, AI-tool prompts) ·
nightly concept-inference engine with verbatim evidence validation ·
foundation-gaps list, `/gaps` `/review` `/learned` `/week` · brief Foundation
section · spaced repetition with skill-graph confidence read/write · weekly
voice-memo outcome capture with local transcription · anti-confabulation
grounding in `/ask` and the engine.

## Phase 2.5 — task & intent layer + editable memory (DONE, July 2026)
Intent classifier (task / weekly_update / plan / client_note / reflection /
question / correction) with multi-intent tagging, entity resolution against
known clients/projects, sub-task extraction, relative-date resolution,
confidence-gated confirmation, and anti-confabulation (new entities flagged
not invented). Task store + `/tasks` `/due` `/done` `/drop` `/task`, surfaced
in the brief. Editable memory: `/recent` `/edit` `/delete` + natural-language
corrections, confirm-before-destroy, downstream healing (deleting a source
purges its tasks/reflections/notes/graph-nodes and invalidates the cached
brief). Voice notes chunked + shown-for-correction before commit. `exo
install`/`uninstall`/`--status` scheduler registration.

## Final consolidation — natural language + trust layer (DONE, July 2026)
One user-facing ID namespace (uids registry; #9 collision fixed with
migration and regression test) · db-persisted confirmations with TTL,
gate-before-classify, never stored as content, singleton bot lock — verified
through the live bot loop across a process restart · natural-language
commands (delete/edit/complete/show/reassign/source; ordinals and set
expressions; structurally validated targets; plan-echo before execution;
forgiving slash syntax) · provenance labels (you said / observed / inferred)
+ /source · task-creation grounding (museum-director case) · full
browser-history backfill (Apr–Jul, June agewarp searches verified) ·
reassignment path · USER_GUIDE.md + /guide generated from one source.

## Graph vault + session handoff (DONE, July 2026)
`exo graph export` + nightly step: the skill graph as an Obsidian vault at
`d:\gravity-vault` (derived data, never synced) — one note per node with
frontmatter/summary/provenance-labeled uid evidence, wikilink edges with the
relationship named inline, type folders for graph-view colors, `_START HERE`
dashboard, manifest-based incremental sync (user files never touched;
deleted nodes' notes removed). Adversarially tested: filename escapes,
tampered manifests, wedged-export and note-restore regressions (29 tests).
Plus `memory/HANDOFF.md` — the living session snapshot every Claude session
reads at start and refreshes at end (rule in CLAUDE.md).

## Multi-device — hub & spoke (DONE, July 2026)
`role: hub | spoke` in config, enforced structurally: the desktop (spoke)
runs only the capture daemon + a daily encrypted queue push; the laptop
(hub) merges batches in the nightly cycle (`exo sync merge` on demand) with
per-batch and per-event idempotency ledgers (migration 0005). Per-device
Plane A allowlists (`capture_code_from` keyed by device_name, fail-closed);
Plane B stays local per machine, structurally excluded from batches and
refused by the hub merge if smuggled. Device-tagged provenance ("you said
(direct log, on desktop)"), `/devices` visibility, `exo setup spoke`
bootstrap. Verified live: real spoke batch → real hub merge → provenance
shown → re-merge inserted zero.

## Phase 3 — the parts that make money (next)
- **Opportunity radar sources**: scrapers/feeds for freelance gigs, science
  competitions, museum-tech grants → `opportunities` table with LLM fit
  scoring against the (now much richer) skill graph; `/interested` /`/pass`
  feedback tunes the scorer.
- **Client CRM**: `/client` bot commands to add touchpoints from the phone;
  the Phase 1 `crm` tables and staleness alerts wired into the brief.
- **Approval queue**: everything outbound (emails, DMs, posts) drafted into a
  queue, sent only after explicit Telegram approval. No autonomous sends.
- **Style-drafting engine**: `style_corpus` retrieval (already embedded) as
  few-shot voice examples; `/draft <brief>` produces client replies and posts
  in my voice, into the approval queue.

## Phase 4 — candidates (decide later)
- Opportunity → project pipeline with time-blocking suggestions
- Local summarization/inference model when a good small CPU one exists
- Additional sensors: calendar import, phone usage export
- Curriculum → portfolio export (graduated concepts as demonstrated skills)

## Standing constraints (do not violate in any phase)
- ≤ ~300 MB resident RAM total on the laptop; state stays portable files
- Work firewall is sacred; new capture sources must go through filter.py
- Provider-agnostic: tiers in config, never hardcoded models
- System API spend target < $10/month; draft-only external actions
