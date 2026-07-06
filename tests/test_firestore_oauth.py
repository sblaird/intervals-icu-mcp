"""Tests for the Firestore-backed OAuth provider.

Uses a fake AsyncDocumentReference (no google-cloud-firestore dependency at
test time) injected via the `document=` constructor kwarg.
"""

from __future__ import annotations

import logging
import time
from typing import Any
from unittest.mock import MagicMock

import pytest
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


def _make_provider(doc: Any) -> FirestoreOAuthProvider:
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


class _OutageDoc:
    """Fake doc whose get/set fail while ``down`` is True (R7 tests)."""

    def __init__(self, data: dict[str, Any] | None = None) -> None:
        self.data = data
        self.down = False
        self.get_calls = 0
        self.set_calls: list[dict[str, Any]] = []

    async def get(self) -> _FakeSnapshot:
        self.get_calls += 1
        if self.down:
            raise RuntimeError("firestore outage")
        return _FakeSnapshot(self.data)

    async def set(self, data: dict[str, Any]) -> None:
        if self.down:
            raise RuntimeError("firestore outage")
        self.set_calls.append(data)
        self.data = data


class TestLoadRetry:
    """R7 (STB-M1): a failed load must not latch _loaded=True forever."""

    async def test_failed_load_retries_and_recovers(self):
        seeder_doc = _FakeDoc()
        await _make_provider(seeder_doc).register_client(_client())

        doc = _OutageDoc(seeder_doc.data)
        doc.down = True
        provider = _make_provider(doc)
        provider.RELOAD_BACKOFF_SECONDS = 0.0

        # First request: load fails, in-memory state is empty, but the
        # provider is NOT latched as loaded.
        assert await provider.get_client("test-client") is None
        assert provider._loaded is False

        # Firestore recovers: the next request retries and finds the client.
        doc.down = False
        loaded = await provider.get_client("test-client")
        assert loaded is not None
        assert provider._loaded is True

    async def test_backoff_window_skips_immediate_reload(self):
        doc = _OutageDoc()
        doc.down = True
        provider = _make_provider(doc)  # default 5s backoff

        await provider.get_client("x")
        assert doc.get_calls == 1
        # Within the backoff window no new Firestore load is attempted.
        await provider.get_client("x")
        assert doc.get_calls == 1

    async def test_state_accumulated_during_outage_survives_late_load(self, caplog):
        # Seed the store with client A.
        seeder_doc = _FakeDoc()
        await _make_provider(seeder_doc).register_client(_client("client-a"))

        doc = _OutageDoc(seeder_doc.data)
        doc.down = True
        provider = _make_provider(doc)
        provider.RELOAD_BACKOFF_SECONDS = 0.0

        # Register client B while Firestore is down (load and persist fail;
        # non-critical persist failure must not raise).
        with caplog.at_level(logging.ERROR):
            await provider.register_client(_client("client-b"))
        assert provider.persist_failures == 1

        # Firestore recovers: the late load restores A without erasing B.
        doc.down = False
        assert await provider.get_client("client-a") is not None
        assert await provider.get_client("client-b") is not None


class TestPersistFailureSurfacing:
    """R7 (STB-M2): persist failures are loud, and fatal on token issuance."""

    async def test_register_persist_failure_logged_not_raised(self, caplog):
        doc = _OutageDoc()
        provider = _make_provider(doc)
        await provider._ensure_loaded()
        doc.down = True

        with caplog.at_level(logging.ERROR):
            await provider.register_client(_client())

        assert provider.persist_failures == 1
        assert any(
            "Failed to persist OAuth state" in record.getMessage()
            and record.levelno >= logging.ERROR
            for record in caplog.records
        )

    async def test_token_exchange_persist_failure_raises(self):
        doc = _OutageDoc()
        provider = _make_provider(doc)
        client = _client()
        await provider.register_client(client)

        params = AuthorizationParams(
            state="s",
            scopes=["mcp"],
            code_challenge="x" * 43,
            redirect_uri=AnyUrl("https://callback.test/cb"),
            redirect_uri_provided_explicitly=True,
        )
        await provider.authorize(client, params)
        code_str = next(iter(provider.auth_codes))
        loaded_code = await provider.load_authorization_code(client, code_str)
        assert loaded_code is not None

        doc.down = True
        with pytest.raises(RuntimeError, match="firestore outage"):
            await provider.exchange_authorization_code(client, loaded_code)

    async def test_refresh_exchange_persist_failure_raises(self):
        doc = _OutageDoc()
        provider = _make_provider(doc)
        client = _client()
        await provider.register_client(client)

        params = AuthorizationParams(
            state="s",
            scopes=["mcp"],
            code_challenge="x" * 43,
            redirect_uri=AnyUrl("https://callback.test/cb"),
            redirect_uri_provided_explicitly=True,
        )
        await provider.authorize(client, params)
        code_str = next(iter(provider.auth_codes))
        loaded_code = await provider.load_authorization_code(client, code_str)
        assert loaded_code is not None
        token = await provider.exchange_authorization_code(client, loaded_code)
        assert token.refresh_token is not None
        refresh = await provider.load_refresh_token(client, token.refresh_token)
        assert refresh is not None

        doc.down = True
        with pytest.raises(RuntimeError, match="firestore outage"):
            await provider.exchange_refresh_token(client, refresh, ["mcp"])
