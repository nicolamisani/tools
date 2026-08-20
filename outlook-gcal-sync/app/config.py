"""Runtime configuration, read once from the environment."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    app_password: str
    base_url: str
    google_client_id: str
    google_client_secret: str
    data_dir: Path

    @property
    def redirect_uri(self) -> str:
        return f"{self.base_url.rstrip('/')}/oauth/google/callback"

    @property
    def google_configured(self) -> bool:
        return bool(self.google_client_id and self.google_client_secret)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "sync.db"

    @property
    def key_path(self) -> Path:
        return self.data_dir / "secret.key"

    @property
    def upload_dir(self) -> Path:
        return self.data_dir / "uploads"

    def upload_path(self, source_id: int) -> Path:
        return self.upload_dir / f"source-{source_id}.ics"


def load_settings() -> Settings:
    data_dir = Path(os.environ.get("DATA_DIR", "./data")).expanduser().resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    return Settings(
        app_password=os.environ.get("APP_PASSWORD", ""),
        base_url=os.environ.get("APP_BASE_URL", "http://localhost:8080"),
        google_client_id=os.environ.get("GOOGLE_CLIENT_ID", "").strip(),
        google_client_secret=os.environ.get("GOOGLE_CLIENT_SECRET", "").strip(),
        data_dir=data_dir,
    )


settings = load_settings()
