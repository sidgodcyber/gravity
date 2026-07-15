# HANDOFF — living session snapshot (overwrite freely, date every claim)

Last updated: 2026-07-14, end of the multi-device (hub & spoke) build.

## Where the project stands (as of 2026-07-14)

Live on this laptop (now formally the HUB, device_name "laptop"): Phases 1,
2, 2.5, trust layer (2026-07-09), graph vault (2026-07-12), and multi-device
hub & spoke (2026-07-14). Bot @Gravsisabot; schedulers via `exo install
--status`. Suite: 168 passing (23 multi-device in tests/test_multidevice.py,
29 vault). Migration 0005 applied to the real db; 6,780 old rows relabeled
DESKTOP-NEVA91K → laptop. Two fresh-context verifiers ran on this build:
verifier #1 found 5 must-fix core defects (merge wedge, passphrase misfile,
two silent-loss paths, role fail-open), verifier #2 confirmed the fixes and
found one more (implausible event ts breaking /recent) — all fixed with
regression tests; final verdict "ready".

## Multi-device (new, 2026-07-14)

role: hub|spoke in config, fail-closed validation. Spokes: capture daemon +
`exo sync push` only (encrypted batches → <remote>/spokes/<device>/ or
manual move); hub merges nightly + `exo sync merge`, insert-only with
batch/event ledgers. Per-device capture_code_from (dict by device_name).
Provenance now names devices ("you said (direct log, on desktop)");
/devices + `exo devices` show machine health. `exo setup spoke` bootstraps
a new machine. Invariants + sharp edges: memory/multi-device.md. Verified
live end-to-end: simulated desktop batch merged into the REAL brain
(record #51, "on desktop"), re-merge inserted zero, demo rows healed away.

## In-flight / unresolved (2026-07-14)

- THE DESKTOP IS NOT SET UP YET. User's next step: run the setup steps from
  the final summary (copy repo, `exo setup spoke --device desktop --hub
  laptop`, edit E: allowlist, install rclone + shared remote on BOTH
  machines, set sync.remote here too — it is still "" on the hub).
- rclone still NOT installed on this laptop; until both machines have the
  shared remote, spoke batches need a manual file move.
- User hasn't reported back on live phone testing of the trust layer.
- Phase 3 (opportunity radar/client channels) specced separately — don't start.

## Standing facts a fresh instance needs

No paid Anthropic key: T2 free stack, T3 degrades (memory/llm-quirks.md).
Windows quirks: memory/environment.md. Two-plane privacy model sacred:
memory/two-plane-model.md. Every user-facing #N is a uid; spokes print no
#N at all: memory/trust-layer.md, memory/multi-device.md.
