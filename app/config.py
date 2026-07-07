"""Env-driven configuration. Everything deploy-specific lives in .env so a
move to Fly/Railway later is config-only (no code changes, no hardcoded paths)."""

from __future__ import annotations

import os
from pathlib import Path


def load_dotenv(path: str | Path = ".env") -> None:
    """Minimal .env loader (KEY=VALUE lines, # comments). Existing environment
    variables win, so real env always overrides the file."""
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip("'\"")
        os.environ.setdefault(key, value)


class Settings:
    def __init__(self, env_file: str | Path = ".env"):
        load_dotenv(env_file)
        env = os.environ
        self.host = env.get("HOST", "0.0.0.0")
        self.port = int(env.get("PORT", "8377"))
        self.data_dir = Path(env.get("DATA_DIR", "./data")).expanduser().resolve()
        self.db_path = Path(
            env.get("DB_PATH", str(self.data_dir / "tippool.sqlite3"))
        ).expanduser().resolve()
        self.timezone = env.get("TIMEZONE", "America/Los_Angeles")
        self.venue_name = env.get("VENUE_NAME", "Tavern Law")
        self.session_days = int(env.get("SESSION_DAYS", "30"))
        # first-boot bootstrap admin (only used when the user table is empty)
        self.admin_email = env.get("ADMIN_EMAIL", "")
        self.admin_password = env.get("ADMIN_PASSWORD", "")
        # Square (M3). Token stays server-side; never sent to the client.
        self.square_access_token = env.get("SQUARE_ACCESS_TOKEN", "")
        # One venue may span multiple Square locations (comma-separated).
        # All locations feed the same single daily tip pool.
        self.square_location_ids = [
            s.strip() for s in env.get("SQUARE_LOCATION_ID", "").split(",") if s.strip()
        ]
        self.square_env = env.get("SQUARE_ENV", "sandbox")
        self.nightly_sync = env.get("NIGHTLY_SYNC", "1") not in ("0", "false", "off")
        self.nightly_sync_hour = int(env.get("NIGHTLY_SYNC_HOUR", "5"))

    @property
    def square_configured(self) -> bool:
        return bool(self.square_access_token and self.square_location_ids)

    def square_for(self, slug: str) -> dict:
        """Per-venue Square credentials (M5). Env vars are suffixed with the
        venue slug (SQUARE_ACCESS_TOKEN__LA_FONTANA=...); the bare, unsuffixed
        names remain Tavern Law's, so existing deployments keep working.
        Tokens are never mixed across venues."""
        sfx = "__" + slug.upper().replace("-", "_")
        env = os.environ
        token = env.get(f"SQUARE_ACCESS_TOKEN{sfx}", "")
        locations = [
            s.strip() for s in env.get(f"SQUARE_LOCATION_ID{sfx}", "").split(",")
            if s.strip()
        ]
        sq_env = env.get(f"SQUARE_ENV{sfx}", "")
        if slug == "tavern-law":
            token = token or self.square_access_token
            locations = locations or self.square_location_ids
            sq_env = sq_env or self.square_env
        return {
            "token": token,
            "location_ids": locations,
            "env": sq_env or "sandbox",
            "configured": bool(token and locations),
        }

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
