"""Firestore-backed OAuth provider for hosted single-user deployments.

Why this exists: the upstream `InMemoryOAuthProvider` keeps every client,
auth code, access token, and refresh token in process memory. On Cloud Run
(or any stateless host), every cold start or new revision wipes that state,
which strands claude.ai's stored connector tokens — the UI keeps showing
"Connected" while every tool call comes back as "didn't complete
authentication."

This subclass keeps the upstream protocol logic (auth flows, expiry,
revocation) and adds write-through persistence to a single Firestore document
so state survives revision rollouts and cold starts.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Protocol, cast

from fastmcp.server.auth.providers.in_memory import InMemoryOAuthProvider
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    RefreshToken,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

logger = logging.getLogger(__name__)


class _AsyncDocumentLike(Protocol):
    """Minimal interface this provider needs from a Firestore document reference.

    Defined as a Protocol so tests can pass a fake document without depending
    on google-cloud-firestore.
    """

    async def get(self) -> Any: ...
    async def set(self, data: dict[str, Any]) -> Any: ...


def _build_default_doc(
    project: str | None,
    collection: str,
    document_id: str,
) -> _AsyncDocumentLike:
    """Construct an AsyncDocumentReference using google-cloud-firestore.

    Lazy-imported so the package isn't required for stdio use.
    """
    from google.cloud import firestore  # type: ignore[attr-defined]

    client = firestore.AsyncClient(project=project) if project else firestore.AsyncClient()
    # The real AsyncDocumentReference satisfies _AsyncDocumentLike structurally
    # (it exposes async get/set); cast because google-cloud-firestore's overloaded
    # signatures don't line up with the minimal Protocol pyright checks against.
    return cast("_AsyncDocumentLike", client.collection(collection).document(document_id))


class FirestoreOAuthProvider(InMemoryOAuthProvider):
    """OAuth provider that mirrors all token state into a single Firestore doc.

    Concurrency note (accepted risk, R7/STB-M5): persistence is a whole-doc
    ``set()``, so concurrent writers would be last-writer-wins. The deploy
    runs with ``max-instances=1`` and single-user load, where interleaved
    mutations are not a realistic scenario; a transaction/merge scheme is
    deliberately not implemented.
    """

    # Minimum seconds between reload attempts after a failed Firestore load
    # (avoids a tight retry loop while Firestore is down); requests in the
    # backoff window are served from in-memory state. Class attribute so
    # tests can zero it.
    RELOAD_BACKOFF_SECONDS: float = 5.0

    def __init__(
        self,
        *args: Any,
        document: _AsyncDocumentLike | None = None,
        project: str | None = None,
        collection: str = "oauth_state",
        document_id: str = "singleton",
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._doc: _AsyncDocumentLike = document or _build_default_doc(
            project, collection, document_id
        )
        self._load_lock = asyncio.Lock()
        self._loaded = False
        self._last_load_failure: float | None = None
        self.persist_failures = 0

    # ----- (de)serialization -----

    def _to_dict(self) -> dict[str, Any]:
        return {
            "clients": {k: v.model_dump_json() for k, v in self.clients.items()},
            "auth_codes": {k: v.model_dump_json() for k, v in self.auth_codes.items()},
            "access_tokens": {k: v.model_dump_json() for k, v in self.access_tokens.items()},
            "refresh_tokens": {k: v.model_dump_json() for k, v in self.refresh_tokens.items()},
            "access_to_refresh_map": dict(self._access_to_refresh_map),
            "refresh_to_access_map": dict(self._refresh_to_access_map),
        }

    def _restore(self, data: dict[str, Any], *, merge: bool = False) -> None:
        # Each map is persisted as {key: model_dump_json()} — i.e. dict[str, str].
        # Annotate the raw dicts so the JSON payloads round-trip with known types.
        # With merge=True the deserialized entries overlay the current maps
        # instead of replacing them (used to re-apply in-memory state that
        # accumulated while Firestore loads were failing).
        raw_clients: dict[str, str] = data.get("clients") or {}
        clients = {
            k: OAuthClientInformationFull.model_validate_json(v) for k, v in raw_clients.items()
        }
        raw_auth_codes: dict[str, str] = data.get("auth_codes") or {}
        auth_codes = {
            k: AuthorizationCode.model_validate_json(v) for k, v in raw_auth_codes.items()
        }
        raw_access_tokens: dict[str, str] = data.get("access_tokens") or {}
        access_tokens = {
            k: AccessToken.model_validate_json(v) for k, v in raw_access_tokens.items()
        }
        raw_refresh_tokens: dict[str, str] = data.get("refresh_tokens") or {}
        refresh_tokens = {
            k: RefreshToken.model_validate_json(v) for k, v in raw_refresh_tokens.items()
        }
        access_to_refresh = dict(data.get("access_to_refresh_map") or {})
        refresh_to_access = dict(data.get("refresh_to_access_map") or {})
        if merge:
            self.clients.update(clients)
            self.auth_codes.update(auth_codes)
            self.access_tokens.update(access_tokens)
            self.refresh_tokens.update(refresh_tokens)
            self._access_to_refresh_map.update(access_to_refresh)
            self._refresh_to_access_map.update(refresh_to_access)
        else:
            self.clients = clients
            self.auth_codes = auth_codes
            self.access_tokens = access_tokens
            self.refresh_tokens = refresh_tokens
            self._access_to_refresh_map = access_to_refresh
            self._refresh_to_access_map = refresh_to_access

    # ----- persistence helpers -----

    async def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        async with self._load_lock:
            if self._loaded:
                return
            # R7 (STB-M1): a failed load no longer latches _loaded — the next
            # request outside the backoff window retries, so one Firestore
            # blip after a cold start can't strand the instance with empty
            # auth state until the next cold start.
            if (
                self._last_load_failure is not None
                and time.monotonic() - self._last_load_failure < self.RELOAD_BACKOFF_SECONDS
            ):
                return
            try:
                snapshot = await self._doc.get()
            except Exception:
                self._last_load_failure = time.monotonic()
                logger.exception(
                    "Failed to load OAuth state from Firestore; serving in-memory state "
                    "and retrying on a later request"
                )
                return
            exists = bool(getattr(snapshot, "exists", False))
            if exists:
                data: dict[str, Any] = snapshot.to_dict() or {}
                # Any state accumulated in memory while loads were failing is
                # newer than the snapshot — re-apply it over the restored maps
                # so a late successful load can't erase it.
                pending = self._to_dict() if self._state_size() != (0, 0, 0, 0) else None
                self._restore(data)
                if pending is not None:
                    self._restore(pending, merge=True)
                logger.info(
                    "Loaded OAuth state from Firestore: clients=%d access=%d refresh=%d auth_codes=%d",
                    len(self.clients),
                    len(self.access_tokens),
                    len(self.refresh_tokens),
                    len(self.auth_codes),
                )
            else:
                logger.info("No prior OAuth state in Firestore; starting empty")
            self._loaded = True
            self._last_load_failure = None

    async def _persist(self, *, critical: bool = False) -> None:
        """Write state to Firestore.

        R7 (STB-M2): failures are logged at error and counted; on
        token-issuing paths (``critical=True``) the failure is re-raised so
        the client sees an error instead of a token that would silently die
        at the next cold start.
        """
        try:
            await self._doc.set(self._to_dict())
        except Exception:
            self.persist_failures += 1
            logger.exception(
                "Failed to persist OAuth state to Firestore (failure #%d)",
                self.persist_failures,
            )
            if critical:
                raise

    def _state_size(self) -> tuple[int, int, int, int]:
        return (
            len(self.clients),
            len(self.auth_codes),
            len(self.access_tokens),
            len(self.refresh_tokens),
        )

    # ----- write-path overrides (always persist) -----

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        await self._ensure_loaded()
        await super().register_client(client_info)
        await self._persist()

    async def authorize(
        self, client: OAuthClientInformationFull, params: AuthorizationParams
    ) -> str:
        await self._ensure_loaded()
        result = await super().authorize(client, params)
        await self._persist()
        return result

    async def exchange_authorization_code(
        self,
        client: OAuthClientInformationFull,
        authorization_code: AuthorizationCode,
    ) -> OAuthToken:
        await self._ensure_loaded()
        result = await super().exchange_authorization_code(client, authorization_code)
        # critical: a token that isn't persisted would die silently at the
        # next cold start while the client shows "Connected" (R7).
        await self._persist(critical=True)
        return result

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: RefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        await self._ensure_loaded()
        result = await super().exchange_refresh_token(client, refresh_token, scopes)
        # critical: rotation revoked the old tokens in memory; losing the new
        # pair to a silent persist failure strands the connector (R7).
        await self._persist(critical=True)
        return result

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        await self._ensure_loaded()
        await super().revoke_token(token)
        await self._persist()

    # ----- read-path overrides (persist only if expiry cleanup mutated state) -----

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        await self._ensure_loaded()
        return await super().get_client(client_id)

    async def load_authorization_code(
        self,
        client: OAuthClientInformationFull,
        authorization_code: str,
    ) -> AuthorizationCode | None:
        await self._ensure_loaded()
        before = self._state_size()
        result = await super().load_authorization_code(client, authorization_code)
        if self._state_size() != before:
            await self._persist()
        return result

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> RefreshToken | None:
        await self._ensure_loaded()
        before = self._state_size()
        result = await super().load_refresh_token(client, refresh_token)
        if self._state_size() != before:
            await self._persist()
        return result

    async def load_access_token(self, token: str) -> AccessToken | None:
        await self._ensure_loaded()
        before = self._state_size()
        result = await super().load_access_token(token)
        if self._state_size() != before:
            await self._persist()
        return result
