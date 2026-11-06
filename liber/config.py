"""Runtime settings, read from `LIBER_*` environment variables (ARCHITECTURE.md §1, §8, §11).

Why a settings object: every tunable lives in one typed place instead of being scattered as
constants, and tests can build a `Settings(...)` pointing at a temporary directory and a fake
upstream. Later sessions add the context-window threshold and the pricing table here (§6).
"""

from pathlib import Path

from pydantic import BaseModel, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

# Also used by the Tier 1 harness, which must not depend on the developer's LIBER_* variables.
DEFAULT_WAL_CHECKPOINT_SECONDS = 1.0
DEFAULT_SUMMARY_MODEL = "claude-haiku-5-5"


class Rates(BaseModel):
    """Input prices in US dollars per million tokens."""

    input: float
    cache_write: float  # 5-minute prompt cache
    cache_read: float


class LongPromptRates(Rates):
    above_tokens: int  # these rates apply to requests with more input tokens than this


class ModelPrice(Rates):
    """One model's prices (plan/cost.py). Some models charge more per token for long prompts."""

    long_prompt: LongPromptRates | None = None

    def rates_for(self, input_tokens: int) -> Rates:
        """The rates a request with `input_tokens` input tokens is charged at."""
        if self.long_prompt is not None and input_tokens > self.long_prompt.above_tokens:
            return self.long_prompt
        return self


# §6: "pricing ratios come from a user-editable config table, not hard-coded claims". Input
# prices are Anthropic's published ones as of 2026-11-04, as are the cache reads for Fable 5.x
# and Opus 5.5; Sonnet 5.5 and Haiku 5.5 (both tiers) are as given by the owner on 2026-11-05.
# The rest are derived from the usual ratios (cache write = 1.25 x input for the 5-minute cache,
# cache read = 0.1 x input). Check the current price list; LIBER_PRICING (JSON) replaces the
# whole table. Keys are model id prefixes; the longest matching one is used.
DEFAULT_PRICING = {
    "claude-fable-5": ModelPrice(input=10.0, cache_write=12.5, cache_read=0.25),
    "claude-opus-5-5": ModelPrice(input=4.0, cache_write=5.0, cache_read=0.20),
    "claude-opus-5": ModelPrice(input=5.0, cache_write=6.25, cache_read=0.50),
    "claude-opus-4": ModelPrice(input=5.0, cache_write=6.25, cache_read=0.50),
    "claude-sonnet-5-5": ModelPrice(input=2.0, cache_write=2.5, cache_read=0.10),
    "claude-sonnet-5": ModelPrice(input=2.0, cache_write=2.5, cache_read=0.20),
    "claude-sonnet-4-6": ModelPrice(input=3.0, cache_write=3.75, cache_read=0.30),
    "claude-haiku-5-5": ModelPrice(
        input=0.10,
        cache_write=0.125,
        cache_read=0.01,
        long_prompt=LongPromptRates(above_tokens=100_000, input=0.50, cache_write=0.625, cache_read=0.05),
    ),
}


def _default_data_dir() -> Path:
    return Path.home() / ".liber"


class Settings(BaseSettings):
    """All of Liber's configuration. Example: `LIBER_PORT=9000 liber serve`."""

    model_config = SettingsConfigDict(env_prefix="LIBER_")

    # Hard rule 3: listen on loopback only, so nothing else on the network can use the proxy.
    host: str = "127.0.0.1"
    port: int = 8484

    # Where requests are forwarded. Point this at another Anthropic-compatible gateway if needed.
    upstream_base_url: str = "https://api.anthropic.com"

    # SQLite database and (later) content blobs live here (§8).
    data_dir: Path = Field(default_factory=_default_data_dir)

    # §11: request/response bodies are recorded by default (needed for undo and evals) but can
    # be switched off. Usage numbers and latency are always recorded; they contain no content.
    record_bodies: bool = True

    # Long replies can stream for many minutes, so the read timeout (the longest silence we
    # tolerate between two chunks) is generous. Connecting should be quick.
    upstream_connect_timeout: float = 10.0
    upstream_read_timeout: float = 600.0

    # How often a background task copies SQLite's write-ahead log into the database file.
    # Requests never do it themselves (docs/decisions/0008). SQLite's own default waits for
    # 1000 pages (~4 MB); a second of Liber's writes is usually far less than that.
    # Must be > 0: zero would make the background task checkpoint in a tight loop.
    wal_checkpoint_seconds: float = Field(default=DEFAULT_WAL_CHECKPOINT_SECONDS, gt=0)

    # §6 break points (plan/policy.py). Idle: a pending plan version is applied on the next new
    # turn once the conversation was idle longer than the provider's prompt-cache lifetime.
    idle_apply_seconds: float = Field(default=300.0, gt=0)
    # Threshold: requests filling more than this share of the model's context window are flagged;
    # with threshold_auto_apply, they also apply the pending version (v1: off).
    context_threshold: float = Field(default=0.7, gt=0, le=1)
    threshold_auto_apply: bool = False

    # Prices for plan cost estimates (plan/cost.py); see DEFAULT_PRICING.
    pricing: dict[str, ModelPrice] = Field(default_factory=lambda: dict(DEFAULT_PRICING))

    # §9 background worker: exact token counts, summaries and topics (liber/worker/). It runs only
    # with its own API key (LIBER_WORKER_API_KEY): client credentials are passed through and never
    # kept (hard rule 2). Without it, plans can't summarize (the CLI says why) and token counts
    # stay estimates. SecretStr: the key never shows in a repr or a log line.
    worker_api_key: SecretStr | None = None
    # The cheap model that writes summaries and topic labels (D10), on the same upstream.
    summary_model: str = DEFAULT_SUMMARY_MODEL
    # ADR 0004 A5 experiment: also read the summarized turns' thinking text and add a labelled
    # "earlier decisions and why" section to the frozen block. Off unless Tier 2 shows it helps.
    carry_forward_reasoning: bool = False

    log_level: str = "INFO"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "liber.db"

    @property
    def blobs_dir(self) -> Path:
        """Offloaded content, by hash (cache/blobs.py)."""
        return self.data_dir / "blobs"
