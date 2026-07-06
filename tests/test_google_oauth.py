"""Tests for the Google-identity OAuth gate (R1, SEC-1).

The hosted OAuth flow must not issue an authorization code to anyone who has
not completed Google Sign-In as an allowlisted email. These tests cover:

- ``authorize()`` redirects to Google instead of auto-issuing a code.
- ``/auth/google/callback`` completes the original claude.ai request only for
  a verified, allowlisted Google identity (id_token signature, issuer,
  audience, nonce, email_verified, and allowlist all checked).
- Rejection paths: non-allowlisted email, unverified email, missing/invalid
  id_token, failed code exchange, unknown/expired state.
- Full-transport acceptance: an unattended ``register -> authorize -> token``
  run obtains no token, while the gated flow completes end-to-end with PKCE.

Google's token and JWKS endpoints are mocked with respx; id_tokens are signed
with a test RSA key so real signature verification runs.
"""

from __future__ import annotations

import hashlib
import time
from base64 import urlsafe_b64encode
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import respx
from authlib.jose import JsonWebKey, JsonWebToken
from mcp.server.auth.provider import AuthorizationParams
from mcp.shared.auth import OAuthClientInformationFull
from pydantic import AnyUrl
from starlette.applications import Starlette
from starlette.routing import Route

from intervals_icu_mcp.google_oauth import (
    GOOGLE_CALLBACK_PATH,
    GOOGLE_JWKS_URI,
    GOOGLE_TOKEN_ENDPOINT,
    GoogleGatedFirestoreOAuthProvider,
    GoogleGatedInMemoryOAuthProvider,
    GoogleOAuthConfig,
    make_google_callback_handler,
)

SERVER_URL = "https://mcp.example.com"
CLAUDE_REDIRECT = "https://claude.ai/api/mcp/auth_callback"
ALLOWED_EMAIL = "stephen@example.com"
GOOGLE_CLIENT_ID = "google-client-id.apps.googleusercontent.com"

# --- signing key / id_token helpers -----------------------------------------

_TEST_KEY = JsonWebKey.generate_key("RSA", 2048, is_private=True, options={"kid": "test-key"})
_JWKS = {"keys": [_TEST_KEY.as_dict(is_private=False)]}


def make_id_token(
    *,
    email: str = ALLOWED_EMAIL,
    email_verified: Any = True,
    nonce: str | None = None,
    aud: str = GOOGLE_CLIENT_ID,
    iss: str = "https://accounts.google.com",
    exp_offset: int = 3600,
    omit: tuple[str, ...] = (),
) -> str:
    claims: dict[str, Any] = {
        "iss": iss,
        "aud": aud,
        "sub": "1234567890",
        "email": email,
        "email_verified": email_verified,
        "iat": int(time.time()),
        "exp": int(time.time()) + exp_offset,
    }
    if nonce is not None:
        claims["nonce"] = nonce
    for key in omit:
        claims.pop(key, None)
    token = JsonWebToken(["RS256"]).encode({"alg": "RS256", "kid": "test-key"}, claims, _TEST_KEY)
    return token.decode("ascii")


def mock_google(id_token: str | None, *, exchange_status: int = 200) -> None:
    """Mock Google's token + JWKS endpoints on respx's default router."""
    payload: dict[str, Any] = {"access_token": "google-access", "token_type": "Bearer"}
    if id_token is not None:
        payload["id_token"] = id_token
    respx.post(GOOGLE_TOKEN_ENDPOINT).mock(
        return_value=httpx.Response(exchange_status, json=payload if exchange_status == 200 else {})
    )
    respx.get(GOOGLE_JWKS_URI).mock(return_value=httpx.Response(200, json=_JWKS))


# --- provider / app fixtures -------------------------------------------------


def make_config(allowed: str = ALLOWED_EMAIL) -> GoogleOAuthConfig:
    return GoogleOAuthConfig(
        client_id=GOOGLE_CLIENT_ID,
        client_secret="google-secret",
        allowed_emails=frozenset({allowed}),
        redirect_uri=f"{SERVER_URL}{GOOGLE_CALLBACK_PATH}",
    )


def make_provider(config: GoogleOAuthConfig | None = None) -> GoogleGatedInMemoryOAuthProvider:
    return GoogleGatedInMemoryOAuthProvider(
        base_url=SERVER_URL,
        required_scopes=["mcp"],
        google_config=config or make_config(),
    )


def make_client_info(client_id: str = "claude-client") -> OAuthClientInformationFull:
    return OAuthClientInformationFull(
        client_id=client_id,
        client_secret="client-secret",
        redirect_uris=[AnyUrl(CLAUDE_REDIRECT)],
        scope="mcp",
    )


def make_params(state: str = "claude-state") -> AuthorizationParams:
    return AuthorizationParams(
        state=state,
        scopes=["mcp"],
        code_challenge="c" * 43,
        redirect_uri=AnyUrl(CLAUDE_REDIRECT),
        redirect_uri_provided_explicitly=True,
    )


async def start_authorize(provider: GoogleGatedInMemoryOAuthProvider) -> tuple[str, str]:
    """Register a client, call authorize, return (google_url, nonce)."""
    client = make_client_info()
    await provider.register_client(client)
    google_url = await provider.authorize(client, make_params())
    nonce = parse_qs(urlparse(google_url).query)["state"][0]
    return google_url, nonce


def callback_app(provider: GoogleGatedInMemoryOAuthProvider) -> Starlette:
    return Starlette(routes=[Route(GOOGLE_CALLBACK_PATH, make_google_callback_handler(provider))])


async def get_callback(
    provider: GoogleGatedInMemoryOAuthProvider, query: dict[str, str]
) -> httpx.Response:
    transport = httpx.ASGITransport(app=callback_app(provider))
    async with httpx.AsyncClient(transport=transport, base_url=SERVER_URL) as tc:
        return await tc.get(GOOGLE_CALLBACK_PATH, params=query)


# --- authorize() redirects to Google -----------------------------------------


class TestAuthorizeRedirectsToGoogle:
    async def test_authorize_returns_google_url_not_a_code(self):
        provider = make_provider()
        google_url, nonce = await start_authorize(provider)

        parsed = urlparse(google_url)
        assert parsed.hostname == "accounts.google.com"
        query = parse_qs(parsed.query)
        assert query["client_id"] == [GOOGLE_CLIENT_ID]
        assert query["redirect_uri"] == [f"{SERVER_URL}{GOOGLE_CALLBACK_PATH}"]
        assert query["response_type"] == ["code"]
        assert "openid" in query["scope"][0] and "email" in query["scope"][0]
        assert query["nonce"] == [nonce]
        # No MCP auth code was issued.
        assert provider.auth_codes == {}

    async def test_each_authorize_gets_a_fresh_nonce(self):
        provider = make_provider()
        _, nonce1 = await start_authorize(provider)
        _, nonce2 = await start_authorize(provider)
        assert nonce1 != nonce2


# --- callback: success path ---------------------------------------------------


class TestCallbackSuccess:
    @respx.mock
    async def test_allowlisted_email_completes_original_request(self):
        provider = make_provider()
        _, nonce = await start_authorize(provider)
        mock_google(make_id_token(nonce=nonce))

        resp = await get_callback(provider, {"code": "google-code", "state": nonce})

        assert resp.status_code == 302
        location = resp.headers["location"]
        assert location.startswith(CLAUDE_REDIRECT)
        query = parse_qs(urlparse(location).query)
        assert query["state"] == ["claude-state"]
        code = query["code"][0]
        # The issued code carries the original PKCE challenge.
        assert provider.auth_codes[code].code_challenge == "c" * 43

    @respx.mock
    async def test_email_verified_as_string_true_is_accepted(self):
        provider = make_provider()
        _, nonce = await start_authorize(provider)
        mock_google(make_id_token(nonce=nonce, email_verified="true"))

        resp = await get_callback(provider, {"code": "google-code", "state": nonce})
        assert resp.status_code == 302

    @respx.mock
    async def test_allowlist_is_case_insensitive(self):
        provider = make_provider()
        _, nonce = await start_authorize(provider)
        mock_google(make_id_token(nonce=nonce, email=ALLOWED_EMAIL.upper()))

        resp = await get_callback(provider, {"code": "google-code", "state": nonce})
        assert resp.status_code == 302

    @respx.mock
    async def test_nonce_is_single_use(self):
        provider = make_provider()
        _, nonce = await start_authorize(provider)
        mock_google(make_id_token(nonce=nonce))

        first = await get_callback(provider, {"code": "google-code", "state": nonce})
        assert first.status_code == 302
        replay = await get_callback(provider, {"code": "google-code", "state": nonce})
        assert replay.status_code == 400


# --- callback: rejection paths ------------------------------------------------


class TestCallbackRejections:
    async def assert_rejected(
        self,
        provider: GoogleGatedInMemoryOAuthProvider,
        query: dict[str, str],
        expected_status: int,
    ) -> None:
        resp = await get_callback(provider, query)
        assert resp.status_code == expected_status
        # No MCP authorization code may exist after a rejection.
        assert provider.auth_codes == {}

    @respx.mock
    async def test_non_allowlisted_email_is_rejected_401(self):
        provider = make_provider()
        _, nonce = await start_authorize(provider)
        mock_google(make_id_token(nonce=nonce, email="attacker@example.com"))
        await self.assert_rejected(provider, {"code": "google-code", "state": nonce}, 401)

    @respx.mock
    async def test_unverified_email_is_rejected_401(self):
        provider = make_provider()
        _, nonce = await start_authorize(provider)
        mock_google(make_id_token(nonce=nonce, email_verified=False))
        await self.assert_rejected(provider, {"code": "google-code", "state": nonce}, 401)

    @respx.mock
    async def test_missing_email_is_rejected_401(self):
        provider = make_provider()
        _, nonce = await start_authorize(provider)
        mock_google(make_id_token(nonce=nonce, omit=("email",)))
        await self.assert_rejected(provider, {"code": "google-code", "state": nonce}, 401)

    @respx.mock
    async def test_missing_id_token_is_rejected_401(self):
        provider = make_provider()
        _, nonce = await start_authorize(provider)
        mock_google(None)
        await self.assert_rejected(provider, {"code": "google-code", "state": nonce}, 401)

    @respx.mock
    async def test_failed_code_exchange_is_rejected_401(self):
        provider = make_provider()
        _, nonce = await start_authorize(provider)
        mock_google(None, exchange_status=400)
        await self.assert_rejected(provider, {"code": "bad-code", "state": nonce}, 401)

    @respx.mock
    async def test_wrong_audience_is_rejected_401(self):
        provider = make_provider()
        _, nonce = await start_authorize(provider)
        mock_google(make_id_token(nonce=nonce, aud="other-client"))
        await self.assert_rejected(provider, {"code": "google-code", "state": nonce}, 401)

    @respx.mock
    async def test_wrong_issuer_is_rejected_401(self):
        provider = make_provider()
        _, nonce = await start_authorize(provider)
        mock_google(make_id_token(nonce=nonce, iss="https://evil.example.com"))
        await self.assert_rejected(provider, {"code": "google-code", "state": nonce}, 401)

    @respx.mock
    async def test_expired_id_token_is_rejected_401(self):
        provider = make_provider()
        _, nonce = await start_authorize(provider)
        mock_google(make_id_token(nonce=nonce, exp_offset=-3600))
        await self.assert_rejected(provider, {"code": "google-code", "state": nonce}, 401)

    @respx.mock
    async def test_nonce_mismatch_is_rejected_401(self):
        provider = make_provider()
        _, nonce = await start_authorize(provider)
        mock_google(make_id_token(nonce="a-different-nonce"))
        await self.assert_rejected(provider, {"code": "google-code", "state": nonce}, 401)

    async def test_unknown_state_is_rejected_400(self):
        provider = make_provider()
        await self.assert_rejected(provider, {"code": "google-code", "state": "never-issued"}, 400)

    async def test_expired_pending_request_is_rejected_400(self, monkeypatch):
        provider = make_provider()
        _, nonce = await start_authorize(provider)
        # Jump past the pending-request TTL.
        real_time = time.time
        monkeypatch.setattr(time, "time", lambda: real_time() + 3600)
        await self.assert_rejected(provider, {"code": "google-code", "state": nonce}, 400)

    async def test_missing_params_rejected_400(self):
        provider = make_provider()
        await self.assert_rejected(provider, {}, 400)

    async def test_google_error_param_rejected_400(self):
        provider = make_provider()
        _, nonce = await start_authorize(provider)
        await self.assert_rejected(provider, {"error": "access_denied", "state": nonce}, 400)


# --- config from env -----------------------------------------------------------


class TestGoogleOAuthConfig:
    def test_from_env_builds_config(self, monkeypatch):
        monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", GOOGLE_CLIENT_ID)
        monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "shh")
        monkeypatch.setenv("MCP_ALLOWED_EMAILS", " Stephen@Example.com , other@example.com ")
        config = GoogleOAuthConfig.from_env(SERVER_URL)
        assert config.client_id == GOOGLE_CLIENT_ID
        assert config.allowed_emails == frozenset({"stephen@example.com", "other@example.com"})
        assert config.redirect_uri == f"{SERVER_URL}{GOOGLE_CALLBACK_PATH}"

    @pytest.mark.parametrize(
        "missing",
        ["GOOGLE_OAUTH_CLIENT_ID", "GOOGLE_OAUTH_CLIENT_SECRET", "MCP_ALLOWED_EMAILS"],
    )
    def test_missing_env_fails_closed(self, monkeypatch, missing):
        monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", GOOGLE_CLIENT_ID)
        monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "shh")
        monkeypatch.setenv("MCP_ALLOWED_EMAILS", ALLOWED_EMAIL)
        monkeypatch.delenv(missing)
        with pytest.raises(RuntimeError, match=missing):
            GoogleOAuthConfig.from_env(SERVER_URL)


# --- Firestore-gated variant ----------------------------------------------------


class _FakeSnapshot:
    def __init__(self, data: dict[str, Any] | None) -> None:
        self._data = data
        self.exists = data is not None

    def to_dict(self) -> dict[str, Any] | None:
        return self._data


class _FakeDoc:
    def __init__(self) -> None:
        self.data: dict[str, Any] | None = None
        self.set_calls: list[dict[str, Any]] = []

    async def get(self) -> _FakeSnapshot:
        return _FakeSnapshot(self.data)

    async def set(self, data: dict[str, Any]) -> None:
        self.set_calls.append(data)
        self.data = data


class TestFirestoreGatedProvider:
    @respx.mock
    async def test_gate_applies_and_completion_persists(self):
        doc = _FakeDoc()
        provider = GoogleGatedFirestoreOAuthProvider(
            base_url=SERVER_URL,
            document=doc,
            google_config=make_config(),
        )
        client = make_client_info()
        await provider.register_client(client)

        google_url = await provider.authorize(client, make_params())
        assert urlparse(google_url).hostname == "accounts.google.com"
        assert provider.auth_codes == {}

        nonce = parse_qs(urlparse(google_url).query)["state"][0]
        mock_google(make_id_token(nonce=nonce))
        redirect_url = await provider.complete_pending_authorization(nonce, "google-code")

        assert redirect_url.startswith(CLAUDE_REDIRECT)
        assert len(provider.auth_codes) == 1
        # Completion persisted the issued code to Firestore.
        assert any("auth_codes" in call and call["auth_codes"] for call in doc.set_calls)


# --- full-transport acceptance ---------------------------------------------------


def _pkce_pair() -> tuple[str, str]:
    verifier = "v" * 64
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def _auth_http_app() -> tuple[Any, GoogleGatedInMemoryOAuthProvider]:
    """A FastMCP HTTP app with the gated provider + callback route, as deployed."""
    from fastmcp import FastMCP
    from mcp.server.auth.settings import ClientRegistrationOptions

    provider = GoogleGatedInMemoryOAuthProvider(
        base_url=SERVER_URL,
        client_registration_options=ClientRegistrationOptions(
            enabled=True, valid_scopes=["mcp"], default_scopes=["mcp"]
        ),
        required_scopes=["mcp"],
        google_config=make_config(),
    )
    test_mcp = FastMCP("gate-test")
    test_mcp.auth = provider
    test_mcp.custom_route(GOOGLE_CALLBACK_PATH, methods=["GET"])(
        make_google_callback_handler(provider)
    )
    return test_mcp.http_app(), provider


class TestFullTransportAcceptance:
    async def test_unattended_register_authorize_token_gets_no_token(self):
        """SEC-1 acceptance: the old headless takeover path yields no token."""
        app, provider = _auth_http_app()
        transport = httpx.ASGITransport(app=app)
        _, challenge = _pkce_pair()
        async with httpx.AsyncClient(transport=transport, base_url=SERVER_URL) as tc:
            reg = await tc.post(
                "/register",
                json={
                    "redirect_uris": [CLAUDE_REDIRECT],
                    "token_endpoint_auth_method": "none",
                    "grant_types": ["authorization_code", "refresh_token"],
                    "response_types": ["code"],
                    "scope": "mcp",
                },
            )
            assert reg.status_code == 201
            client_id = reg.json()["client_id"]

            authz = await tc.get(
                "/authorize",
                params={
                    "response_type": "code",
                    "client_id": client_id,
                    "redirect_uri": CLAUDE_REDIRECT,
                    "state": "s",
                    "code_challenge": challenge,
                    "code_challenge_method": "S256",
                    "scope": "mcp",
                },
            )
            # Redirects to Google Sign-In, NOT back to the client with a code.
            assert authz.status_code in (302, 307)
            assert urlparse(authz.headers["location"]).hostname == "accounts.google.com"
            assert provider.auth_codes == {}

            token = await tc.post(
                "/token",
                data={
                    "grant_type": "authorization_code",
                    "code": "guessed-code",
                    "redirect_uri": CLAUDE_REDIRECT,
                    "client_id": client_id,
                    "code_verifier": "v" * 64,
                },
            )
            assert token.status_code == 400
            assert "access_token" not in token.json()

    @respx.mock
    async def test_gated_flow_completes_end_to_end_with_pkce(self):
        """Signing in via Google as the allowlisted email yields a working token."""
        app, provider = _auth_http_app()
        transport = httpx.ASGITransport(app=app)
        verifier, challenge = _pkce_pair()
        async with httpx.AsyncClient(transport=transport, base_url=SERVER_URL) as tc:
            reg = await tc.post(
                "/register",
                json={
                    "redirect_uris": [CLAUDE_REDIRECT],
                    "token_endpoint_auth_method": "none",
                    "grant_types": ["authorization_code", "refresh_token"],
                    "response_types": ["code"],
                    "scope": "mcp",
                },
            )
            client_id = reg.json()["client_id"]

            authz = await tc.get(
                "/authorize",
                params={
                    "response_type": "code",
                    "client_id": client_id,
                    "redirect_uri": CLAUDE_REDIRECT,
                    "state": "claude-state",
                    "code_challenge": challenge,
                    "code_challenge_method": "S256",
                    "scope": "mcp",
                },
            )
            google_url = authz.headers["location"]
            nonce = parse_qs(urlparse(google_url).query)["state"][0]

            mock_google(make_id_token(nonce=nonce))
            cb = await tc.get(GOOGLE_CALLBACK_PATH, params={"code": "google-code", "state": nonce})
            assert cb.status_code == 302
            back = urlparse(cb.headers["location"])
            assert cb.headers["location"].startswith(CLAUDE_REDIRECT)
            query = parse_qs(back.query)
            assert query["state"] == ["claude-state"]

            token = await tc.post(
                "/token",
                data={
                    "grant_type": "authorization_code",
                    "code": query["code"][0],
                    "redirect_uri": CLAUDE_REDIRECT,
                    "client_id": client_id,
                    "code_verifier": verifier,
                },
            )
            assert token.status_code == 200
            body = token.json()
            assert body["access_token"]
            assert (await provider.verify_token(body["access_token"])) is not None


# --- secrets hygiene ---------------------------------------------------------------


class TestSecretsHygiene:
    @respx.mock
    async def test_client_secret_never_logged(self, caplog):
        """The Google client secret must not appear in any log record."""
        import logging

        caplog.set_level(logging.DEBUG, logger="intervals_icu_mcp.google_oauth")
        provider = make_provider()
        _, nonce = await start_authorize(provider)
        mock_google(make_id_token(nonce=nonce))
        await get_callback(provider, {"code": "google-code", "state": nonce})

        for record in caplog.records:
            assert "google-secret" not in record.getMessage()
