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
from typing import Any, Protocol

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
    return client.collection(collection).document(document_id)


class FirestoreOAuthProvider(InMemoryOAuthProvider):
    """OAuth provider that mirrors all token state into a single Firestore doc."""

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

    def _restore(self, data: dict[str, Any]) -> None:
        self.clients = {
            k: OAuthClientInformationFull.model_validate_json(v)
            for k, v in (data.get("clients") or {}).items()
        }
        self.auth_codes = {
            k: AuthorizationCode.model_validate_json(v)
            for k, v in (data.get("auth_codes") or {}).items()
        }
        self.access_tokens = {
            k: AccessToken.model_validate_json(v)
            for k, v in (data.get("access_tokens") or {}).items()
        }
        self.refresh_tokens = {
            k: RefreshToken.model_validate_json(v)
            for k, v in (data.get("refresh_tokens") or {}).items()
        }
        self._access_to_refresh_map = dict(data.get("access_to_refresh_map") or {})
        self._refresh_to_access_map = dict(data.get("refresh_to_access_map") or {})

    # ----- persistence helpers -----

    async def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        async with self._load_lock:
            if self._loaded:
                return
            try:
                snapshot = await self._doc.get()
                exists = bool(getattr(snapshot, "exists", False))
                if exists:
                    data = snapshot.to_dict() or {}
                    self._restore(data)
                    logger.info(
                        "Loaded OAuth state from Firestore: clients=%d access=%d refresh=%d auth_codes=%d",
                        len(self.clients),
                        len(self.access_tokens),
                        len(self.refresh_tokens),
                        len(self.auth_codes),
                    )
                else:
                    logger.info("No prior OAuth state in Firestore; starting empty")
            except Exception:
                logger.exception("Failed to load OAuth state from Firestore; starting empty")
            self._loaded = True

    async def _persist(self) -> None:
        try:
            await self._doc.set(self._to_dict())
        except Exception:
            logger.exception("Failed to persist OAuth state to Firestore")

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
        await self._persist()
        return result

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: RefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        await self._ensure_loaded()
        result = await super().exchange_refresh_token(client, refresh_token, scopes)
        await self._persist()
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
