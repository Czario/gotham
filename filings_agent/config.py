"""Filings-agent configuration.

Adapted from the earning_agent ``config.py``.  Every value is sourced from the
environment (``.env`` at the repo root) with safe defaults, so the agent can be
imported without any agent-specific variables being set.

Provider variables (``LLM_PROVIDER`` and the per-provider blocks) exist for the
ported ``filings_agent.llm`` factory.  The provider SDKs themselves are imported
lazily by that module, so importing this config never requires them.
"""
from __future__ import annotations

import os

from dotenv import load_dotenv

load_dotenv()

# ── MongoDB ────────────────────────────────────────────────────────────────
# The agent writes statement bundles into the same normalize_data database the
# scraper pipeline and the admin backend use.
MONGODB_URI: str = os.getenv("MONGODB_URI", "mongodb://localhost:27017")
DATABASE_NAME: str = os.getenv("DATABASE_NAME", "normalize_data")

# ── LLM provider selector ──────────────────────────────────────────────────
# "deepseek" (default), "groq", "gemini", or "ollama".
LLM_PROVIDER: str = os.getenv("LLM_PROVIDER", "deepseek").strip().lower()

# ── Ollama ─────────────────────────────────────────────────────────────────
OLLAMA_BASE_URL: str = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
OLLAMA_MODEL: str = os.getenv("OLLAMA_MODEL", "llama3.1:8b")
OLLAMA_NUM_CTX: int = int(os.getenv("OLLAMA_NUM_CTX", "4096"))

# ── DeepSeek ───────────────────────────────────────────────────────────────
DEEPSEEK_API_KEY: str = os.getenv("DEEPSEEK_API_KEY", "")
DEEPSEEK_MODEL: str = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")
DEEPSEEK_BASE_URL: str = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1")
DEEPSEEK_REQUEST_TIMEOUT: float = float(os.getenv("DEEPSEEK_REQUEST_TIMEOUT", "120"))

# ── Groq (used only when LLM_PROVIDER=groq) ────────────────────────────────
GROQ_API_KEY: str = os.getenv("GROQ_API_KEY", "")
GROQ_MODEL: str = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")
GROQ_BASE_URL: str = os.getenv("GROQ_BASE_URL", "https://api.groq.com/openai/v1")
GROQ_REQUEST_TIMEOUT: float = float(os.getenv("GROQ_REQUEST_TIMEOUT", "60"))
GROQ_RPM: int = int(os.getenv("GROQ_RPM", "30"))
GROQ_TPM: int = int(os.getenv("GROQ_TPM", "12000"))

# ── Google Gemini (used only when LLM_PROVIDER=gemini) ─────────────────────
GEMINI_API_KEY: str = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL: str = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
GEMINI_REQUEST_TIMEOUT: float = float(os.getenv("GEMINI_REQUEST_TIMEOUT", "120"))

# ── Agent chat-model retries ───────────────────────────────────────────────
# Retries for the tool-calling chat model used by the agent nodes (hierarchy
# review, guidance).  Default 1 — the hierarchy agent is authoritative, so a
# transient provider timeout must not cost the filing: under
# HIERARCHY_AGENT_FAILURE_POLICY=block a single unretried timeout blocks the
# whole filing.  LangChain's own default of 2 is not used because it lets a hung
# call ride out `timeout x 3` (DeepSeek: 120s x 3 = 6 min) inside one agent step.
# Set 0 to bound a stall to exactly one timeout, at the cost of failing the
# filing on the first provider error.
LLM_CHAT_MAX_RETRIES: int = int(os.getenv("LLM_CHAT_MAX_RETRIES", "1"))

# ── Dev LLM response cache (opt-in; never enable in production) ─────────────
LLM_CACHE_ENABLED: bool = os.getenv("LLM_CACHE", "0").strip().lower() in {
    "1", "true", "yes", "on"
}
LLM_CACHE_DIR: str = os.getenv("LLM_CACHE_DIR", ".llm_cache")

# ── Save gate ──────────────────────────────────────────────────────────────
# When True, the persist node refuses to write a filing that still has
# unresolved high-severity validation findings.  Default is advisory (False):
# findings are recorded, the validation ceiling still excludes provably-wrong
# concepts, but a filing is NEVER blocked from being written.
STRICT_ACCURACY: bool = os.getenv("STRICT_ACCURACY", "0").strip().lower() not in {
    "0", "false", "no", "off", ""
}

# ── Validation / review behaviour ──────────────────────────────────────────
# AGENT_MODE controls how much the agent is allowed to do:
#   "report" — validate and record findings, never modify values (default)
#   "repair" — apply provenance-tagged corrections / gap fills
#   "strict" — as repair, but corrections require explicit approval
AGENT_MODE: str = os.getenv("AGENT_MODE", "report").strip().lower()

# Enables the LLM JUDGE for the final write decision (P8).  Off by default:
# the deterministic decision policy (an explicit WriteDecision) is used instead.
# When on, the judge is consulted only for the ambiguous middle — blocking
# findings exist AND something is still writable — and may narrow the write or
# opt into a partial write.  It can never widen the validation ceiling.
AGENT_DECISION_ENABLED: bool = os.getenv("AGENT_DECISION_ENABLED", "0").strip().lower() in {
    "1", "true", "yes", "on"
}

# Enables the LLM review node (judgment calls).  Off = deterministic nodes only.
AGENT_REVIEW_ENABLED: bool = os.getenv("AGENT_REVIEW_ENABLED", "1").strip().lower() not in {
    "0", "false", "no", "off", ""
}

# ── Forward-looking MD&A guidance ──────────────────────────────────────────
GUIDANCE_ENABLED: bool = os.getenv("GUIDANCE_ENABLED", "1").strip().lower() not in {
    "0", "false", "no", "off", ""
}
# Enables the guidance LLM pass.  When off (or when no MD&A text is available)
# the deterministic pipeline still runs; guidance is simply skipped.
GUIDANCE_LLM_ENABLED: bool = os.getenv("GUIDANCE_LLM_ENABLED", "1").strip().lower() not in {
    "0", "false", "no", "off", ""
}
GUIDANCE_MAX_RECORDS: int = int(os.getenv("GUIDANCE_MAX_RECORDS", "15"))
# Cap on the MD&A text handed to the guidance agent.
GUIDANCE_MAX_CHARS: int = int(os.getenv("GUIDANCE_MAX_CHARS", "200000"))


# ── Advisory long-term memory (optional) ───────────────────────────────────
# Unlike the earning_agent, this is OPTIONAL and defaults to off so importing
# the package never requires the variable to be set.
MEMORY_ENABLED: bool = os.getenv("MEMORY_ENABLED", "0").strip().lower() in {
    "1", "true", "yes", "on"
}

# Window (in stored periods) used when prioritising concepts for review.
PROMPT_HISTORY_PERIODS: int = int(os.getenv("PROMPT_HISTORY_PERIODS", "3"))

# ── Audit trail (P7) ───────────────────────────────────────────────────────
# JSONL trail of every node transition, LLM step and tool call.
AGENT_AUDIT_ENABLED: bool = os.getenv("AGENT_AUDIT_ENABLED", "1").strip().lower() not in {
    "0", "false", "no", "off", ""
}
AGENT_AUDIT_DIR: str = os.getenv("AGENT_AUDIT_DIR", "Logs/agent_audit")

# ── Durable run sessions / batch resume (P7) ───────────────────────────────
AGENT_SESSION_ENABLED: bool = os.getenv("AGENT_SESSION_ENABLED", "1").strip().lower() not in {
    "0", "false", "no", "off", ""
}
# When true (default) a batch run skips companies a previous run of the same
# session already completed.
AGENT_BATCH_RESUME: bool = os.getenv("AGENT_BATCH_RESUME", "1").strip().lower() not in {
    "0", "false", "no", "off", ""
}
