"""Tests for the static service-token bypass on the Google OAuth gate.

The GravelFit backend (and the Anthropic MCP connector it drives) authenticate
to this server non-interactively with a static bearer token — they cannot run
the browser-based Google Sign-In flow that claude.ai uses. ``MCP_SERVICE_TOKEN``
lets ``load_access_token`` accept that one token directly, while every other
token still flows through the normal OAuth store.

Security properties under test:
- The bypass only fires for the *exact* configured token (constant-time compare).
- A missing/empty ``MCP_SERVICE_TOKEN`` disables the bypass entirely (fail-closed).
- A misconfigured short token (<32 chars) is refused even when presented, so a
  weak secret can never open the server.
- A non-matching token falls through to the real store on both provider variants
  (in-memory and Firestore), i.e. the override never short-circuits the chain.
"""

from __future__ import annotations

from typing import Any

import pytest
from mcp.server.auth.provider import AuthorizationParams  # noqa: F401  (parity with siblings)
from pydantic import AnyUrl  # noqa: F401

from intervals_icu_mcp.google_oauth import (
    GOOGLE_CALLBACK_PATH,
    GoogleGatedFirestoreOAuthProvider,
    GoogleGatedInMemoryOAuthProvider,
    GoogleOAuthConfig,
)

SERVER_URL = "https://mcp.example.com"
ALLOWED_EMAIL = "stephen@example.com"
GOOGLE_CLIENT_ID = "google-client-id.apps.googleusercontent.com"

VALID_TOKEN = "svc_" + "a" * 44  # 48 chars, comfortably >= 32
SHORT_TOKEN = "svc_tooshort"  # < 32 chars


def make_config() -> GoogleOAuthConfig:
    return GoogleOAuthConfig(
        client_id=GOOGLE_CLIENT_ID,
        client_secret="google-secret",
        allowed_emails=frozenset({ALLOWED_EMAIL}),
        redirect_uri=f"{SERVER_URL}{GOOGLE_CALLBACK_PATH}",
    )


def make_inmemory_provider() -> GoogleGatedInMemoryOAuthProvider:
    return GoogleGatedInMemoryOAuthProvider(
        base_url=SERVER_URL,
        required_scopes=["mcp"],
        google_config=make_config(),
    )


class _FakeSnapshot:
    def __init__(self, data: dict[str, Any] | None) -> None:
        self._data = data
        self.exists = data is not None

    def to_dict(self) -> dict[str, Any] | None:
        return self._data


class _FakeDoc:
    """Minimal in-memory async Firestore document (mirrors test_firestore_oauth)."""

    def __init__(self, initial: dict[str, Any] | None = None) -> None:
        self.data: dict[str, Any] | None = initial
        self.set_calls: list[dict[str, Any]] = []

    async def get(self) -> _FakeSnapshot:
        return _FakeSnapshot(self.data)

    async def set(self, data: dict[str, Any]) -> None:
        self.set_calls.append(data)
        self.data = data


def make_firestore_provider() -> GoogleGatedFirestoreOAuthProvider:
    return GoogleGatedFirestoreOAuthProvider(
        base_url=SERVER_URL,
        google_config=make_config(),
        document=_FakeDoc(),
    )


# --- in-memory variant ------------------------------------------------------


class TestInMemoryServiceToken:
    async def test_valid_token_is_accepted(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("MCP_SERVICE_TOKEN", VALID_TOKEN)
        provider = make_inmemory_provider()

        access = await provider.load_access_token(VALID_TOKEN)

        assert access is not None
        assert access.token == VALID_TOKEN
        assert access.client_id == "anthropic-mcp-connector"
        assert access.scopes == ["mcp"]
        assert access.expires_at is None

    async def test_wrong_token_falls_through_to_store(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("MCP_SERVICE_TOKEN", VALID_TOKEN)
        provider = make_inmemory_provider()

        # No such token in the in-memory store -> None (not the service identity).
        assert await provider.load_access_token("some-other-token") is None

    async def test_unset_env_disables_bypass(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv("MCP_SERVICE_TOKEN", raising=False)
        provider = make_inmemory_provider()

        assert await provider.load_access_token(VALID_TOKEN) is None

    async def test_empty_env_disables_bypass(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("MCP_SERVICE_TOKEN", "   ")
        provider = make_inmemory_provider()

        assert await provider.load_access_token(VALID_TOKEN) is None

    async def test_short_configured_token_is_refused(self, monkeypatch: pytest.MonkeyPatch):
        # Even presenting the exact (too-short) secret must not open the server.
        monkeypatch.setenv("MCP_SERVICE_TOKEN", SHORT_TOKEN)
        provider = make_inmemory_provider()

        assert await provider.load_access_token(SHORT_TOKEN) is None


# --- Firestore variant ------------------------------------------------------


class TestFirestoreServiceToken:
    async def test_valid_token_is_accepted(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("MCP_SERVICE_TOKEN", VALID_TOKEN)
        provider = make_firestore_provider()

        access = await provider.load_access_token(VALID_TOKEN)

        assert access is not None
        assert access.client_id == "anthropic-mcp-connector"
        assert access.scopes == ["mcp"]

    async def test_wrong_token_falls_through_to_firestore_store(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setenv("MCP_SERVICE_TOKEN", VALID_TOKEN)
        provider = make_firestore_provider()

        # Falls through the MRO to FirestoreOAuthProvider.load_access_token,
        # which loads state and returns None for an unknown token (no crash).
        assert await provider.load_access_token("some-other-token") is None
