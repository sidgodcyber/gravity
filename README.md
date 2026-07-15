# Exocortex — Phases 1 & 2

A personal agentic OS: it captures your digital life into a portable SQLite
brain, watches your whole research trail, decides on its own what you keep
leaning on without understanding, and turns that into a daily curriculum —
one concept at a time, spaced-repeated until it sticks. Devices are sensors
and terminals; **the state is the system**.

```
                       ┌─ Plane A: code/git  (allowlist) ──► state/exocortex.db ─┐ syncs
capture daemon ─► firewall ┤                                                     │
                       └─ Plane B: research trail (browser, search, AI prompts,  │
                          window, clipboard) ──► state/local.db ── LOCAL ONLY, never synced
                                          │
   nightly cycle (03:30): git scan + AI-trail import + CONCEPT ENGINE ──► foundation gaps
                                          │
   morning brief (07:00) + Foundation section ──► Telegram ◄── /ask /gaps /review /learned /week
   weekly check-in (Sun 19:00) ──► voice note ──► local transcription ──► outcomes
```

## Setup

Works on Windows and Linux. Requires Python ≥ 3.11 and [uv](https://docs.astral.sh/uv/)
(or plain pip).

```bash
git clone <your-repo> exocortex && cd exocortex
uv sync --all-extras                 # or: pip install -e .[embeddings]
copy config.example.yaml config.yaml    # Linux: cp
```

Then edit `config.yaml`:

1. **Fill in the firewall blocklist** with your employer's paths, app names,
   window-title and URL patterns. Do this before first run. If you will ever
   run the daemon on a company machine, put that machine's hostname in
   `firewall.work_mode_hosts` — the daemon there permanently captures nothing
   except your manual `/log` entries.
2. **Telegram**: create a bot with [@BotFather](https://t.me/BotFather)
   (`/newbot`, 2 minutes), put the token in `telegram.bot_token`. Start
   `exo bot`, message it once — it replies with your chat id; paste that into
   `telegram.chat_id`. The bot ignores everyone but you.
3. **API keys** under `router.api_keys` (inline or `env:VARNAME`).
4. **Sync**: set a long `sync.passphrase` and (optionally) an rclone remote.

Everything runs through one command:

```
exo daemon run|status     capture daemon (+ RAM, counts, mode)
exo work on|off|status    firewall work mode (also /work in Telegram)
exo bot                   Telegram bot, long-polling
exo ask "..."             RAG answer over your state + trail
exo brief [--send]        morning brief now (fresh; --send serves 07:00 cache)
exo gaps                  ranked foundation gaps with evidence
exo learned "<topic>"     mark a concept understood (graduates it)
exo week "<text>"         log this week's outcome (or send a voice note)
exo log "..."             record a reflection from the CLI
exo budget                spend + Claude Code plan pace + advice
exo spent 3.50 "..."      manual usage log (fallback for plan tracking)
exo cycle nightly         the sleep cycle + concept engine (03:30)
exo cycle weekly          send the weekly check-in nudge (Sun 19:00)
exo transcribe <audio>    local voice→text (used by the bot for voice notes)
exo style import <path>   import writing samples (voice corpus)
exo embed                 embed style corpus locally (fastembed, CPU)
exo sync push|pull        encrypted brain sync (Plane A only)
exo graph export          write the Obsidian vault (also runs nightly)
exo install [--apply]     generate (and register) auto-start for this OS
exo migrate               apply pending db migrations (+ plane split)
```

Telegram commands: `/ask` (or just message it), `/brief`, `/gaps`,
`/review` (spaced-repetition check-in with tap-to-grade buttons), `/learned
<topic>`, `/week <text>` (or send a **voice note** — transcribed locally),
plus the Phase 1 `/log`, `/budget`, `/work`, `/spent`.

## The trust layer (final consolidation)

Everything the bot shows or stores follows four rules, each with regression
tests reproducing a live failure:

- **One ID namespace.** Every `#N` the bot ever displays — task, log, note —
  comes from a single registry and retrieves exactly that record forever;
  deleted ids are never reused. (`/delete 9` can no longer hit a different
  table's row 9.)
- **Confirmations are durable and never stored.** A pending "reply yes to
  confirm" lives in the database (10-minute expiry), survives bot restarts,
  executes on yes/ok/confirm, cancels on no, and anything else cancels it AND
  is processed as a normal message. Your "Yes" is consumed, never logged.
  Only one bot instance can run at a time.
- **Plain English works.** "delete task 9", "delete everything except #1",
  "mark the roven one done", "where did that note come from?" — a resolution
  step maps your words to concrete records (it can only pick from real ids in
  context, never invent one) and echoes the exact plan before touching
  anything. Slash commands are forgiving (`/delete task 9` works).
- **Provenance everywhere.** Every record says *you said* / *observed* /
  *inferred* with source and timestamp (`/source <id>` for the full answer).
  Tasks presupposing an event with no trace in the store are held behind a
  question instead of minted ("I have no record of a call with a museum
  director — create anyway?").

**`USER_GUIDE.md` is the rulebook** — generated from the same source that
powers `/guide` in the bot, so it can't drift. Ask the bot "what can you do"
or `/guide <topic>` anytime.

## Talk to it naturally — the intent layer (Phase 2.5)

You don't pick a command for most things: just message the bot and it works
out *what kind of thing* you said, stores it in the right place, and echoes
back what it did so you can catch a mistake immediately.

- **Tasks** — "research Roven's salon idea by Friday" becomes a tracked task
  with a due date; a message with several pieces becomes a parent task with
  sub-tasks. `/tasks` lists open work by client; `/due` shows what's due (and
  overdue) soon; `/done <id>` and `/drop <id>` close them; `/task <text>`
  forces one explicitly. Open and overdue tasks lead the morning brief.
- **Weekly updates, ideas, client notes, reflections** — each routes to the
  right store (outcomes feed the concept engine, ideas are held for you to
  promote later with `/task`, client facts go to that client's dossier).
- **Questions** route to `/ask`; they're answered, not stored.
- **Corrections** — say "delete the Urban Straps note" or "it's Roven not
  Urban" and it finds the matching records, **shows you the list, and only
  changes anything after you confirm**.

Every message is stored with its raw text, so nothing is lost even when the
classifier guesses wrong — and you can always fix it (below). Entities it
doesn't recognize are flagged as new rather than invented; if it can't
resolve a date or a client, it says so instead of making one up.

## Fixing what it stored — editable memory (Phase 2.5)

`/recent [n]` shows your last records with IDs and their intent. `/edit <id>
<new text>` replaces one and re-runs extraction; `/delete <id>` removes one
(**always asks you to confirm first**). Natural-language corrections work too.

Deletes **heal downstream**: removing a message also removes the tasks,
reflections, client notes, and skill-graph nodes it spawned, and invalidates
today's cached brief — so a wrong entry can't haunt future briefs or gap
analysis. A skill-graph node cited by *other* evidence survives; only nodes
whose sole source you deleted are pruned.

## Voice notes are rough-but-correctable by design

The weekly check-in (and any voice note) is transcribed **locally on CPU**
with a small Whisper model. On this hardware transcription is imperfect, so
the system doesn't pretend otherwise: it shows you what it heard and lets one
reply fix it — reply `ok` to file it as heard, or just retype the corrected
version and it uses that instead. That correction step is the feature, not a
workaround for perfect accuracy the laptop can't deliver.

## The two capture planes (Phase 2)

Capture is general across everything **except your employer's code**, split
into two planes that are separated *structurally*, not by a filter you could
misconfigure:

- **Plane A — code/files.** Git activity (and any future file-content
  reading) happens **only** for paths on the `capture_code_from` allowlist —
  your personal repos. It's an allowlist, so anything not listed is
  unreadable by construction; company work lives outside those paths and
  cannot be captured. Symlinks/junctions inside an allowlisted tree that
  point outside it do **not** widen it (resolved-path matching, fails
  closed). Plane A data lives in `state/exocortex.db` and **syncs** normally.
- **Plane B — the research trail.** Browser history, search queries, page
  titles, your prompts to AI coding tools, window titles, and clipboard are
  captured generally into `state/local.db`. That file is **never** part of a
  sync bundle — the guarantee is structural (separate file; `sync push` only
  ever packages `exocortex.db`, and refuses to run if trail rows are ever
  found in the synced db). Your research trail never leaves this machine.

Concept names, scores, and explainers (the curriculum) are Plane A and sync;
the raw trail excerpts that justify them (the "why" and evidence) stay in
`local.db`.

## The auto-curriculum (Phase 2)

Because most coding is now done *by* AI, the learning lives in your research
trail, not in code you typed. Each night the concept engine reads ~2 weeks of
trail and:

- finds **recurring lookups** — things you searched or asked AI about on
  multiple days (repetition = it hasn't stuck),
- judges **depth vs. surface** engagement,
- flags **emerging domains**, and
- produces a ranked **foundation-gaps** list, each with a plain-language
  "why" citing your actual evidence trail, plus a short original explainer.

Every gap's evidence quotes are validated **verbatim against the trail**
before it's shown — an ungrounded claim is dropped, not displayed. When the
trail is too thin, `/gaps` says so honestly rather than inventing something.
The daily brief surfaces **one** concept (never a firehose); `/review`
spaced-repeats it on a 2/4/8/16-day ladder; getting it right graduates it and
raises its skill-graph confidence, fumbling resets it and lowers confidence.
Recurring lookups also lower confidence — so the graph honestly reflects what
you know versus what you keep re-Googling.

Weekly ritual: Sunday 19:00 the bot asks "what did you ship this week?" —
answer with a 30-second voice note (transcribed locally, on-device) or
`/week <text>`. That's the entire deliberate-logging burden.

### Auto-start (required setup — a nudge that never registered is a silent failure)

Run **`exo install --apply`** once on each machine, then **`exo install
--status`** to confirm — it lists every scheduled item with its next fire
time, so you can see the nightly cycle and Sunday weekly nudge are actually
scheduled rather than silently dormant. `exo uninstall` removes them cleanly.

- **Windows:** `exo install --apply` registers the nightly cycle (03:30),
  brief delivery (07:00), and weekly check-in (Sun 19:00) as per-user
  Scheduled Tasks, and drops the always-on **daemon and bot into your Startup
  folder** so they launch at logon. (Logon launch uses the Startup folder
  rather than an `ONLOGON` scheduled task on purpose — creating those needs
  admin on locked-down machines; the Startup folder doesn't.)
- **Linux:** `exo install` generates units under `ops/linux/`; run `bash
  ops/linux/install.sh`. Installs systemd user services (daemon, bot) and
  timers (nightly, brief, weekly). For capture before first login:
  `loginctl enable-linger $USER`. Window titles need X11 + `xdotool`; on
  Wayland the daemon logs a warning and still captures clipboard/files/browser.

There is also an **optional** GitHub Actions nightly
(`.github/workflows/nightly.yml`) that operates only on synced encrypted
state — it stays dormant until you add its secrets (see comments in the file).

## Security model — read this part

**What never gets stored:** anything matching the firewall blocklist. Every
capture event (window title, clipboard, file path, browser URL) passes one
choke point (`exocortex/daemon/filter.py`) before it can touch disk:

- **Paths** match on literal *and* symlink-resolved forms, component-wise and
  case-insensitively — `C:\work` via a junction is caught; `C:\workspace2` is
  not falsely blocked. Unparseable paths are dropped (fail closed).
- **Apps** match on process executable name, so a renamed window title cannot
  hide a blocklisted app. Clipboard events carry the foreground app/window at
  copy time and are filtered by the same rules — copying *out of* a company
  window is blocked even if the text is innocent.
- **Work mode** (`exo work on`, or `/work on` from your phone) drops
  everything except manual `/log`. Toggling it on also purges any
  captured-but-unwritten queue, and the writer re-checks the flag at flush —
  an event racing the toggle cannot land. Hosts in `work_mode_hosts` are
  locked into work mode permanently.
- **Sensitive titles** (banking, password managers, incognito) are stored but
  tagged `sensitivity=sensitive` and never included in LLM prompts or /ask.
- **Clipboard secrets** (API keys, tokens, JWTs, private keys) are redacted
  before storage.
- Browser-history imports run through the same firewall (URL + title rules)
  and are skipped entirely while work mode is on.

The adversarial test suite for all of the above lives in
`tests/test_firewall.py` — run it after any change to the filter.

**What is stored, and where:** plaintext in `state/exocortex.db` on your own
disk. It leaves the machine only via `exo sync push`, encrypted (age format,
scrypt passphrase) before transport. Your writing samples are embedded
locally on CPU — they never go to an embedding API. Telegram messages transit
Telegram's servers — don't `/log` things you wouldn't put in a chat app.
T1_free providers (Gemini/Groq free tiers) may train on inputs; the nightly
summarizer sends window titles/domains/reflections there. If that bothers
you, move `nightly_summary` traffic to T2 by putting an Anthropic model first
in `T1_free`.

## Budget governor

- Every system LLM call lands in `usage_ledger` with a cost estimate; when
  month-to-date spend reaches `router.monthly_budget_usd`, paid tiers lock
  and everything degrades to free tiers automatically.
- **Claude Code plan usage** is estimated by parsing its local session
  transcripts (`~/.claude/projects/**/*.jsonl` — location and 30-day
  retention verified against the docs and the real files on 2026-07-05;
  duplicate usage lines are deduped by requestId). Costs shown are
  *API-equivalent value* to visualize pace on a flat-rate plan. If Anthropic
  changes the format, `/budget` will say it found nothing — then use
  `/spent` to log usage manually; that path always works.

## Restore drill (new machine) — practice this once

1. Install Python + uv (+ rclone if you use a remote), clone this repo,
   `uv sync --all-extras`.
2. `copy config.example.yaml config.yaml` — set `sync.passphrase` (and
   `sync.remote` if used). These two values are the keys to your brain; keep
   them in your password manager.
3. `exo sync pull` — fetches the newest bundle and restores
   `state/exocortex.db` (and config.yaml if the bundle has one and you don't).
   Refuses to overwrite an existing db unless `--force` (which keeps a
   timestamped `.bak`).
4. `exo daemon status` — confirm the brain is back (event counts survive).
5. `exo install` — re-register auto-start on the new machine.

No rclone remote? `exo sync push` writes the encrypted bundle to
`state/sync-out/`; move it however you like and drop it in `state/sync-in/`
on the new machine before `exo sync pull`. Bundles are standard age files:
`age -d exocortex-*.tar.gz.age` works anywhere as a last resort.

## Multi-device (hub & spoke)

One machine is the **hub** (`role: hub`) — it runs the Telegram bot, the
nightly/weekly cycles, the brief, and owns the canonical brain. Every other
machine is a **spoke** (`role: spoke`): it runs ONLY the capture daemon,
stages reflections (`exo log`) and allowlisted git commits locally, and
pushes them as encrypted append-only batch files to
`<sync.remote>/spokes/<device>/` (or `state/queue/pending/` for a manual
move). The hub merges pending batches nightly (and on `exo sync merge`) —
insert-only with per-batch and per-event ledgers, so re-importing the same
batch inserts nothing, ever.

The role is structural, not advisory: a spoke refuses to start the bot (two
pollers on one token split Telegram updates — lived through it), refuses
cycles/brief/brain push/pull, and `exo install` on a spoke registers only
the daemon and the daily queue push. Per-device Plane A allowlists
(`capture_code_from` keyed by `device_name`) mean one machine's paths never
apply on another, and a device without an entry captures nothing. **Plane B
never leaves any machine**: each device keeps its own local trail, so the
concept engine sees only the hub's trail — the desktop contributes through
git commits and your reflections, and that trade-off is deliberate.

Every record names its device: "you said (direct log, on desktop)".
`/devices` (or `exo devices`) shows each machine's last batch — a silently
dead spoke daemon is visible from your phone.

Add a machine: copy the repo (without config.yaml), `uv sync --all-extras`,
then `exo setup spoke --device <name> --hub laptop` and follow its printed
steps (edit the allowlist, set the shared sync passphrase, configure the
same rclone remote, `exo install --apply`). **Every machine needs a unique
`device_name`** — event ids are namespaced by it, so two spokes sharing a
name would silently swallow each other's records, and the hub refuses
batches claiming its own name.

## Graph vault (Obsidian)

`exo graph export` (also a nightly step) renders the skill graph as a folder
of markdown notes at `vault.path` (default `d:\gravity-vault`, outside this
repo) — open it in [Obsidian](https://obsidian.md) and its graph view becomes
the visual map of everything Gravity knows: one note per node, connections as
wikilinks with the relationship named inline, evidence quoted with provenance
labels and record ids. Start at `_START HERE.md`.

The vault is **derived data and is deliberately NOT in the encrypted sync**:
it regenerates from `state/exocortex.db` on any machine, so bundling it would
only bloat the backup. Exporter-owned notes (frontmatter
`gravity: exporter-owned`) are overwritten/removed to mirror the db; any file
you create yourself in the vault is never touched.

## Footprint

Measured on the target laptop (8 GB RAM, Windows 11, 2026-07-05): daemon
**35.6 MB** RSS steady-state. The bot idles small and grows to ~150-200 MB
only after the first /ask imports LiteLLM — combined resident load stays
well under the 300 MB budget. Embedding and nightly jobs are short-lived
processes and don't count toward resident use.

## Layout

```
config.yaml               all knobs (copy of config.example.yaml)
state/exocortex.db        Plane A brain: reflections, concepts, skill graph,
                          code activity — SYNCS
state/local.db            Plane B trail: browser/search/AI-prompt/window/
                          clipboard + concept evidence — LOCAL ONLY
exocortex/daemon/         capture + firewall (filter.py choke point);
                          allowlist.py = Plane A guard; sources/ (window,
                          clipboard, files, browser, git_activity, ai_trail)
exocortex/curriculum/     concept engine, spaced repetition, transcription
exocortex/router/         tiers, ledger, governor, Claude Code parser
exocortex/retrieval/      FTS + local embeddings + style corpus
exocortex/extract/        reflections → skill graph
exocortex/cycles/         nightly (git+trail+engine+brief), weekly nudge
exocortex/sync/           encrypted push/pull (Plane A only, guarded)
memory/                   lessons for future Claude Code sessions on this repo
```

Migrations are numbered SQL files in `exocortex/state/migrations/`; add
`0002_*.sql` etc. — they apply automatically on next connect.

## Troubleshooting

- `exo daemon status` says NOT RUNNING → check `state/logs/daemon.log`.
- Bot silent → wrong `chat_id` (message it; it tells you the right one when
  unclaimed), or another instance is polling the same token.
- `/budget` shows no Claude Code usage → transcript format changed or
  `claude_code.transcripts_dir` wrong; fall back to `/spent`.
- Free-tier model names rot; edit `router.tiers.T1_free` — a dead entry just
  falls through to the next one.
