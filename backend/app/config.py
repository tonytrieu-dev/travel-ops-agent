"""Application configuration, loaded once from the environment (fail-fast).

Secrets are wrapped in ``SecretStr`` so they never appear in logs or ``repr`` output.
Every tunable the agent and adapters rely on lives here as a named constant — no magic
numbers scattered through the codebase.
"""

from functools import lru_cache
from pathlib import Path

from pydantic import SecretStr, computed_field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Cerebras runs gpt-oss-120b directly and keeps clean JSON tool-calls for Pydantic AI.
CEREBRAS_MODEL = "gpt-oss-120b"
GEMINI_JUDGE_MODEL = "gemini-3.6-flash"

SEARCHAPI_BASE_URL = "https://www.searchapi.io/api/v1/search"
SEARCHAPI_TIMEOUT_SECONDS = 60.0

SLACK_API_TIMEOUT_SECONDS = 10.0

# RecordedProvider replays real-captured payloads from here; never hand-fabricated.
FLIGHT_CASSETTE_DIR = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "recorded" / "flights"
ACTIVITY_CASSETTE_PATH = (
    Path(__file__).resolve().parent.parent
    / "tests"
    / "fixtures"
    / "recorded"
    / "activities"
    / "san_diego.json"
)

# Agent guardrails: MAX_TOOL_STEPS/MAX_REQUESTS_PER_RUN bound the ReAct loop's iteration count.
# MAX_CONTEXT_TOKENS matches gpt-oss-120b's real 30_000 tokens/minute limit on Cerebras — don't
# raise it, that just trades a clean UsageLimitExceeded (handled gracefully in dbos_runtime.py)
# for a raw 429 mid-run. If runs legitimately hit it, trim tool-result verbosity instead.
MAX_TOOL_STEPS = 8
MAX_CONTEXT_TOKENS = 30_000
MAX_REQUESTS_PER_RUN = MAX_TOOL_STEPS + 3

# pydantic-ai's default is 1 retry, too little room for the model to self-correct.
MAX_OUTPUT_RETRIES = 3

# 400 chars x 5 results/call still overfilled the provider budget after 4 web_search calls in
# one run (redundant queries the prompt now forbids, e.g. re-searching flight info by name).
MAX_TOOL_RESULT_CHARS = 300

# The char budget above assumes 5 results/call, but nothing enforced that ceiling — the model can
# (and did: 9, 10) ask web_search for more, still blowing past the 8K tokens/min throttle.
MAX_WEB_SEARCH_RESULTS = 3

# Forums/social platforms are user opinion, not vetted travel information. Excluded at the
# Tavily API level (deterministic) rather than trusted to the model to self-filter.
EXCLUDED_ACTIVITY_SEARCH_DOMAINS = [
    "reddit.com",
    "quora.com",
    "pinterest.com",
    "facebook.com",
    "twitter.com",
    "x.com",
    "tiktok.com",
    "instagram.com",
]

# A flight offer is only bookable for a short window because airfares are volatile. After this,
# the booking is marked EXPIRED and the user must re-search rather than book a stale price.
BOOKING_TTL_MINUTES = 30

# Reuse a real flight search for the same route+dates for this long instead of spending another
# unit of the scarce one-time 100-search SearchApi quota. Deliberately short relative to that
# quota concern: flight prices reprice multiple times a day, and a cached result must not
# outlive BOOKING_TTL_MINUTES (30) by much, or a "fresh" search could already hand out a token
# that's dead by the time a human confirms it. A renewable-quota production deployment should
# revalidate even more often than this.
FLIGHT_CACHE_TTL_MINUTES = 15

# Cerebras list prices (USD/M tokens) for the panel's estimated cost only.
LLM_INPUT_PRICE_PER_MILLION_TOKENS = 0.59
LLM_OUTPUT_PRICE_PER_MILLION_TOKENS = 0.79

# No auth yet: every request acts as this one fixed user. get_current_user() is the single seam
# that will start resolving a real identity later — route handlers never read this directly.
DEMO_USER_EMAIL = "tonytrieu.dev@gmail.com"

# Cap concurrent agent runs so a burst can't blow through the LLM provider's rate limits.
MAX_CONCURRENT_AGENT_RUNS = 2

# Per-IP request cap on the expensive routes (/plan, /flights/search) — both spend real,
# scarce third-party quota (LLM requests, the one-time SearchApi search allotment).
RATE_LIMIT_MAX_REQUESTS = 10
RATE_LIMIT_WINDOW_SECONDS = 60


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(".env", "../.env"), env_file_encoding="utf-8", extra="ignore"
    )

    cerebras_api_key: SecretStr
    gemini_api_key: SecretStr | None = None
    searchapi_api_key: SecretStr
    tavily_api_key: SecretStr
    database_url: str

    use_live_flight_api: bool = True
    frontend_origin: str = "http://localhost:5173"
    zero_trust_enforced: bool = False
    zero_trust_signing_secret: SecretStr = SecretStr("local-development-only")

    slack_bot_token: SecretStr | None = None
    slack_signing_secret: SecretStr | None = None
    slack_approvals_channel_id: str | None = None

    @computed_field
    @property
    def dbos_database_url(self) -> str:
        """DBOS speaks the plain (psycopg/sync) Postgres URL, not the asyncpg dialect."""
        return self.database_url.replace("postgresql+asyncpg://", "postgresql://")

    @model_validator(mode="after")
    def _require_secure_zero_trust_secret(self) -> "Settings":
        if self.zero_trust_enforced and self.zero_trust_signing_secret.get_secret_value() == (
            "local-development-only"
        ):
            raise ValueError(
                "ZERO_TRUST_SIGNING_SECRET must be set to a non-default value when "
                "ZERO_TRUST_ENFORCED is enabled"
            )
        return self


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide settings singleton.

    Fails loudly at startup if a required key is missing, rather than at the first request.
    """
    return Settings()  # pyright: ignore[reportCallIssue]  # values come from the environment
