"""ERCOT Public API OAuth client (B2C ROPC flow).

NOTE: the GIS Report source does not use this — it comes from the public MIS.
This client exists so authenticated ERCOT datasets (real-time pricing, outage
reports, etc.) can be added later. It caches the token until shortly before it
expires (tokens last ~1 hour).
"""
from __future__ import annotations

import time

import httpx

from ..config import Settings


class ErcotAuth:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._token: str | None = None
        self._expires_at: float = 0.0

    def _fetch_token(self) -> str:
        s = self.settings
        if not (s.ercot_username and s.ercot_password):
            raise RuntimeError(
                "ERCOT_USERNAME / ERCOT_PASSWORD not configured; "
                "the authenticated ERCOT API is unavailable."
            )
        data = {
            "username": s.ercot_username,
            "password": s.ercot_password,
            "grant_type": "password",
            "scope": f"openid {s.ercot_client_id} offline_access",
            "client_id": s.ercot_client_id,
            "response_type": "id_token",
        }
        resp = httpx.post(s.ercot_token_url, data=data, timeout=30)
        resp.raise_for_status()
        payload = resp.json()
        token = payload.get("access_token") or payload.get("id_token")
        if not token:
            raise RuntimeError(f"No token in ERCOT auth response: {list(payload)}")
        expires_in = int(payload.get("expires_in", 3600))
        # Refresh a minute early to avoid edge-of-expiry failures.
        self._expires_at = time.time() + max(expires_in - 60, 60)
        self._token = token
        return token

    def token(self) -> str:
        if self._token and time.time() < self._expires_at:
            return self._token
        return self._fetch_token()

    def headers(self) -> dict[str, str]:
        if not self.settings.ercot_subscription_key:
            raise RuntimeError("ERCOT_SUBSCRIPTION_KEY not configured.")
        return {
            "Authorization": f"Bearer {self.token()}",
            "Ocp-Apim-Subscription-Key": self.settings.ercot_subscription_key,
        }
