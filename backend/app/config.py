"""Application configuration, loaded from environment / .env.

Nothing here is required to *parse* the GIS report (that source is public).
The ERCOT credentials are wired in now so authenticated ERCOT datasets can be
added later without touching the rest of the app. Supabase is required only
when you actually want to persist discovered projects.
"""
from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # --- Supabase (point at any project in any account) ---
    supabase_url: str | None = None
    # Prefer the service-role key for server-side upserts (bypasses RLS).
    supabase_service_key: str | None = None

    # --- ERCOT Public API (only needed for authenticated datasets, not GIS) ---
    ercot_username: str | None = None
    ercot_password: str | None = None
    ercot_subscription_key: str | None = None

    # ERCOT B2C ROPC OAuth constants (public, documented values).
    ercot_token_url: str = (
        "https://ercotb2c.b2clogin.com/ercotb2c.onmicrosoft.com/"
        "B2C_1_PUBAPI-ROPC-FLOW/oauth2/v2.0/token"
    )
    ercot_client_id: str = "fec253ea-0d06-4272-a5e6-b478baeecd70"
    ercot_api_base: str = "https://api.ercot.com/api/public-reports"

    @property
    def supabase_configured(self) -> bool:
        return bool(self.supabase_url and self.supabase_service_key)


@lru_cache
def get_settings() -> Settings:
    return Settings()
