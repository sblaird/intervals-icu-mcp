"""HTTP/SSE entry point with single-user OAuth for hosted deployments.

The upstream entry point (``intervals_icu_mcp.server``) builds a FastMCP
instance for stdio use. We import that module to pick up the same instance
(with all 48 tools, the resource, and the prompts already registered),
attach an in-memory OAuth provider for claude.ai's Custom Connector to
authenticate against, and run it over HTTP.

Required env vars at runtime:
    INTERVALS_ICU_API_KEY    intervals.icu API key (HTTP Basic password)
    INTERVALS_ICU_ATHLETE_ID athlete id, e.g. "i12345"
    MCP_SERVER_URL           public HTTPS base URL of this service
    PORT                     bind port (Cloud Run sets this)
"""

from __future__ import annotations

import logging
import os

from fastmcp.server.auth.auth import ClientRegistrationOptions, RevocationOptions
from fastmcp.server.auth.providers.in_memory import InMemoryOAuthProvider
from starlette.requests import Request
from starlette.responses import JSONResponse

import intervals_icu_mcp.server as server_module  # noqa: F401  # registers all tools

logger = logging.getLogger("intervals_icu_mcp.remote")


def main() -> None:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    server_url = os.getenv("MCP_SERVER_URL", "").rstrip("/")
    if not server_url:
        raise RuntimeError("MCP_SERVER_URL must be set to the public HTTPS URL of this service")

    mcp = server_module.mcp
    mcp.auth = InMemoryOAuthProvider(
        base_url=server_url,
        client_registration_options=ClientRegistrationOptions(
            enabled=True,
            valid_scopes=["mcp"],
            default_scopes=["mcp"],
        ),
        revocation_options=RevocationOptions(enabled=True),
        required_scopes=["mcp"],
    )

    # Workaround for fastmcp 2.12.4: the WWW-Authenticate header on 401s points
    # to /.well-known/oauth-protected-resource (no suffix), but only the
    # /sse-suffixed route is registered. Alias the no-suffix path to the same
    # resource metadata so claude.ai's discovery follows correctly.
    @mcp.custom_route("/.well-known/oauth-protected-resource", methods=["GET", "OPTIONS", "HEAD"])
    async def protected_resource_metadata(_request: Request) -> JSONResponse:
        return JSONResponse(
            {
                "resource": f"{server_url}/sse",
                "authorization_servers": [f"{server_url}/"],
                "scopes_supported": ["mcp"],
                "bearer_methods_supported": ["header"],
            }
        )

    port = int(os.getenv("PORT", "8080"))
    host = os.getenv("HOST", "0.0.0.0")
    transport = os.getenv("MCP_TRANSPORT", "sse")
    logger.info("Starting OAuth-protected MCP server on %s:%s (transport=%s, issuer=%s)", host, port, transport, server_url)
    mcp.run(transport=transport, host=host, port=port)


if __name__ == "__main__":
    main()
