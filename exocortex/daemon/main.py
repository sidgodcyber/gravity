"""Capture daemon: one lean process. Sources run as threads and push through
the firewall pipeline; this main loop owns the database connection, flushes
batches, polls the work-mode switch, and runs periodic browser imports.
LiteLLM is never imported here — the daemon stays small.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import logging.handlers
import threading
import time

import psutil

from ..config import Config
from ..state import db
from .filter import Firewall
from .pipeline import Pipeline
from .sources import ai_trail, browser
from .sources.clipboard import ClipboardSource
from .sources.files import start_file_watcher
from .sources.window import WindowSource

log = logging.getLogger("exo.daemon")

STATUS_FILE = "daemon_status.json"
CONTROL_POLL_S = 2.0
STATUS_WRITE_S = 10.0


def _setup_logging(cfg: Config) -> None:
    logdir = cfg.state_dir / "logs"
    logdir.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(
        logdir / "daemon.log", maxBytes=1_000_000, backupCount=2, encoding="utf-8"
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s"))
    logging.basicConfig(level=logging.INFO, handlers=[handler, logging.StreamHandler()])


def run(cfg: Config) -> None:
    if cfg.path is None:
        raise SystemExit(
            "no config.yaml found — refusing to capture with an empty firewall.\n"
            "Copy config.example.yaml to config.yaml and fill in the blocklist first."
        )
    _setup_logging(cfg)
    db.split_plane_b(cfg)  # idempotent: trail rows live in local.db, not sync
    conn = db.connect(cfg.db_path)          # synced db: control/work_mode only
    local_conn = db.local_connect(cfg)      # Plane B trail: all capture writes
    firewall = Firewall(cfg.data["firewall"])
    cap = cfg.data["capture"]
    pipeline = Pipeline(local_conn, firewall, cfg.device_id)
    pipeline.set_work_mode(db.get_control(conn, "work_mode", "off") == "on")
    if firewall.forced_work_mode:
        log.info("host is in work_mode_hosts — permanent work mode, manual logs only")

    stop = threading.Event()
    WindowSource(pipeline, float(cap["window_poll_s"]), stop).start()
    ClipboardSource(
        pipeline, float(cap["clipboard_poll_s"]), int(cap["clipboard_max_chars"]), stop
    ).start()
    observer = start_file_watcher(pipeline, cap.get("watched_folders") or [])

    proc = psutil.Process()
    started = time.time()
    last_flush = last_control = last_status = 0.0
    last_browser = float(db.get_control(local_conn, "browser_import_last", "0") or 0)
    last_ai_trail = float(db.get_control(local_conn, "ai_trail_last", "0") or 0)
    log.info("daemon started (device=%s)", cfg.device_id)

    import_thread: threading.Thread | None = None
    ai_thread: threading.Thread | None = None

    def _refresh_work_mode() -> None:
        new_mode = db.get_control(conn, "work_mode", "off") == "on"
        if pipeline.work_mode and not new_mode:
            # observed an on->off flip (however it was made): raise the
            # browser floor so work-period history is never imported
            db.set_control(
                conn, "browser_floor_us", str(int(time.time() * 1_000_000))
            )
        pipeline.set_work_mode(new_mode)

    try:
        while True:
            time.sleep(1.0)
            now = time.time()

            if now - last_control >= CONTROL_POLL_S:
                _refresh_work_mode()
                last_control = now

            if (
                now - last_flush >= float(cap["flush_interval_s"])
                or pipeline.pending() >= int(cap["flush_max_events"])
            ):
                # fresh read right before writing: a toggle from another
                # process can never race a flush onto disk
                _refresh_work_mode()
                last_control = now
                pipeline.flush()
                last_flush = now

            bi = cap.get("browser_import") or {}
            if (
                bi.get("enabled")
                and not pipeline.work_mode
                and not firewall.forced_work_mode
                and now - last_browser >= float(bi.get("interval_hours", 6)) * 3600
                and (import_thread is None or not import_thread.is_alive())
            ):
                # own thread + own db connections: copying a fat History file
                # must not stall flushes or the work-mode poll
                floor_us = int(db.get_control(conn, "browser_floor_us", "0") or 0)

                def _run_import() -> None:
                    ic = db.local_connect(cfg)
                    try:
                        n = browser.import_history(ic, pipeline, floor_us)
                        log.info("browser import: %d entries submitted", n)
                    except Exception:
                        log.exception("browser import failed")
                    finally:
                        ic.close()

                import_thread = threading.Thread(
                    target=_run_import, name="browser-import", daemon=True
                )
                import_thread.start()
                last_browser = now
                db.set_control(local_conn, "browser_import_last", str(now))

            if (
                not pipeline.work_mode
                and not firewall.forced_work_mode
                and now - last_ai_trail >= 3600  # hourly is plenty
                and (ai_thread is None or not ai_thread.is_alive())
            ):
                # own thread + own connection: a big transcripts rglob must not
                # stall flushes or the work-mode poll
                def _run_ai_import() -> None:
                    ic = db.local_connect(cfg)
                    try:
                        n = ai_trail.import_prompts(cfg, ic, pipeline)
                        if n:
                            log.info("ai trail: %d prompts submitted", n)
                    except Exception:
                        log.exception("ai trail import failed")
                    finally:
                        ic.close()

                ai_thread = threading.Thread(
                    target=_run_ai_import, name="ai-trail-import", daemon=True
                )
                ai_thread.start()
                last_ai_trail = now
                db.set_control(local_conn, "ai_trail_last", str(now))

            if now - last_status >= STATUS_WRITE_S:
                _write_status(cfg, proc, pipeline, firewall, started, running=True)
                last_status = now
    except KeyboardInterrupt:
        log.info("daemon stopping")
    finally:
        stop.set()
        if observer:
            observer.stop()
        pipeline.flush()
        _write_status(cfg, proc, pipeline, firewall, started, running=False)
        local_conn.close()
        conn.close()


def _write_status(cfg: Config, proc: psutil.Process, pipeline: Pipeline,
                  firewall: Firewall, started: float, running: bool) -> None:
    status = {
        "running": running,
        "pid": proc.pid,
        "started": started,
        "updated": time.time(),
        "rss_mb": round(proc.memory_info().rss / (1024 * 1024), 1),
        "mode": "work" if (pipeline.work_mode or firewall.forced_work_mode) else "capture",
        "submitted": pipeline.submitted,
        "dropped": pipeline.dropped,
        "written": pipeline.written,
        "pending": pipeline.pending(),
    }
    try:
        (cfg.state_dir / STATUS_FILE).write_text(json.dumps(status), encoding="utf-8")
    except OSError:
        pass


def status_text(cfg: Config) -> str:
    """Human-readable status for `exo daemon status`."""
    lines = []
    status_path = cfg.state_dir / STATUS_FILE
    if status_path.exists():
        s = json.loads(status_path.read_text(encoding="utf-8"))
        alive = (
            s.get("running")
            and psutil.pid_exists(int(s["pid"]))
            and time.time() - s["updated"] < 60
        )
        lines.append(f"daemon: {'RUNNING' if alive else 'NOT RUNNING'}"
                     + (f" (pid {s['pid']})" if alive else ""))
        if alive:
            lines.append(f"mode: {s['mode']}")
            lines.append(f"RAM: {s['rss_mb']} MB")
            lines.append(
                f"since start: {s['submitted']} captured, {s['dropped']} dropped by firewall,"
                f" {s['written']} written, {s['pending']} queued"
            )
    else:
        lines.append("daemon: NOT RUNNING (never started on this state dir)")

    local_conn = db.local_connect(cfg)
    midnight = dt.datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    today_ms = int(midnight.timestamp() * 1000)
    n_today = local_conn.execute(
        "SELECT count(*) FROM life_stream WHERE ts >= ?", (today_ms,)
    ).fetchone()[0]
    total = local_conn.execute("SELECT count(*) FROM life_stream").fetchone()[0]
    local_conn.close()
    conn = db.connect(cfg.db_path)
    work = db.get_control(conn, "work_mode", "off")
    conn.close()
    lines.append(f"events today: {n_today} (total {total})")
    lines.append(f"work mode switch: {work}")
    return "\n".join(lines)
