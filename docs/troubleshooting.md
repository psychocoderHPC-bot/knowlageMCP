<!--
SPDX-FileCopyrightText: 2026 René Widera
SPDX-License-Identifier: ISC
-->

# Troubleshooting

First step for almost every problem: `knowlageMCP-gatewayctl doctor` on the
host. It checks config validity, permissions, SQLite/FTS5, migration version,
free space, the listener, the admin socket mode and path, optional backend
readiness, and host/container connection hints, without leaking secrets.

| Symptom | Diagnosis | Fix |
|---|---|---|
| Port 2230 already in use | Another process (or a second gateway) holds the port. | `doctor` and `ss -ltnp` show the owner. Stop the other process, or run `knowlageMCP-gateway --bootstrap` with a different `--port` and update the agent bridge `--port` to match. The bootstrap never silently picks another port. |
| Admin socket permission denied | The calling user is not the gateway user, or the socket directory is not 0700 / socket not 0600. | Run `gatewayctl` as the gateway user. `doctor` reports the socket path and mode. The socket is in a private runtime subdirectory; do not copy it elsewhere. |
| Container cannot reach the host gateway | Loopback binding is not automatically container-reachable, or the terok shield does not allow the route. | Add `shield: allow: ["localhost:2230"]` to the terok project config. Existing running tasks may need recreation. Verify the actual terok host-service forwarding for your runtime; if the default `host.containers.internal` name does not resolve, pass `--host`/`--url` to the bridge. |
| Host or Origin rejected (403 at connect) | The request `Host` header is not in `server.allowed_hosts`, or a browser `Origin` reached the server. | Add the exact host name the container uses (for example the published IP name) to `server.allowed_hosts`. Browser callers need an explicit `allowed_origins` entry; keep it empty for API-only use. |
| `RATE_LIMITED` on registration or requests | Per-source or global registration limits, or per-session request limits exceeded. | Honor `retry_after_s` in the error. Raise `limits.registration_*` or `limits.requests_per_minute` only if the workload genuinely needs it. |
| `REVIEW_INELIGIBLE` on `submit_review` | Reviewer family restriction (own/family revision), missing or expired lease, lost grant, or wrong revision. | Do not retry. Have an eligible reviewer from a different family, or the operator, act on the entry. Re-lease with `review_next` if the lease expired. |
| `backup restore` refused: destination not empty | Restore never overwrites an existing directory. | Choose an empty directory with `--destination`, or remove the target's contents deliberately. |
| `STORAGE_FULL` on evidence upload | Evidence quota (`storage.evidence_quota_bytes`) exhausted, or one object exceeds `limits.evidence_object_bytes`. | Delete unreferenced evidence or raise the quota. Split oversized artifacts; a single object is capped at 256 KiB by default. |
| Schema too new | A newer gateway wrote the database; this gateway refuses to run against a schema it does not understand. | Start the newer gateway, or restore a backup taken by this version (`backup verify` then `backup restore --destination`). Never hand-edit the database. |
| Disk full or read-only database | No space left, or the filesystem is mounted read-only. | `doctor` reports free space. Free space or fix the mount; SQLite WAL mode requires a writable file. A second gateway on the same database fails on the lock file, not on disk. |
| Stale leases after a gateway restart | Restarts invalidate all leases; jobs that were leased at shutdown are available again. | No action needed: the items are reassignable and were not marked successful. Mentors re-lease with `review_next`. |
| Mentor never approved | The request expired (default 10 minutes), or the operator has not decided. | Run `knowlageMCP-gatewayctl enrollments` while the request is fresh, then `approve REQUEST --role mentor --ttl 2h --namespace shared`. Expired requests cannot be approved; the agent requests again. `watch` shows new requests live. |
| Exit code 3 from gatewayctl | The gateway or its admin socket is not running. | Start the gateway (`systemctl --user start knowlage-mcp-gateway`), then `doctor`. |
| Exit code 4 from gatewayctl | Conflict or invalid state: stale `expected_revision`, a failed `config check`, or an already-decided enrollment. | Re-read the current state and retry with the fresh value, or fix the config problem `config check` listed. |
 | Agent gets `AUTH_REQUIRED` on a shared bridge | More than one actor is active on the connection and the call omitted `actor_handle`. | Pass the actor handle returned by `register_agent` to every tool call. |
