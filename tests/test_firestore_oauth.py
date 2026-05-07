"""Tests for the Firestore-backed OAuth provider.

Uses a fake AsyncDocumentReference (no google-cloud-firestore dependency at
test time) injected via the `document=` constructor kwarg.
"""

from __future__ import annotations

import time
from typing import Any
from unittest.mock import MagicMock

from mcp.server.auth.provider import AccessToken, AuthorizationParams
from mcp.shared.auth import OAuthClientInformationFull
from pydantic import AnyUrl

from intervals_icu_mcp.firestore_oauth import FirestoreOAuthProvider


class _FakeSnapshot:
    def __init__(self, data: dict[str, Any] | None) -> None:
        self._data = data
        self.exists = data is not None

    def to_dict(self) -> dict[str, Any] | None:
        return self._data


class _FakeDoc:
    """In-memory async document — captures every set() and replays via get()."""

    def __init__(self, initial: dict[str, Any] | None = None) -> None:
        self.data: dict[str, Any] | None = initial
        self.set_calls: list[dict[str, Any]] = []

    async def get(self) -> _FakeSnapshot:
        return _FakeSnapshot(self.data)

    async def set(self, data: dict[str, Any]) -> None:
        self.set_calls.append(data)
        self.data = data


def _make_provider(doc: _FakeDoc) -> FirestoreOAuthProvider:
    return FirestoreOAuthProvider(
        base_url="https://example.test",
        document=doc,
    )


def _client(client_id: str = "test-client") -> OAuthClientInformationFull:
    return OAuthClientInformationFull(
        client_id=client_id,
        client_secret="secret",
        redirect_uris=[AnyUrl("https://callback.test/cb")],
        scope="mcp",
    )


class TestPersistOnRegister:
    async def test_register_client_writes_to_firestore(self):
        doc = _FakeDoc()
        provider = _make_provider(doc)
        await provider.register_client(_client())
        assert len(doc.set_calls) == 1
        persisted = doc.set_calls[-1]
        assert "test-client" in persisted["clients"]


class TestRoundTripState:
    async def test_state_loaded_from_firestore_on_first_use(self):
        # First provider: register a client, capture the persisted state.
        doc = _FakeDoc()
        provider1 = _make_provider(doc)
        await provider1.register_client(_client())

        # Second provider sees the same doc — should restore the client.
        provider2 = _make_provider(doc)
        loaded = await provider2.get_client("test-client")
        assert loaded is not None
        assert loaded.client_id == "test-client"

    async def test_full_oauth_flow_round_trip(self):
        """Auth code → access+refresh tokens persisted; new provider sees tokens."""
        doc = _FakeDoc()
        provider1 = _make_provider(doc)
        client = _client()
        await provider1.register_client(client)

        params = AuthorizationParams(
            state="s",
            scopes=["mcp"],
            code_challenge="x" * 43,
            redirect_uri=AnyUrl("https://callback.test/cb"),
            redirect_uri_provided_explicitly=True,
        )
        await provider1.authorize(client, params)
        assert len(provider1.auth_codes) == 1
        code_str = next(iter(provider1.auth_codes))

        loaded_code = await provider1.load_authorization_code(client, code_str)
        assert loaded_code is not None
        oauth_token = await provider1.exchange_authorization_code(client, loaded_code)
        assert oauth_token.access_token
        assert oauth_token.refresh_token

        # Cold start a new provider against the same doc — token should validate.
        provider2 = _make_provider(doc)
        verified = await provider2.verify_token(oauth_token.access_token)
        assert verified is not None
        assert verified.token == oauth_token.access_token


class TestExpiryCleanupPersists:
    async def test_expired_access_token_cleanup_persists(self):
        doc = _FakeDoc()
        provider = _make_provider(doc)
        await provider.register_client(_client())
        # Inject an already-expired access token directly.
        provider.access_tokens["expired-token"] = AccessToken(
            token="expired-token",
            client_id="test-client",
            scopes=["mcp"],
            expires_at=int(time.time()) - 60,
        )
        # Force loaded so we don't blow it away on first read.
        provider._loaded = True

        prior_set_count = len(doc.set_calls)
        result = await provider.load_access_token("expired-token")
        assert result is None
        # Cleanup should have triggered a persist.
        assert len(doc.set_calls) == prior_set_count + 1
        assert "expired-token" not in doc.set_calls[-1]["access_tokens"]

    async def test_unchanged_load_does_not_persist(self):
        doc = _FakeDoc()
        provider = _make_provider(doc)
        await provider.register_client(_client())
        # Inject a still-valid token.
        provider.access_tokens["good-token"] = AccessToken(
            token="good-token",
            client_id="test-client",
            scopes=["mcp"],
            expires_at=int(time.time()) + 3600,
        )
        provider._loaded = True

        prior_set_count = len(doc.set_calls)
        result = await provider.load_access_token("good-token")
        assert result is not None
        # No state change → no extra Firestore write.
        assert len(doc.set_calls) == prior_set_count


class TestLoadFailureIsTolerated:
    async def test_load_exception_falls_back_to_empty_state(self):
        doc = MagicMock()

        async def boom():
            raise RuntimeError("firestore exploded")

        doc.get = boom
        provider = FirestoreOAuthProvider(base_url="https://example.test", document=doc)
        # Should not raise — empty state is the fallback.
        result = await provider.get_client("does-not-exist")
        assert result is None
