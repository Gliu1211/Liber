"""Runtime settings, read from `LIBER_*` environment variables (ARCHITECTURE.md §1, §8, §11).

Why a settings object: every tunable lives in one typed place instead of being scattered as
constants, and tests can build a `Settings(...)` pointing at a temporary directory and a fake
upstream. Later sessions add the context-window threshold and the pricing table here (§6).
"""

from pathlib import Path

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# Also used by the Tier 1 harness, which must not depend on the developer's LIBER_* variables.
DEFAULT_WAL_CHECKPOINT_SECONDS = 1.0


class ModelPrice(BaseModel):
    """One model's input prices in US dollars per million tokens (plan/cost.py)."""

    input: float
    cache_write: float  # 5-minute prompt cache
    cache_read: float


# §6: "pricing ratios come from a user-editable config table, not hard-coded claims". Input
# prices are Anthropic's published ones as of 2026-11-04, as are the cache reads for Fable 5.x,
# Opus 5.5 and Sonnet 5.5. The rest are derived from the usual ratios (cache write = 1.25 x input
# for the 5-minute cache, cache read = 0.1 x input). Check the current price list; LIBER_PRICING
# (JSON) replaces the whole table. Keys are model id prefixes; the longest matching one is used.
DEFAULT_PRICING = {
    "claude-fable-5": ModelPrice(input=10.0, cache_write=12.5, cache_read=0.25),
    "claude-opus-5-5": ModelPrice(input=4.0, cache_write=5.0, cache_read=0.20),
    "claude-opus-5": ModelPrice(input=5.0, cache_write=6.25, cache_read=0.50),
    "claude-opus-4": ModelPrice(input=5.0, cache_write=6.25, cache_read=0.50),
    "claude-sonnet-5": ModelPrice(input=2.0, cache_write=2.5, cache_read=0.20),
    "claude-sonnet-4-6": ModelPrice(input=3.0, cache_write=3.75, cache_read=0.30),
    "claude-haiku-5-5": ModelPrice(input=0.10, cache_write=0.125, cache_read=0.01),
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

    log_level: str = "INFO"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "liber.db"

    @property
    def blobs_dir(self) -> Path:
        """Offloaded content, by hash (cache/blobs.py)."""
        return self.data_dir / "blobs"
