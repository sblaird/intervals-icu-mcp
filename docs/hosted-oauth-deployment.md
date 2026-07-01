# Hosted OAuth deployment (Cloud Run)

This document covers the **remote** entry point
(`intervals_icu_mcp.remote_server`) that serves the MCP server over streamable
HTTP with an OAuth Custom Connector for claude.ai. It does **not** apply to the
local stdio server (`intervals_icu_mcp.server`), which needs only the two API
credentials in `.env`.

## The "No approval received" / reconnect-to-fix failure

**Symptom:** tool calls (and occasionally even a *read*) return
`No approval received`, and **disconnecting + reconnecting the connector fixes
it** until the next time.

**Root cause:** OAuth state (registered clients, authorization codes, access +
refresh tokens) lives in whichever token store `remote_server.py` selects at
startup. With the **default in-memory store**, every Cloud Run cold start or new
revision **wipes that state**. claude.ai still holds a bearer token, but the
server no longer recognizes it, so:

- a call with the now-unknown token 401s;
- claude.ai tries to refresh — but the persisted **refresh token is gone too**,
  so the refresh fails and it falls back to re-authorizing;
- the client registration/authorization code are also gone → `No approval
  received`.

Reconnecting mints a brand-new registration + token against the *current*
revision, which is why it "fixes" things until the next cold start. The
anomalous *read* requiring approval is the same mechanism — after a cold start
nothing validates, reads included; it is not an approval-UI bug.

## Required production configuration

| Env var | Required value | Why |
|---|---|---|
| `OAUTH_TOKEN_STORE` | `firestore` | Persists OAuth state to a single Firestore document so it survives cold starts / new revisions. Default `memory` is the outage cause above. |
| `MCP_STATELESS_HTTP` | `1` (default) | Every request gets a fresh transport, so a cold start can't strand an in-flight session. Already the default; do not set to `0` in production. |
| `MCP_SERVER_URL` | public HTTPS base URL | OAuth issuer / resource metadata must match the public URL. |
| `INTERVALS_ICU_API_KEY` | intervals.icu API key | HTTP Basic password for the upstream API. |
| `INTERVALS_ICU_ATHLETE_ID` | e.g. `i12345` | Athlete id. |

Optional Firestore overrides (defaults shown):
`OAUTH_FIRESTORE_PROJECT` (defaults to ADC project), `OAUTH_FIRESTORE_COLLECTION`
(`oauth_state`), `OAUTH_FIRESTORE_DOCUMENT` (`singleton`). The service account
needs Firestore read/write (`roles/datastore.user`).

## Verify the deployed values (before)

> gcloud auth may need refreshing first: run `gcloud auth login`.

```bash
# List services to find the name/region:
gcloud run services list --format="table(metadata.name,region,status.url)"

# Inspect the env vars actually set on the live revision:
gcloud run services describe <SERVICE> --region <REGION> \
  --format="value(spec.template.spec.containers[0].env)"
```

Record the current `OAUTH_TOKEN_STORE` / `MCP_STATELESS_HTTP` values. If
`OAUTH_TOKEN_STORE` is unset or `memory`, that is the bug.

## Correct the values (after)

```bash
gcloud run services update <SERVICE> --region <REGION> \
  --update-env-vars OAUTH_TOKEN_STORE=firestore
# MCP_STATELESS_HTTP defaults to 1; only set it explicitly if a prior deploy set it to 0:
#   --update-env-vars MCP_STATELESS_HTTP=1
```

Re-run the `describe` command and confirm `OAUTH_TOKEN_STORE=firestore`.

## Confirming state survives a cold start

Automated proof lives in `tests/test_firestore_oauth.py`:

- `test_full_oauth_flow_round_trip` runs register → authorize → token exchange on
  one provider, then **constructs a fresh provider against the same document**
  (simulating a cold start / new revision) and asserts the access token still
  validates — i.e. no client reconnect required.
- `test_expired_access_token_cleanup_persists` /
  `test_unchanged_load_does_not_persist` confirm the read path persists only when
  expiry cleanup actually mutates state.

Manual check after enabling Firestore: connect the connector, force a new
revision (redeploy or `gcloud run services update ... --no-traffic` then promote,
or simply wait for a cold start), and confirm a tool call still succeeds
**without** disconnecting/reconnecting.

## Token refresh is handled server-side

`FirestoreOAuthProvider.exchange_refresh_token` persists the rotated tokens, and
`load_refresh_token` restores them on a cold start. So once `OAUTH_TOKEN_STORE=
firestore` is set, an expired access token is refreshed transparently using the
persisted refresh token — it does **not** surface to claude.ai as an approval
prompt.
