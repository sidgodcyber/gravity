"""`exo` — the one command line for everything."""

from __future__ import annotations

import argparse
import sys

from .config import Config
from .state import db

# Commands a SPOKE may run. Everything else needs the canonical brain and
# the hub's jobs — running it against a spoke's staging db would show or
# mutate data that never reaches the real memory. This gate (plus the
# independent refusals inside run_bot and sync push/pull) is the structural
# fix for the two-bots-one-token class of bug.
SPOKE_COMMANDS = {"daemon", "work", "log", "sync", "guide", "transcribe",
                  "install", "uninstall", "migrate", "setup"}


def _spoke_refusal(cfg: Config, cmd: str) -> str:
    hub = cfg.get("hub_name") or "the hub machine"
    return (f"this machine is a spoke ({cfg.device_name}) — `exo {cmd}` runs"
            f" on {hub} only.\nSpokes capture and push; talk to Gravity via"
            f" the Telegram bot (hosted on {hub}).\n"
            f"Spoke commands: {', '.join(sorted(SPOKE_COMMANDS))}")


def main(argv=None) -> int:
    # Windows consoles default to cp1252, which chokes on emoji in reports
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    p = argparse.ArgumentParser(prog="exo", description="Exocortex — personal agentic OS")
    p.add_argument("--config", default=None, help="path to config.yaml (default: repo root)")
    sub = p.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("daemon", help="capture daemon")
    d.add_argument("action", choices=["run", "status"])

    w = sub.add_parser("work", help="work-mode firewall switch")
    w.add_argument("state", choices=["on", "off", "status"])

    lg = sub.add_parser("log", help="record a reflection (same as Telegram /log)")
    lg.add_argument("text", nargs="+")

    ask = sub.add_parser("ask", help="ask a question over your state store")
    ask.add_argument("question", nargs="+")

    sub.add_parser("bot", help="run the Telegram bot (long-polling)")

    cy = sub.add_parser("cycle", help="batch cycles")
    cy.add_argument("action", choices=["nightly", "weekly"])

    msg = sub.add_parser("msg", help="classify + store a message (as the bot does)")
    msg.add_argument("text", nargs="+")

    sub.add_parser("tasks", help="open tasks grouped by client/project")
    sub.add_parser("due", help="tasks due soon (overdue first)")
    dn = sub.add_parser("done", help="mark a task done")
    dn.add_argument("id", type=int)
    dr = sub.add_parser("drop", help="drop a task")
    dr.add_argument("id", type=int)
    tk = sub.add_parser("task", help="force-create a task")
    tk.add_argument("text", nargs="+")
    rc = sub.add_parser("recent", help="last stored records with IDs")
    rc.add_argument("n", type=int, nargs="?", default=10)
    dl = sub.add_parser("delete", help="delete a stored record (+ its descendants)")
    dl.add_argument("id", type=int)
    dl.add_argument("--yes", action="store_true", help="skip the confirmation")
    src = sub.add_parser("source", help="where a record came from")
    src.add_argument("id", type=int)
    gd = sub.add_parser("guide", help="Gravity's rulebook")
    gd.add_argument("topic", nargs="?", default="")
    gd.add_argument("--write", action="store_true", help="regenerate USER_GUIDE.md")
    sub.add_parser("backfill-history", help="one-time full browser-history import")

    sub.add_parser("gaps", help="ranked foundation-gaps list with evidence")

    ln = sub.add_parser("learned", help="mark a concept as understood")
    ln.add_argument("topic", nargs="+")

    wk = sub.add_parser("week", help="log this week's outcome (text)")
    wk.add_argument("text", nargs="+")

    tr = sub.add_parser("transcribe", help="transcribe an audio file locally")
    tr.add_argument("audio", help="path to audio file (ogg/oga/mp3/wav)")

    br = sub.add_parser("brief", help="morning brief")
    br.add_argument("--send", action="store_true", help="deliver via Telegram too")

    sub.add_parser("budget", help="token/cost usage report")

    sp = sub.add_parser("spent", help="manually log Claude Code / plan usage")
    sp.add_argument("amount", type=float, help="estimated USD (or tokens if you prefer)")
    sp.add_argument("what", nargs="*", default=[])

    sy = sub.add_parser("sync", help="encrypted sync of the state files")
    sy.add_argument("action", choices=["push", "pull", "merge"],
                    help="push: brain bundle (hub) / capture queue (spoke);"
                         " pull: restore brain (hub); merge: import spoke batches (hub)")
    sy.add_argument("--force", action="store_true", help="pull: overwrite existing state")

    sub.add_parser("devices", help="hub: known devices, last batches, pending merges")

    se = sub.add_parser("setup", help="first-run setup helpers")
    se.add_argument("what", choices=["spoke"], help="spoke: write a capture-only config")
    se.add_argument("--device", required=True, help="this machine's name, e.g. desktop")
    se.add_argument("--hub", default="", help="the hub machine's name, e.g. laptop")

    st = sub.add_parser("style", help="style corpus")
    st.add_argument("action", choices=["import"])
    st.add_argument("path", help="file or directory of writing samples")
    st.add_argument("--kind", default="", help="email | post | message | caption")

    emb = sub.add_parser("embed", help="(re)embed style corpus and recent text")
    emb.add_argument("--all", action="store_true", help="re-embed everything")

    ins = sub.add_parser("install", help="register auto-start tasks for this OS")
    ins.add_argument("--apply", action="store_true", help="actually register tasks (Windows)")
    ins.add_argument("--status", action="store_true", help="show what's registered + next fire")

    sub.add_parser("uninstall", help="remove the auto-start tasks")

    sub.add_parser("migrate", help="apply pending database migrations")

    gr = sub.add_parser("graph", help="knowledge-graph tools")
    gr.add_argument("action", choices=["export"], help="export: write the Obsidian vault")
    gr.add_argument("--path", default=None, help="vault location (default from config)")

    args = p.parse_args(argv)
    cfg = Config.load(args.config)

    from .config import ConfigError
    try:
        is_spoke = cfg.is_spoke  # raises on a role typo — fail closed, loudly
    except ConfigError as e:
        print(f"config error: {e}")
        return 2
    if is_spoke and args.cmd not in SPOKE_COMMANDS:
        print(_spoke_refusal(cfg, args.cmd))
        return 2

    if args.cmd == "daemon":
        from .daemon import main as daemon_main
        if args.action == "run":
            daemon_main.run(cfg)
        else:
            print(daemon_main.status_text(cfg))
        return 0

    if args.cmd == "work":
        conn = db.connect(cfg.db_path)
        if args.state == "status":
            print(f"work mode: {db.get_control(conn, 'work_mode', 'off')}")
        else:
            db.set_work_mode(conn, args.state == "on")
            print(f"work mode set to {args.state}"
                  + (" — capture suspended (manual /log still works)" if args.state == "on" else ""))
        conn.close()
        return 0

    if args.cmd == "migrate":
        moved = db.split_plane_b(cfg)
        if moved:
            print(f"plane split: moved {moved} trail rows into local.db")
        conn = db.connect(cfg.db_path)  # connect() applies migrations
        from .state.uids import backfill as uid_backfill
        minted = uid_backfill(conn)
        if minted:
            print(f"uid registry: minted {minted} user-facing ids")
        print(f"synced db schema version: {db.applied_version(conn)}")
        # relabel hostname-stamped rows to the friendly device_name (same
        # machine, truthful rename — makes provenance read "on laptop")
        import socket
        host = socket.gethostname()
        if cfg.data.get("device_name") and cfg.device_name != host:
            n = 0
            for sql in ("UPDATE reflections SET device = ? WHERE device = ?",
                        "UPDATE life_stream SET device = ? WHERE device = ?"):
                n += conn.execute(sql, (cfg.device_name, host)).rowcount
            conn.commit()
            lconn = db.local_connect(cfg)
            n += lconn.execute("UPDATE life_stream SET device = ? WHERE device = ?",
                               (cfg.device_name, host)).rowcount
            lconn.commit()
            lconn.close()
            if n:
                print(f"device labels: {n} rows relabeled {host} → {cfg.device_name}")
        conn.close()
        lconn = db.local_connect(cfg)
        print(f"local db schema version: {db.applied_version(lconn)}")
        lconn.close()
        return 0

    if args.cmd == "graph":
        from .vault import export_vault
        if args.path:
            cfg.data.setdefault("vault", {})["path"] = args.path
        s = export_vault(cfg)
        print(f"vault: {s['vault']}")
        print(f"{s['notes']} notes — {s['written']} written,"
              f" {s['unchanged']} unchanged, {s['removed']} removed")
        print("open the folder as a vault in Obsidian; start at '_START HERE.md'")
        return 0

    if args.cmd == "log":
        from .extract.reflections import add_reflection
        text = " ".join(args.text)
        conn = db.connect(cfg.db_path)
        # spokes stage the reflection for the hub: no LLM call (no keys
        # here), and no #id shown — ids are minted by the hub's namespace
        rid = add_reflection(conn, cfg, text, extract_now=not cfg.is_spoke)
        if cfg.is_spoke:
            print("queued for the hub — lands in memory at the next"
                  " `exo sync push` + nightly merge")
        else:
            from .state.uids import uid_for
            print(f"logged reflection #{uid_for(conn, 'reflections', rid)}")
        conn.close()
        return 0

    if args.cmd == "ask":
        from .retrieval.ask import answer
        print(answer(Config.load(args.config), " ".join(args.question)))
        return 0

    if args.cmd == "bot":
        from .bot.main import run_bot
        run_bot(cfg)
        return 0

    if args.cmd == "cycle":
        from .cycles.nightly import run_nightly, run_weekly
        if args.action == "nightly":
            run_nightly(cfg)
        else:
            ok = run_weekly(cfg)
            print("weekly check-in sent" if ok else "weekly check-in NOT sent (telegram?)")
        return 0

    if args.cmd == "msg":
        from .intents.route import handle_incoming
        out = handle_incoming(cfg, " ".join(args.text), source="cli")
        print(out["reply"])
        if out.get("followup"):
            print(out["followup"])
        if out.get("pending"):
            print(f"[pending: {out['pending']['type']} — resolve in the bot]")
        return 0

    if args.cmd in ("tasks", "due"):
        from .intents.tasks import due_soon, open_tasks
        conn = db.connect(cfg.db_path)
        print(open_tasks(conn) if args.cmd == "tasks" else due_soon(conn))
        conn.close()
        return 0

    if args.cmd in ("done", "drop"):
        from .intents.tasks import set_status_by_uid
        conn = db.connect(cfg.db_path)
        txt = set_status_by_uid(conn, args.id, "done" if args.cmd == "done" else "dropped")
        conn.close()
        print(f"{args.cmd}: #{args.id} {txt}" if txt else f"#{args.id} isn't a task.")
        return 0

    if args.cmd == "task":
        from .intents.classify import classify
        from .intents.dispatch import dispatch
        from .intents.route import set_pending
        conn = db.connect(cfg.db_path)
        c = classify(cfg, conn, " ".join(args.text), forced_intent="task")
        result = dispatch(cfg, conn, c, source="cli")
        print(result["reply"])
        if result.get("pending"):
            set_pending(conn, result["pending"])
            print("(answer with: exo msg yes — or reply in Telegram)")
        conn.close()
        return 0

    if args.cmd == "recent":
        from .intents.memory_edit import recent
        conn = db.connect(cfg.db_path)
        print(recent(conn, args.n))
        conn.close()
        return 0

    if args.cmd == "delete":
        # confirm-before-destroy holds on the CLI too
        from .intents.memory_edit import delete_by_uid
        from .state.uids import resolve as uid_resolve
        conn = db.connect(cfg.db_path)
        if not args.yes:
            ref = uid_resolve(conn, args.id)
            if ref is None:
                print(f"No record #{args.id}.")
            else:
                from .intents.route import _peek
                print(f"#{args.id}: {_peek(conn, ref[0], ref[1])[:100]}")
                print(f"Re-run with --yes to delete:  exo delete {args.id} --yes")
            conn.close()
            return 0
        ok, msg_out = delete_by_uid(cfg, conn, args.id)
        print(msg_out)
        conn.close()
        return 0

    if args.cmd == "source":
        from .intents.provenance import describe
        conn = db.connect(cfg.db_path)
        print(describe(cfg, conn, args.id))
        conn.close()
        return 0

    if args.cmd == "guide":
        from .guide import guide_text, write_user_guide
        if args.write:
            print("wrote", write_user_guide())
        else:
            print(guide_text(args.topic))
        return 0

    if args.cmd == "backfill-history":
        from .daemon.sources.backfill import backfill_history
        print(backfill_history(cfg))
        return 0

    if args.cmd == "gaps":
        from .curriculum.spaced import gaps_report
        print(gaps_report(cfg))
        return 0

    if args.cmd == "learned":
        from .curriculum.spaced import mark_learned
        conn = db.connect(cfg.db_path)
        print(mark_learned(cfg, conn, " ".join(args.topic)))
        conn.close()
        return 0

    if args.cmd == "week":
        from .curriculum.spaced import store_weekly_outcome
        rid = store_weekly_outcome(cfg, " ".join(args.text))
        print(f"week logged #{rid}")
        return 0

    if args.cmd == "transcribe":
        from .curriculum.transcribe import transcribe_file
        print(transcribe_file(cfg, args.audio))
        return 0

    if args.cmd == "brief":
        from .cycles.brief import brief_command
        print(brief_command(cfg, send=args.send))
        return 0

    if args.cmd == "budget":
        from .router.budget import budget_report
        print(budget_report(cfg))
        return 0

    if args.cmd == "spent":
        conn = db.connect(cfg.db_path)
        conn.execute(
            "INSERT INTO spent_log (ts, what, amount) VALUES (?, ?, ?)",
            (db.now_ms(), " ".join(args.what) or "manual entry", args.amount),
        )
        conn.commit()
        conn.close()
        print("recorded")
        return 0

    if args.cmd == "sync":
        from .sync.main import SyncError
        try:
            if args.action == "push":
                if cfg.is_spoke:
                    from .sync.spoke import push_queue
                    print(push_queue(cfg))
                else:
                    from .sync.main import push
                    print(push(cfg))
            elif args.action == "pull":
                from .sync.main import pull
                print(pull(cfg, force=args.force))
            else:  # merge — hub consumes spoke capture batches
                if cfg.is_spoke:
                    print(_spoke_refusal(cfg, "sync merge"))
                    return 2
                from .sync.merge import merge_batches, merge_summary
                print(merge_summary(merge_batches(cfg)))
        except SyncError as e:
            print(f"sync: {e}")
            return 1
        return 0

    if args.cmd == "devices":
        from .sync.merge import devices_report
        print(devices_report(cfg))
        return 0

    if args.cmd == "setup":
        from .ops.setup import setup_spoke
        print(setup_spoke(cfg, args.device, args.hub))
        return 0

    if args.cmd == "style":
        from .retrieval.style import import_samples
        n = import_samples(cfg, args.path, kind=args.kind)
        print(f"imported {n} samples")
        return 0

    if args.cmd == "embed":
        from .retrieval.embeddings import embed_pending
        n = embed_pending(cfg, reembed_all=getattr(args, "all", False))
        print(f"embedded {n} rows")
        return 0

    if args.cmd == "install":
        from .ops import installer
        if args.status:
            print(installer.status(cfg))
        else:
            installer.install(cfg, apply=args.apply)
        return 0

    if args.cmd == "uninstall":
        from .ops import installer
        print(installer.uninstall(cfg))
        return 0

    p.error(f"unknown command {args.cmd}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
