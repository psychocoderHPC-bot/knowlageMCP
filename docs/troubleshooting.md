<!--
SPDX-FileCopyrightText: 2026 René Widera
SPDX-License-Identifier: ISC
-->

# Troubleshooting

First step for almost every problem: `knowlageMCP-gatewayctl doctor` on the
host. It checks config validity, permissions, SQLite/FTS5, migration version,
free space, the listener, the admin socket mode and path, and
host/container connection hints, without leaking secrets. Integration adapter
state is reported by `knowlageMCP-gatewayctl integrations status` and
`integrations probe`.

| Symptom | Diagnosis | Fix |
|---|---|---|
| Port 2230 already in use | Another process (or a second gateway) holds the port. | `doctor` and `ss -ltnp` show the owner. Stop the other process, or run `knowlageMCP-gateway --bootstrap` with a different `--port` and update the agent bridge `--port` to match. The bootstrap never silently picks another port. |
| Admin socket permission denied | The calling user is not the gateway user, or the socket directory is not 0700 / socket not 0600. | Run `gatewayctl` as the gateway user. `doctor` reports the socket path and mode. The socket is in a private runtime subdirectory; do not copy it elsewhere. |
| Container cannot reach the host gateway | Loopback binding is not automatically container-reachable, or the terok shield does not allow the route. | Add `shield: allow: ["localhost:2230"]` to the terok project config. Existing running tasks may need recreation. Verify the actual terok host-service forwarding for your runtime; if the default `host.containers.internal` name does not resolve, pass `--host`/`--url` to the bridge. |
| Host or Origin rejected (403 at connect) | The request `Host` header is not in `server.allowed_hosts`, or a browser `Origin` reached the server. | Add the exact host name the container uses (for example the published IP name) to `server.allowed_hosts`. Browser callers need an explicit `allowed_origins` entry; keep it empty for API-only use. |
| `RATE_LIMITED` on registration or requests | Per-source or global registration limits, or per-session or global request limits exceeded. | Honor `retry_after_s` in the error. Raise `limits.registration_*`, `limits.requests_per_minute`, or `limits.requests_per_minute_global` only if the workload genuinely needs it. |
| `REVIEW_INELIGIBLE` on `submit_review` | Reviewer family restriction (own/family revision), missing or expired lease, lost grant, or wrong revision. | Do not retry. Have an eligible reviewer from a different family, or the operator, act on the entry. Re-lease with `review_next` if the lease expired. |
| `backup restore` refused: destination not empty | Restore never overwrites an existing directory. | Choose an empty directory with `--destination`, or remove the target's contents deliberately. |
| After a backup restore, hybrid search returns only lexical results or `semantic index unavailable` | The Qdrant vector index is derived and not in the backup snapshot, so the restored database has no matching ready generation. Stale points are never trusted; hits are re-checked against SQLite. | Run `knowlageMCP-gatewayctl integrations reindex` to rebuild the derived index for the restored database, then check `integrations status` for a `ready` generation. |
| `STORAGE_FULL` on evidence upload | Evidence quota (`storage.evidence_quota_bytes`) exhausted, or one object exceeds `limits.evidence_object_bytes`. | Delete unreferenced evidence or raise the quota. Split oversized artifacts; a single object is capped at 256 KiB by default. |
| Schema too new | A newer gateway wrote the database; this gateway refuses to run against a schema it does not understand. | Start the newer gateway, or restore a backup taken by this version (`backup verify` then `backup restore --destination`). Never hand-edit the database. |
| Disk full or read-only database | No space left, or the filesystem is mounted read-only. | `doctor` reports free space. Free space or fix the mount; SQLite WAL mode requires a writable file. A second gateway on the same database fails on the lock file, not on disk. |
| Stale leases after a gateway restart | Restarts invalidate all leases; jobs that were leased at shutdown are available again. | No action needed: the items are reassignable and were not marked successful. Mentors re-lease with `review_next`. |
| Mentor never approved | The request expired (default 10 minutes), or the operator has not decided. | Run `knowlageMCP-gatewayctl enrollments` while the request is fresh, then `approve REQUEST --role mentor --ttl 2h --namespace shared`. Expired requests cannot be approved; the agent requests again. `watch` shows new requests live. |
| Exit code 3 from gatewayctl | The gateway or its admin socket is not running. | Start the gateway (`systemctl --user start knowlage-mcp-gateway`), then `doctor`. |
| Exit code 4 from gatewayctl | Conflict or invalid state: stale `expected_revision`, a failed `config check`, or an already-decided enrollment. | Re-read the current state and retry with the fresh value, or fix the config problem `config check` listed. |
| Agent gets `AUTH_REQUIRED` on a shared bridge | More than one actor is active on the connection and the call omitted `actor_handle`. | Pass the actor handle returned by `register_agent` to every tool call. |
| Integrations enabled but search reports no semantic results | `integrations status` shows no `ready` generation, so the vector index is unavailable and search stays lexical (fail-open). | Run `knowlageMCP-gatewayctl integrations reindex`, then check `integrations status` for a `ready` generation. |
| Search results carry a `semantic backend unavailable` warning | The embedding or vector backend is unreachable, unconfigured, or returned a dimension mismatch. The lexical result is still valid. | `knowlageMCP-gatewayctl integrations probe` names the failing role. Check the backend URL, `vector_size`/`dimensions` agreement, and API key environment variable. |
| Personal namespace content is not found semantically | `integrations.embedding.allow_private_text` is false, so personal/private text was never indexed for a remote provider. | Set `allow_private_text = true` if exporting that text to the configured endpoint is acceptable, then `integrations reindex`. With false, that content is reachable only through lexical search. |
| Semantic search stopped after changing the embedding model or dimensions | A model or dimension change starts a new generation; the old one is marked `stale` and vectors never mix. | Run `knowlageMCP-gatewayctl integrations reindex` to build the new generation. |
| Qdrant collection missing or index unavailable after a restart | Restart rehydration adopts the last ready generation only when its model and dimensions still match the configured embedding adapter; the collection may also be absent from Qdrant. | Run `integrations reindex` to recreate the collection and rebuild. `integrations status` lists recorded generations and their state. |
| Remote embedding endpoint returns 401/403 | The API key environment variable named by `integrations.embedding.api_key_env` is unset, empty, or wrong. The key value is never stored in the config. | Set the variable in the gateway's environment (for example `KNOWLAGEMCP_EMBEDDING_API_KEY`), restart the gateway, and `integrations probe`. |
| Reranker provider down or returning garbage | The reranker fails open: the merged hybrid result is returned without reranking, with a `semantic backend unavailable` warning. | No action required for correctness. `integrations probe` shows reranker availability; fix the endpoint or set `provider = "lexical"`. |
| Graphiti/Mem0 extraction produced nothing visible | Proposals are stored with state `proposed` and are never published; nothing appears in search until they go through the normal candidate/review path. | List them with `knowlageMCP-gatewayctl integrations proposals [--entry ENTRY]`. Acceptance is manual via the normal review flow or `integrations import-memories` (candidate/unverified entries). |
