"""Google-identity gate for the hosted OAuth flow (R1, SEC-1).

Why this exists: fastmcp's ``InMemoryOAuthProvider`` (and our Firestore
subclass) auto-approves every authorization request — ``authorize()`` issues a
code with zero identity check, so anyone who finds the public URL can run
``/register -> /authorize -> /token`` headless and obtain a token that runs
every tool against the single intervals.icu key.

This module keeps the server as its own OAuth Authorization Server towards
claude.ai, but federates the *user-authentication* step to Google Sign-In:

1. ``authorize()`` stashes the pending claude.ai request under a short-lived
   random nonce and redirects the browser to Google's authorization endpoint
   (scope ``openid email``, our nonce as both ``state`` and OIDC ``nonce``).
2. ``/auth/google/callback`` exchanges the Google code, verifies the
   ``id_token`` (signature via Google's JWKS, issuer, audience, expiry,
   nonce), and requires a **verified** email that appears in the
   ``MCP_ALLOWED_EMAILS`` allowlist.
3. Only then is the stashed request completed — the base provider issues the
   MCP authorization code and the browser returns to claude.ai's
   ``redirect_uri``. PKCE and redirect-uri validation are untouched.

Configuration is fail-closed: ``GoogleOAuthConfig.from_env`` raises if the
Google client credentials or the allowlist are missing, so the server can
never silently boot with an open OAuth flow. There is deliberately no bypass
flag and no baked-in default email.

Pending requests live in process memory only. With max-instances=1 the only
loss scenario is an instance restart in the middle of a sign-in, which simply
requires re-starting authorization from claude.ai.
"""

from __future__ import annotations

import logging
import os
import secrets
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlencode

import httpx
from authlib.jose import JsonWebKey, JsonWebToken
from authlib.jose.errors import JoseError
from fastmcp.server.auth.providers.in_memory import InMemoryOAuthProvider
from mcp.server.auth.provider import AuthorizationParams
from mcp.shared.auth import OAuthClientInformationFull
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response

from intervals_icu_mcp.firestore_oauth import FirestoreOAuthProvider

logger = logging.getLogger(__name__)

GOOGLE_CALLBACK_PATH = "/auth/google/callback"
GOOGLE_AUTHORIZATION_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
GOOGLE_JWKS_URI = "https://www.googleapis.com/oauth2/v3/certs"
GOOGLE_ISSUERS = ("https://accounts.google.com", "accounts.google.com")

PENDING_AUTHORIZATION_TTL_SECONDS = 10 * 60
GOOGLE_HTTP_TIMEOUT_SECONDS = 10.0

_REQUIRED_ENV = ("GOOGLE_OAUTH_CLIENT_ID", "GOOGLE_OAUTH_CLIENT_SECRET", "MCP_ALLOWED_EMAILS")


class GoogleAuthError(Exception):
    """A rejected or failed Google sign-in; maps to an HTTP error response."""

    def __init__(self, status_code: int, error: str, description: str) -> None:
        super().__init__(description)
        self.status_code = status_code
        self.error = error
        self.description = description


@dataclass(frozen=True)
class GoogleOAuthConfig:
    """Google OAuth client + allowlist configuration."""

    client_id: str
    client_secret: str
    allowed_emails: frozenset[str]
    redirect_uri: str

    @classmethod
    def from_env(cls, server_url: str) -> GoogleOAuthConfig:
        """Build from environment; fail closed if anything is missing.

        Requires GOOGLE_OAUTH_CLIENT_ID, GOOGLE_OAUTH_CLIENT_SECRET, and
        MCP_ALLOWED_EMAILS (comma-separated). No defaults: a hosted deploy
        must never boot with an unlocked OAuth flow.
        """
        client_id = os.getenv("GOOGLE_OAUTH_CLIENT_ID", "").strip()
        client_secret = os.getenv("GOOGLE_OAUTH_CLIENT_SECRET", "").strip()
        allowed_emails = frozenset(
            email.strip().lower()
            for email in os.getenv("MCP_ALLOWED_EMAILS", "").split(",")
            if email.strip()
        )
        values = (client_id, client_secret, allowed_emails)
        missing = [name for name, value in zip(_REQUIRED_ENV, values, strict=False) if not value]
        if missing:
            raise RuntimeError(
                "Google OAuth gate is not configured; refusing to run an open OAuth flow. "
                f"Missing: {', '.join(missing)}"
            )
        return cls(
            client_id=client_id,
            client_secret=client_secret,
            allowed_emails=allowed_emails,
            redirect_uri=f"{server_url.rstrip('/')}{GOOGLE_CALLBACK_PATH}",
        )


@dataclass
class PendingAuthorization:
    """A stashed claude.ai authorization request awaiting Google sign-in."""

    client: OAuthClientInformationFull
    params: AuthorizationParams
    expires_at: float = field(
        default_factory=lambda: time.time() + PENDING_AUTHORIZATION_TTL_SECONDS
    )


async def _exchange_google_code(config: GoogleOAuthConfig, google_code: str) -> dict[str, Any]:
    """Exchange the Google authorization code for tokens."""
    data = {
        "code": google_code,
        "client_id": config.client_id,
        "client_secret": config.client_secret,
        "redirect_uri": config.redirect_uri,
        "grant_type": "authorization_code",
    }
    try:
        async with httpx.AsyncClient(timeout=GOOGLE_HTTP_TIMEOUT_SECONDS) as http:
            response = await http.post(GOOGLE_TOKEN_ENDPOINT, data=data)
    except httpx.HTTPError as exc:
        raise GoogleAuthError(
            502, "temporarily_unavailable", "Could not reach Google to verify sign-in."
        ) from exc
    if response.status_code != 200:
        # Log the status only — the body could echo request parameters.
        logger.warning("Google code exchange failed with HTTP %d", response.status_code)
        raise GoogleAuthError(401, "invalid_grant", "Google sign-in could not be verified.")
    payload: dict[str, Any] = response.json()
    return payload


async def _fetch_google_jwks() -> dict[str, Any]:
    """Fetch Google's current signing keys (single-user load; no caching needed)."""
    try:
        async with httpx.AsyncClient(timeout=GOOGLE_HTTP_TIMEOUT_SECONDS) as http:
            response = await http.get(GOOGLE_JWKS_URI)
    except httpx.HTTPError as exc:
        raise GoogleAuthError(
            502, "temporarily_unavailable", "Could not fetch Google signing keys."
        ) from exc
    if response.status_code != 200:
        logger.warning("Google JWKS fetch failed with HTTP %d", response.status_code)
        raise GoogleAuthError(
            502, "temporarily_unavailable", "Could not fetch Google signing keys."
        )
    payload: dict[str, Any] = response.json()
    return payload


async def verify_google_identity(
    config: GoogleOAuthConfig, google_code: str, expected_nonce: str
) -> str:
    """Verify the Google sign-in and return the allowlisted email.

    Raises GoogleAuthError (401) unless the id_token verifies against Google's
    JWKS with the right issuer/audience/expiry/nonce AND carries a verified
    email present in the allowlist.
    """
    token_payload = await _exchange_google_code(config, google_code)
    raw_id_token = token_payload.get("id_token")
    if not isinstance(raw_id_token, str) or not raw_id_token:
        raise GoogleAuthError(401, "invalid_grant", "Google response contained no id_token.")

    jwks = await _fetch_google_jwks()
    try:
        key_set = JsonWebKey.import_key_set(jwks)
        # authlib ships no type stubs; go through Any so strict pyright
        # doesn't trip on the partially-unknown decode() signature.
        jwt: Any = JsonWebToken(["RS256"])
        claims: Any = jwt.decode(
            raw_id_token,
            key_set,
            claims_options={
                "iss": {"essential": True, "values": list(GOOGLE_ISSUERS)},
                "aud": {"essential": True, "value": config.client_id},
                "exp": {"essential": True},
            },
        )
        claims.validate()
    except (JoseError, ValueError, KeyError) as exc:
        logger.warning("Google id_token failed verification (%s)", exc.__class__.__name__)
        raise GoogleAuthError(401, "invalid_token", "Google id_token failed verification.") from exc

    if claims.get("nonce") != expected_nonce:
        raise GoogleAuthError(401, "invalid_token", "Google id_token nonce mismatch.")

    email = str(claims.get("email") or "").strip().lower()
    email_verified = claims.get("email_verified")
    if isinstance(email_verified, str):
        email_verified = email_verified.strip().lower() == "true"
    if not email or email_verified is not True:
        raise GoogleAuthError(
            401, "access_denied", "Google account email is missing or unverified."
        )
    if email not in config.allowed_emails:
        logger.warning("Rejected Google sign-in from non-allowlisted account: %s", email)
        raise GoogleAuthError(401, "access_denied", "This connector is locked to its owner.")
    return email


class GoogleGateProvider(InMemoryOAuthProvider):
    """Provider base whose ``authorize()`` federates to Google Sign-In.

    Subclasses combine this with a concrete token store (in-memory or
    Firestore). ``super().authorize(...)`` inside
    ``complete_pending_authorization`` resolves via the MRO to the store's own
    authorize, so code issuance (and Firestore persistence) is unchanged.
    """

    _google_config: GoogleOAuthConfig
    _pending_authorizations: dict[str, PendingAuthorization]

    def _init_google_gate(self, google_config: GoogleOAuthConfig) -> None:
        self._google_config = google_config
        self._pending_authorizations = {}

    @property
    def google_config(self) -> GoogleOAuthConfig:
        return self._google_config

    def _prune_expired_pending(self) -> None:
        now = time.time()
        expired = [
            nonce
            for nonce, pending in self._pending_authorizations.items()
            if pending.expires_at <= now
        ]
        for nonce in expired:
            del self._pending_authorizations[nonce]

    async def authorize(
        self, client: OAuthClientInformationFull, params: AuthorizationParams
    ) -> str:
        """Stash the request and send the browser to Google instead of issuing a code."""
        self._prune_expired_pending()
        nonce = secrets.token_urlsafe(32)
        self._pending_authorizations[nonce] = PendingAuthorization(client=client, params=params)
        query = urlencode(
            {
                "client_id": self._google_config.client_id,
                "redirect_uri": self._google_config.redirect_uri,
                "response_type": "code",
                "scope": "openid email",
                "state": nonce,
                "nonce": nonce,
                "prompt": "select_account",
            }
        )
        return f"{GOOGLE_AUTHORIZATION_ENDPOINT}?{query}"

    async def complete_pending_authorization(self, nonce: str, google_code: str) -> str:
        """Verify Google sign-in, then finish the stashed request.

        Returns the redirect URI back to the MCP client (with the issued
        code). The nonce is single-use: it is consumed before verification, so
        a failed sign-in requires restarting authorization from the client.
        """
        self._prune_expired_pending()
        pending = self._pending_authorizations.pop(nonce, None)
        if pending is None:
            raise GoogleAuthError(
                400,
                "invalid_request",
                "Unknown or expired authorization request; restart authorization "
                "from the MCP client.",
            )
        email = await verify_google_identity(self._google_config, google_code, nonce)
        logger.info("Google sign-in verified for %s; issuing MCP authorization code", email)
        return await super().authorize(pending.client, pending.params)


class GoogleGatedInMemoryOAuthProvider(GoogleGateProvider):
    """In-memory token store with the Google identity gate."""

    def __init__(self, *args: Any, google_config: GoogleOAuthConfig, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._init_google_gate(google_config)


class GoogleGatedFirestoreOAuthProvider(GoogleGateProvider, FirestoreOAuthProvider):
    """Firestore-backed token store with the Google identity gate.

    MRO: gate -> FirestoreOAuthProvider -> InMemoryOAuthProvider, so incoming
    ``authorize()`` hits the gate, while completion flows through Firestore's
    authorize (which persists the issued code).
    """

    def __init__(self, *args: Any, google_config: GoogleOAuthConfig, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._init_google_gate(google_config)


def make_google_callback_handler(
    provider: GoogleGateProvider,
) -> Callable[[Request], Awaitable[Response]]:
    """Build the ``/auth/google/callback`` route handler for ``mcp.custom_route``."""

    async def google_callback(request: Request) -> Response:
        params = request.query_params
        error = params.get("error")
        if error:
            logger.warning("Google sign-in returned error: %s", error)
            return _error_response(400, "access_denied", f"Google sign-in failed: {error}")
        google_code = params.get("code")
        nonce = params.get("state")
        if not google_code or not nonce:
            return _error_response(400, "invalid_request", "Missing code or state parameter.")
        try:
            redirect_url = await provider.complete_pending_authorization(nonce, google_code)
        except GoogleAuthError as exc:
            logger.warning("Google callback rejected: %s", exc.description)
            return _error_response(exc.status_code, exc.error, exc.description)
        return RedirectResponse(redirect_url, status_code=302)

    return google_callback


def _error_response(status_code: int, error: str, description: str) -> JSONResponse:
    return JSONResponse({"error": error, "error_description": description}, status_code=status_code)
