# SPDX-FileCopyrightText: 2026 René Widera
# SPDX-License-Identifier: ISC
#
# Illustrative examples.

# Examples

Everything in this directory is an **illustration** of the documented protocol,
not a dependency or a supported product surface. All data is synthetic.

## openapi_client.py

A dependency-light walkthrough of the raw HTTP handshake (standard library
only) against a locally running gateway:

1. `GET /v1/health` (no auth).
2. `POST /v1/bridge/register` (no auth) returns a session token and actor
   handle. The token is a secret and stays in the client process.
3. `POST /v1/tools/search` with `Authorization: Bearer <token>` and the actor
   handle makes one bounded tool call.
4. `POST /v1/bridge/close` releases the session.

Run it with a local gateway up:

```bash
python examples/openapi_client.py                # default http://127.0.0.1:2230
python examples/openapi_client.py http://127.0.0.1:9999   # custom port
```

For normal agent use you do not write this handshake yourself: the
`knowlageMCP-mcp` stdio bridge performs registration internally and keeps the
token out of model-visible tool results. This example exists to show what the
bridge does under the hood and to give a minimal, auditable reference for the
HTTP contract documented in [docs/protocol.md](../docs/protocol.md).
