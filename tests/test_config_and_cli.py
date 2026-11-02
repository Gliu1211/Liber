"""Settings defaults and the `liber serve` entry point (hard rule 3: bind to 127.0.0.1)."""

from pathlib import Path
from typing import Any

import pytest

from liber import cli
from liber.config import Settings


def test_defaults_are_local_and_safe() -> None:
    settings = Settings()

    assert settings.host == "127.0.0.1"
    assert settings.upstream_base_url == "https://api.anthropic.com"
    assert settings.data_dir == Path.home() / ".liber"
    assert settings.db_path == Path.home() / ".liber" / "liber.db"
    assert settings.record_bodies is True


def test_settings_come_from_liber_environment_variables(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LIBER_PORT", "9999")
    monkeypatch.setenv("LIBER_UPSTREAM_BASE_URL", "https://example.test")
    monkeypatch.setenv("LIBER_RECORD_BODIES", "false")

    settings = Settings()

    assert settings.port == 9999
    assert settings.upstream_base_url == "https://example.test"
    assert settings.record_bodies is False


def capture_uvicorn_run(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    captured: dict[str, Any] = {}

    def fake_run(app: object, **kwargs: Any) -> None:
        captured["app"] = app
        captured.update(kwargs)

    monkeypatch.setattr(cli.uvicorn, "run", fake_run)
    return captured


def test_serve_binds_to_localhost_by_default(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("LIBER_DATA_DIR", str(tmp_path))
    captured = capture_uvicorn_run(monkeypatch)

    cli.main(["serve"])

    assert captured["host"] == "127.0.0.1"
    assert captured["port"] == Settings().port
    # Let the upstream's own Date/Server headers through instead of adding uvicorn's.
    assert captured["server_header"] is False
    assert captured["date_header"] is False


def test_serve_accepts_host_and_port_flags(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("LIBER_DATA_DIR", str(tmp_path))
    captured = capture_uvicorn_run(monkeypatch)

    cli.main(["serve", "--port", "9001", "--host", "127.0.0.2"])

    assert (captured["host"], captured["port"]) == ("127.0.0.2", 9001)
