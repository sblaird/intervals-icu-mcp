"""HTTP/SSE entry point with single-user OAuth for hosted deployments.

The upstream entry point (``intervals_icu_mcp.server``) builds a FastMCP
instance for stdio use. We import that module to pick up the same instance
(with all 48 tools, the resource, and the prompts already registered),
attach an OAuth provider for claude.ai's Custom Connector to authenticate
against, and run it over HTTP.

Required env vars at runtime:
    INTERVALS_ICU_API_KEY    intervals.icu API key (HTTP Basic password)
    INTERVALS_ICU_ATHLETE_ID athlete id, e.g. "i12345"
    MCP_SERVER_URL           public HTTPS base URL of this service
    PORT                     bind port (Cloud Run sets this)

Optional:
    OAUTH_TOKEN_STORE        "memory" (default) or "firestore". When set to
                             "firestore", OAuth state survives revisions.
    OAUTH_FIRESTORE_PROJECT  GCP project id for Firestore (defaults to ADC).
    OAUTH_FIRESTORE_COLLECTION/OAUTH_FIRESTORE_DOCUMENT  override the doc path.
    MCP_STATELESS_HTTP       "1"/"true" (default) to run streamable-http in
                             stateless mode — every request creates a fresh
                             transport, so cold-start session loss can't strand
                             claude.ai. Set to "0" to use stateful sessions
                             (in-memory; will 400 after a cold start).
"""

from __future__ import annotations

import logging
import os
from typing import Any

from fastmcp.server.auth.providers.in_memory import InMemoryOAuthProvider
from mcp.server.auth.settings import ClientRegistrationOptions, RevocationOptions
from starlette.requests import Request
from starlette.responses import JSONResponse

import intervals_icu_mcp.server as server_module  # noqa: F401  # registers all tools
from intervals_icu_mcp.firestore_oauth import FirestoreOAuthProvider

logger = logging.getLogger("intervals_icu_mcp.remote")


def main() -> None:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    server_url = os.getenv("MCP_SERVER_URL", "").rstrip("/")
    if not server_url:
        raise RuntimeError("MCP_SERVER_URL must be set to the public HTTPS URL of this service")

    transport = os.getenv("MCP_TRANSPORT", "streamable-http")
    transport_path = "/mcp" if transport in ("streamable-http", "http") else "/sse"
    resource_url = f"{server_url}{transport_path}"

    mcp = server_module.mcp
    client_registration_options = ClientRegistrationOptions(
        enabled=True,
        valid_scopes=["mcp"],
        default_scopes=["mcp"],
    )
    revocation_options = RevocationOptions(enabled=True)
    token_store = os.getenv("OAUTH_TOKEN_STORE", "memory").lower()
    if token_store == "firestore":
        mcp.auth = FirestoreOAuthProvider(
            base_url=server_url,
            client_registration_options=client_registration_options,
            revocation_options=revocation_options,
            required_scopes=["mcp"],
            project=os.getenv("OAUTH_FIRESTORE_PROJECT") or None,
            collection=os.getenv("OAUTH_FIRESTORE_COLLECTION", "oauth_state"),
            document_id=os.getenv("OAUTH_FIRESTORE_DOCUMENT", "singleton"),
        )
        logger.info("Using Firestore-backed OAuth token store")
    else:
        mcp.auth = InMemoryOAuthProvider(
            base_url=server_url,
            client_registration_options=client_registration_options,
            revocation_options=revocation_options,
            required_scopes=["mcp"],
        )
        logger.info("Using in-memory OAuth token store (state will not survive revisions)")

    # Workaround for fastmcp 2.12.4: the WWW-Authenticate header on 401s points
    # to /.well-known/oauth-protected-resource (no suffix), but only the
    # per-resource path is registered. Alias the no-suffix path to the same
    # resource metadata so claude.ai's discovery follows correctly.
    async def protected_resource_metadata(_request: Request) -> JSONResponse:
        return JSONResponse(
            {
                "resource": resource_url,
                "authorization_servers": [f"{server_url}/"],
                "scopes_supported": ["mcp"],
                "bearer_methods_supported": ["header"],
            }
        )

    mcp.custom_route("/.well-known/oauth-protected-resource", methods=["GET", "OPTIONS", "HEAD"])(
        protected_resource_metadata
    )

    port = int(os.getenv("PORT", "8080"))
    host = os.getenv("HOST", "0.0.0.0")
    stateless_http = os.getenv("MCP_STATELESS_HTTP", "1").lower() in ("1", "true", "yes")
    logger.info(
        "Starting OAuth-protected MCP server on %s:%s (transport=%s, stateless=%s, issuer=%s)",
        host,
        port,
        transport,
        stateless_http,
        server_url,
    )
    run_kwargs: dict[str, Any] = {"transport": transport, "host": host, "port": port}
    if transport in ("streamable-http", "http"):
        run_kwargs["stateless_http"] = stateless_http
    mcp.run(**run_kwargs)


if __name__ == "__main__":
    main()
