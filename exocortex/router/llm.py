"""Tiered model router. Every LLM call the system makes goes through
``complete()``: tasks name a tier, the tier is a fallback chain of LiteLLM
model ids from config.yaml, and every attempt lands in usage_ledger.

LiteLLM is imported lazily so the daemon (which never calls this) and the
idle bot stay small.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3

from ..config import Config
from ..state.db import now_ms
from . import prices

log = logging.getLogger("exo.router")

PAID_TIERS = ("T2_workhorse", "T3_reasoning")

# an empty tier degrades to the next one down instead of erroring
TIER_DEGRADE = {"T3_reasoning": "T2_workhorse"}

KEY_ENV = {
    "anthropic": "ANTHROPIC_API_KEY",
    "gemini": "GEMINI_API_KEY",
    "groq": "GROQ_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
    "nvidia": "NVIDIA_NIM_API_KEY",
}


class RouterError(Exception):
    pass


class BudgetExceeded(RouterError):
    pass


def _export_keys(cfg: Config) -> None:
    for name, env in KEY_ENV.items():
        val = (cfg.get(f"router.api_keys.{name}") or "").strip()
        if val and not os.environ.get(env):
            os.environ[env] = val


def _record(conn: sqlite3.Connection, model: str, tier: str, task: str,
            tokens_in: int, tokens_out: int, cost: float, ok: bool) -> None:
    provider = model.split("/", 1)[0] if "/" in model else "anthropic"
    conn.execute(
        "INSERT INTO usage_ledger (ts, provider, model, tier, task, tokens_in,"
        " tokens_out, cost_usd, ok) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (now_ms(), provider, model, tier, task, tokens_in, tokens_out, cost, int(ok)),
    )
    conn.commit()


def complete(cfg: Config, conn: sqlite3.Connection, tier: str, task: str,
             messages: list[dict], *, max_tokens: int = 1024,
             suppress_reasoning: bool = False) -> str:
    return complete_info(cfg, conn, tier, task, messages, max_tokens=max_tokens,
                         suppress_reasoning=suppress_reasoning)[0]


def complete_info(cfg: Config, conn: sqlite3.Connection, tier: str, task: str,
                  messages: list[dict], *, max_tokens: int = 1024,
                  suppress_reasoning: bool = False) -> tuple[str, str]:
    """Like complete(), but also returns which model actually answered.
    suppress_reasoning: for per-message hot paths (intent classification) —
    hidden thinking tokens eat the output budget and truncate JSON."""
    models = cfg.get(f"router.tiers.{tier}") or []
    while not models and tier in TIER_DEGRADE:
        tier = TIER_DEGRADE[tier]
        models = cfg.get(f"router.tiers.{tier}") or []
        log.info("tier empty — degrading to %s", tier)
    if not models:
        raise RouterError(f"no models configured for tier {tier}")
    if tier in PAID_TIERS:
        from .budget import check_budget
        check_budget(cfg, conn)

    _export_keys(cfg)
    import litellm  # lazy on purpose

    litellm.suppress_debug_info = True
    litellm.drop_params = True  # providers ignore params they don't support
    # bulk-tier calls suppress "thinking" spend where the provider allows it
    # (gemini 2.5 etc. burn most of max_tokens on hidden reasoning otherwise)
    extra = ({"reasoning_effort": "low"}
             if suppress_reasoning or tier in ("T0_local", "T1_free") else {})
    last_err: Exception | None = None
    for model in models:
        try:
            resp = litellm.completion(
                model=model, messages=messages, max_tokens=max_tokens,
                timeout=180, num_retries=1, **extra,
            )
        except Exception as e:  # provider down / rate limited / bad key — try next
            last_err = e
            log.warning("model %s failed (%s), trying next in %s", model, e, tier)
            _record(conn, model, tier, task, 0, 0, 0.0, ok=False)
            continue
        text = (resp.choices[0].message.content or "").strip()
        usage = getattr(resp, "usage", None)
        tokens_in = int(getattr(usage, "prompt_tokens", 0) or 0)
        tokens_out = int(getattr(usage, "completion_tokens", 0) or 0)
        cost = 0.0
        if tier in PAID_TIERS:
            try:
                # trust litellm when it knows the model — $0 is legitimate for
                # :free models, which are allowed in the workhorse tier
                cost = float(litellm.completion_cost(completion_response=resp) or 0.0)
            except Exception:
                if model.endswith(":free") or "/openrouter/free" in model:
                    cost = 0.0  # :free endpoints are $0 by definition
                else:
                    # UNKNOWN paid model: never record as free, the governor
                    # depends on the ledger — assume opus-level rates
                    cost = prices.estimate(model, tokens_in, tokens_out, assume_paid=True)
                    if prices.lookup(model) is None:
                        log.warning("no price known for model %s — assuming"
                                    " opus-level rates in the ledger", model)
        _record(conn, model, tier, task, tokens_in, tokens_out, cost, ok=True)
        return text, model
    raise RouterError(f"all models failed for tier {tier}; last error: {last_err}")


def parse_json_loose(text: str):
    """Parse JSON out of an LLM reply that may include code fences or prose."""
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.MULTILINE).strip()
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        start, end = t.find("{"), t.rfind("}")
        if start != -1 and end > start:
            return json.loads(t[start : end + 1])
        raise
