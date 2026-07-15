"""Generate auto-start plumbing for this machine: Task Scheduler entries on
Windows, systemd user units + timers on Linux. `exo install` writes the files
and prints the commands; `exo install --apply` registers them (Windows)."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from ..config import REPO_ROOT, Config


def _startup_dir() -> Path:
    return (Path(os.environ.get("APPDATA", Path.home() / "AppData/Roaming"))
            / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup")

# Time-triggered tasks: Task Scheduler, per-user, no admin needed.
WIN_TASKS = [
    ("Exocortex Nightly", "nightly.cmd", "/SC DAILY /ST {run_time}"),
    ("Exocortex Brief", "brief.cmd", "/SC DAILY /ST {brief_time}"),
    ("Exocortex Weekly", "weekly.cmd", "/SC WEEKLY /D {weekly_day} /ST {weekly_time}"),
]
# A spoke schedules ONLY the queue push (before the hub's nightly merge).
WIN_TASKS_SPOKE = [
    ("Exocortex Queue Push", "push.cmd", "/SC DAILY /ST {push_time}"),
]
# Always-on processes launch at logon via the Startup folder — creating
# ONLOGON scheduled tasks requires elevation on locked-down machines, the
# Startup folder does not.
WIN_STARTUP = [("Exocortex Daemon", "daemon.cmd"), ("Exocortex Bot", "bot.cmd")]
WIN_STARTUP_SPOKE = [("Exocortex Daemon", "daemon.cmd")]


def win_tasks_for(cfg: Config) -> list[tuple[str, str, str]]:
    """The role decides what may even be REGISTERED — a spoke structurally
    never gets bot/nightly/brief/weekly entries."""
    return WIN_TASKS_SPOKE if cfg.is_spoke else WIN_TASKS


def win_startup_for(cfg: Config) -> list[tuple[str, str]]:
    return WIN_STARTUP_SPOKE if cfg.is_spoke else WIN_STARTUP

WEEKDAY_WIN = {"mon": "MON", "tue": "TUE", "wed": "WED", "thu": "THU",
               "fri": "FRI", "sat": "SAT", "sun": "SUN"}
WEEKDAY_SYSTEMD = {"mon": "Mon", "tue": "Tue", "wed": "Wed", "thu": "Thu",
                   "fri": "Fri", "sat": "Sat", "sun": "Sun"}


def _venv_python(windowed: bool = False) -> Path:
    exe = Path(sys.executable)
    if windowed and exe.name.lower() == "python.exe":
        w = exe.with_name("pythonw.exe")
        if w.exists():
            return w
    return exe


def _write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="\n")
    return path


def generate_windows(cfg: Config) -> list[str]:
    out = REPO_ROOT / "ops" / "windows"
    pyw = _venv_python(windowed=True)
    py = _venv_python()
    cmds = {
        "daemon.cmd": f'@echo off\ncd /d "{REPO_ROOT}"\n"{pyw}" -m exocortex.cli daemon run\n',
        "bot.cmd": f'@echo off\ncd /d "{REPO_ROOT}"\n"{pyw}" -m exocortex.cli bot\n',
        "nightly.cmd": f'@echo off\ncd /d "{REPO_ROOT}"\n"{py}" -m exocortex.cli cycle nightly\n',
        "brief.cmd": f'@echo off\ncd /d "{REPO_ROOT}"\n"{py}" -m exocortex.cli brief --send\n',
        "weekly.cmd": f'@echo off\ncd /d "{REPO_ROOT}"\n"{py}" -m exocortex.cli cycle weekly\n',
        "push.cmd": f'@echo off\ncd /d "{REPO_ROOT}"\n"{py}" -m exocortex.cli sync push\n',
    }
    for name, content in cmds.items():
        _write(out / name, content)
    register = []
    for task, cmd, sched in win_tasks_for(cfg):
        sched = sched.format(
            run_time=cfg.get("cycle.run_time", "03:30"),
            brief_time=cfg.get("cycle.brief_time", "07:00"),
            weekly_day=WEEKDAY_WIN.get(
                str(cfg.get("curriculum.weekly_question_day", "sun")).lower()[:3], "SUN"),
            weekly_time=cfg.get("curriculum.weekly_question_time", "19:00"),
            push_time=cfg.get("sync.spoke_push_time", "03:00"),
        )
        register.append(f'schtasks /Create /F /TN "{task}" {sched} /TR "{out / cmd}"')
    _write(out / "register-tasks.cmd", "@echo off\n" + "\n".join(register) + "\n")
    # unregister covers BOTH roles' task names, so switching roles can't strand entries
    _write(out / "unregister-tasks.cmd", "@echo off\n" + "\n".join(
        f'schtasks /Delete /F /TN "{t}"'
        for t, _, _ in WIN_TASKS + WIN_TASKS_SPOKE) + "\n")
    return register


def generate_linux(cfg: Config) -> None:
    out = REPO_ROOT / "ops" / "linux"
    py = _venv_python()
    run_time = cfg.get("cycle.run_time", "03:30")
    brief_time = cfg.get("cycle.brief_time", "07:00")

    def service(name: str, args: str, description: str, restart: bool) -> str:
        extra = "Restart=on-failure\nRestartSec=10\n" if restart else ""
        return (f"[Unit]\nDescription={description}\n\n[Service]\n"
                f"WorkingDirectory={REPO_ROOT}\nExecStart={py} -m exocortex.cli {args}\n"
                f"{extra}\n[Install]\nWantedBy=default.target\n")

    def timer(name: str, at: str) -> str:
        return (f"[Unit]\nDescription=Run {name} daily\n\n[Timer]\n"
                f"OnCalendar=*-*-* {at}:00\nPersistent=true\n\n[Install]\nWantedBy=timers.target\n")

    _write(out / "exocortex-daemon.service",
           service("daemon", "daemon run", "Exocortex capture daemon", True))
    _write(out / "exocortex-bot.service",
           service("bot", "bot", "Exocortex Telegram bot", True))
    _write(out / "exocortex-nightly.service",
           service("nightly", "cycle nightly", "Exocortex nightly cycle", False))
    _write(out / "exocortex-nightly.timer", timer("nightly cycle", run_time))
    _write(out / "exocortex-brief.service",
           service("brief", "brief --send", "Exocortex morning brief", False))
    _write(out / "exocortex-brief.timer", timer("morning brief", brief_time))
    weekly_day = WEEKDAY_SYSTEMD.get(
        str(cfg.get("curriculum.weekly_question_day", "sun")).lower()[:3], "Sun")
    weekly_time = cfg.get("curriculum.weekly_question_time", "19:00")
    _write(out / "exocortex-weekly.service",
           service("weekly", "cycle weekly", "Exocortex weekly check-in", False))
    _write(out / "exocortex-weekly.timer",
           (f"[Unit]\nDescription=Weekly outcome check-in\n\n[Timer]\n"
            f"OnCalendar={weekly_day} *-*-* {weekly_time}:00\nPersistent=true\n\n"
            f"[Install]\nWantedBy=timers.target\n"))
    _write(out / "install.sh", f"""#!/usr/bin/env bash
set -e
mkdir -p ~/.config/systemd/user
cp "{out}"/exocortex-*.service "{out}"/exocortex-*.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now exocortex-daemon.service exocortex-bot.service
systemctl --user enable --now exocortex-nightly.timer exocortex-brief.timer exocortex-weekly.timer
echo "installed. check: systemctl --user status exocortex-daemon"
# survive logout: loginctl enable-linger $USER
""")


def status(cfg: Config) -> str:
    """What's registered and when each next fires."""
    if sys.platform == "win32":
        lines = [f"Role: {cfg.role} (device: {cfg.device_name})",
                 "Scheduled tasks (Windows Task Scheduler):"]
        any_found = False
        for task, _, _ in win_tasks_for(cfg):
            r = subprocess.run(
                ["schtasks", "/Query", "/TN", task, "/FO", "LIST"],
                capture_output=True, text=True)
            if r.returncode != 0:
                lines.append(f"  ✗ {task}: NOT registered")
                continue
            any_found = True
            nxt = next((ln.split(":", 1)[1].strip()
                        for ln in r.stdout.splitlines()
                        if ln.strip().startswith("Next Run Time")), "?")
            st = next((ln.split(":", 1)[1].strip()
                       for ln in r.stdout.splitlines()
                       if ln.strip().startswith("Status")), "")
            lines.append(f"  ✓ {task}: next {nxt} [{st}]")
        if not any_found:
            lines.append("  (none registered — run `exo install --apply`)")
        lines.append("\nLogon launch (Startup folder):")
        sd = _startup_dir()
        for task, cmd in win_startup_for(cfg):
            present = (sd / cmd).exists()
            lines.append(f"  {'✓' if present else '✗'} {task}: "
                         + ("runs at next logon" if present else "NOT installed"))
        return "\n".join(lines)
    # Linux: report the timer files + systemctl list-timers if available
    out = REPO_ROOT / "ops" / "linux"
    lines = ["systemd user timers:"]
    r = subprocess.run(["systemctl", "--user", "list-timers", "--all", "--no-pager"],
                       capture_output=True, text=True)
    if r.returncode == 0:
        for ln in r.stdout.splitlines():
            if "exocortex" in ln:
                lines.append("  " + ln.strip())
        if len(lines) == 1:
            lines.append("  none active — run `bash " + str(out / "install.sh") + "`")
    else:
        lines.append("  (systemctl unavailable; unit files are in " + str(out) + ")")
    return "\n".join(lines)


def uninstall(cfg: Config) -> str:
    if sys.platform == "win32":
        out = []
        # remove BOTH roles' entries — a role switch must not strand jobs
        for task, _, _ in WIN_TASKS + WIN_TASKS_SPOKE:
            r = subprocess.run(["schtasks", "/Delete", "/F", "/TN", task],
                               capture_output=True, text=True)
            out.append(f"  {'removed' if r.returncode == 0 else 'not present'}: {task}")
        sd = _startup_dir()
        for task, cmd in WIN_STARTUP:
            p = sd / cmd
            if p.exists():
                p.unlink()
                out.append(f"  removed from Startup: {task}")
        return "Unregistered:\n" + "\n".join(out)
    out = REPO_ROOT / "ops" / "linux"
    _write(out / "uninstall.sh", """#!/usr/bin/env bash
systemctl --user disable --now exocortex-daemon.service exocortex-bot.service \\
  exocortex-nightly.timer exocortex-brief.timer exocortex-weekly.timer 2>/dev/null
rm -f ~/.config/systemd/user/exocortex-*.service ~/.config/systemd/user/exocortex-*.timer
systemctl --user daemon-reload
echo "exocortex units removed"
""")
    return "run: bash " + str(out / "uninstall.sh")


def install(cfg: Config, apply: bool = False) -> None:
    register = generate_windows(cfg)
    generate_linux(cfg)
    print(f"generated launchers under {REPO_ROOT / 'ops'}")
    print("(files embed THIS machine's paths — re-run `exo install` on each machine)")
    if sys.platform == "win32":
        win = REPO_ROOT / "ops" / "windows"
        if apply:
            for cmd in register:
                r = subprocess.run(cmd, shell=True, capture_output=True, text=True)
                st = "ok" if r.returncode == 0 else f"FAILED: {r.stderr.strip()}"
                print(f"  {cmd.split(chr(34))[1]}: {st}")
            # always-on processes launch at logon via the Startup folder (no
            # admin); a spoke gets ONLY the daemon — never bot.cmd
            sd = _startup_dir()
            sd.mkdir(parents=True, exist_ok=True)
            for task, cmd in win_startup_for(cfg):
                _write(sd / cmd, (win / cmd).read_text(encoding="utf-8"))
                print(f"  {task}: added to Startup folder (launches at logon)")
            if cfg.is_spoke:
                if (sd / "bot.cmd").exists():
                    (sd / "bot.cmd").unlink()
                    print("  removed stale bot.cmd from Startup (this is a spoke)")
                # a hub→spoke role switch must not leave hub jobs firing
                for task, _, _ in WIN_TASKS:
                    r = subprocess.run(["schtasks", "/Delete", "/F", "/TN", task],
                                       capture_output=True, text=True)
                    if r.returncode == 0:
                        print(f"  removed stale hub task: {task}")
        else:
            print("to register time-based tasks, run:")
            print(f"  {win / 'register-tasks.cmd'}")
            if cfg.is_spoke:
                print("daemon: copy daemon.cmd into your Startup folder"
                      " (spokes run NO bot)")
            else:
                print("daemon+bot: copy daemon.cmd and bot.cmd into your Startup folder")
            print("(or just re-run: exo install --apply)")
    else:
        print("to install systemd user units, run:")
        print(f"  bash {REPO_ROOT / 'ops' / 'linux' / 'install.sh'}")
