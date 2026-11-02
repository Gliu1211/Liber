"""Runtime settings, read from `LIBER_*` environment variables (ARCHITECTURE.md §1, §8, §11).

Why a settings object: every tunable lives in one typed place instead of being scattered as
constants, and tests can build a `Settings(...)` pointing at a temporary directory and a fake
upstream. Later sessions add the context-window threshold and the pricing table here (§6).
"""

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


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
    wal_checkpoint_seconds: float = 1.0

    log_level: str = "INFO"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "liber.db"
