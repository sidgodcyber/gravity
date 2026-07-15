"""Token budget governor and the /budget report.

The governor is a hard gate: once month-to-date paid spend reaches the
configured cap, T2/T3 calls raise BudgetExceeded and callers degrade to
T1_free. Recommendations are rule-based — no tokens are spent computing
how to save tokens.
"""

from __future__ import annotations

import datetime as dt
import sqlite3

from ..config import Config
from ..state import db
from . import claude_code
from .llm import BudgetExceeded


def _month_start_ms() -> int:
    now = dt.datetime.now()
    return int(now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000)


def _day_start(days_ago: int = 0) -> dt.datetime:
    d = dt.datetime.now().astimezone() - dt.timedelta(days=days_ago)
    return d.replace(hour=0, minute=0, second=0, microsecond=0)


def month_spend(conn: sqlite3.Connection) -> float:
    row = conn.execute(
        "SELECT COALESCE(SUM(cost_usd), 0) FROM usage_ledger WHERE ts >= ?",
        (_month_start_ms(),),
    ).fetchone()
    return float(row[0])


def check_budget(cfg: Config, conn: sqlite3.Connection) -> None:
    cap = float(cfg.get("router.monthly_budget_usd", 10.0))
    spent = month_spend(conn)
    if spent >= cap:
        raise BudgetExceeded(
            f"monthly budget hit (${spent:.2f} of ${cap:.2f}) — paid tiers locked; "
            "T1_free still works. Raise router.monthly_budget_usd to override."
        )


def _ledger_window(conn: sqlite3.Connection, since_ms: int) -> tuple[float, int]:
    row = conn.execute(
        "SELECT COALESCE(SUM(cost_usd), 0), COALESCE(SUM(tokens_in + tokens_out), 0)"
        " FROM usage_ledger WHERE ts >= ? AND ok = 1",
        (since_ms,),
    ).fetchone()
    return float(row[0]), int(row[1])


def _fmt_tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.0f}k"
    return str(n)


def budget_report(cfg: Config) -> str:
    conn = db.connect(cfg.db_path)
    try:
        return _budget_report(cfg, conn)
    finally:
        conn.close()


def _budget_report(cfg: Config, conn: sqlite3.Connection) -> str:
    now = dt.datetime.now().astimezone()
    cap = float(cfg.get("router.monthly_budget_usd", 10.0))
    today = _day_start()
    week = _day_start(6)

    m_cost, _ = _ledger_window(conn, _month_start_ms())
    d_cost, d_tok = _ledger_window(conn, int(today.timestamp() * 1000))
    w_cost, w_tok = _ledger_window(conn, int(week.timestamp() * 1000))
    day_of_month = max(now.day, 1)
    projected = m_cost / day_of_month * 30

    lines = [f"💰 Budget — {now:%a %b %d}"]
    lines.append(
        f"System API this month: ${m_cost:.2f} of ${cap:.2f}"
        f" · today ${d_cost:.2f} ({_fmt_tokens(d_tok)} tok)"
        f" · 7d ${w_cost:.2f} ({_fmt_tokens(w_tok)} tok)"
    )
    lines.append(
        f"Projected month-end: ${projected:.2f} "
        + ("⚠️ over budget" if projected > cap else "✓ within budget")
    )

    # Claude Code plan usage (parsed transcripts; API-equivalent value)
    events = claude_code.scan_events(cfg.get("claude_code.transcripts_dir"), week)
    s_today = claude_code.summarize(events, today)
    s_week = claude_code.summarize(events, week)
    if s_week.calls:
        lines.append(
            f"Claude Code (plan): today {_fmt_tokens(s_today.total_tokens)} tok"
            f" (~${s_today.equiv_usd:.2f} API-equiv)"
            f" · 7d {_fmt_tokens(s_week.total_tokens)} tok (~${s_week.equiv_usd:.2f})"
        )
        top = sorted(s_week.by_model.items(), key=lambda kv: -kv[1])[:2]
        lines.append("  mostly " + ", ".join(f"{m.split('/')[-1]}" for m, _ in top))
    else:
        lines.append("Claude Code: no transcript usage found in the last 7 days"
                     " (parser found nothing — if you did use it, log with /spent)")

    row = conn.execute(
        "SELECT COALESCE(SUM(amount), 0) FROM spent_log WHERE ts >= ?",
        (int(week.timestamp() * 1000),),
    ).fetchone()
    if row[0]:
        lines.append(f"Manual /spent this week: ${float(row[0]):.2f}")

    lines.append("Advice:")
    lines.extend("  • " + tip for tip in _advice(cfg, cap, m_cost, projected, s_today, s_week, now))
    return "\n".join(lines)


def _advice(cfg: Config, cap: float, m_cost: float, projected: float,
            s_today: "claude_code.UsageSummary", s_week: "claude_code.UsageSummary",
            now: dt.datetime) -> list[str]:
    tips: list[str] = []
    if m_cost >= cap:
        tips.append("Budget cap reached — paid tiers are locked; briefs fall back to T1_free.")
    elif projected > cap:
        tips.append(
            f"On pace for ${projected:.2f}/mo — route summaries to T1_free and keep"
            " T2 for the final brief only."
        )
    else:
        headroom = cap - projected
        tips.append(f"System spend is healthy (~${headroom:.2f}/mo headroom).")

    if s_week.calls:
        daily_avg = s_week.total_tokens / 7
        if daily_avg and s_today.total_tokens > 1.5 * daily_avg:
            tips.append(
                "Heavy Claude Code day (>1.5x your 7-day average) — run remaining"
                " execution tasks on Sonnet and save Opus-class capacity for"
                " tonight's planning/architecture work."
            )
        weekly_budget = float(cfg.get("claude_code.weekly_token_budget", 0) or 0)
        if weekly_budget > 0:
            used = s_week.total_tokens / weekly_budget
            days_left = 8 - now.isoweekday()  # Mon=7 left ... Sun=1 left
            tips.append(
                f"Plan week: {used:.0%} of your weekly token budget used with"
                f" ~{days_left} day(s) left"
                + (" — slow down or shift bulk work to system T1_free." if used > 0.8 else ".")
            )
    return tips
