# LLM call quirks learned during live verification (2026-07-07)

- T2_workhorse currently runs on FREE models (user has no paid Anthropic key
  yet): openrouter nemotron-3-ultra-550b:free is the front-runner and is
  genuinely strong. OpenRouter free tier: ~20 req/min, 200 req/day; slugs
  churn monthly — the `openrouter/openrouter/free` auto-router entry is the
  churn-proof backstop. NVIDIA NIM (nvidia_nim/ prefix, NVIDIA_NIM_API_KEY)
  has ONE-TIME credits, keep it low in the chain.
- Cost rule in llm.py: trust litellm.completion_cost when it succeeds ($0 is
  legit for :free); on failure, ":free" models are $0 by definition, unknown
  paid models get conservative opus-level estimates. Don't "simplify" this —
  each branch guards the budget governor a different way.
- Empty T3_reasoning degrades to T2_workhorse automatically (TIER_DEGRADE).

- gemini-2.5-flash (and sonnet-5 adaptive) are THINKING models: reasoning
  consumes completion tokens invisibly. A max_tokens of 300 truncated a tiny
  JSON extraction mid-array ("Expecting value" parse errors = suspect
  truncation first). Budgets now: extraction 1200, ask 1600, day summary
  1000, brief 4000. Don't "optimize" these downward.
- litellm's anthropic provider lazily imports `tenacity` — it must be a
  declared dependency or T2/T3 fail with a confusing import error.
- httpx logs full request URLs at INFO level, which for Telegram includes
  the BOT TOKEN in every getUpdates line. run_bot() silences httpx/httpcore
  to WARNING — keep it that way in any new long-polling code.
- T2 unavailability (missing key, outage) now degrades /ask and briefs to
  T1_free automatically; only total failure produces the mechanical brief.
