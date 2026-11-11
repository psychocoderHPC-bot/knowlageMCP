# SPDX-FileCopyrightText: 2026 René Widera
# SPDX-License-Identifier: ISC
"""Illustration of the documented knowlageMCP HTTP handshake.

This is an illustration, not a dependency. It uses only the standard library
and shows, against a locally running gateway:

1. GET  /v1/health             (no auth)
2. POST /v1/bridge/register    (no auth, gets a session token)
3. POST /v1/tools/search       (Bearer token, one bounded tool call)
4. POST /v1/tools/upload_evidence (bounded text-only evidence, no paths)
5. POST /v1/bridge/mentor      (request a mentor enrollment)
6. GET  /v1/bridge/enrollment/{request_id} (poll the request state)
7. POST /v1/bridge/close       (Bearer token, releases the session)

All data is synthetic. Run it with a local gateway up:

    python examples/openapi_client.py [URL]     (default http://127.0.0.1:2230)
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request

DEFAULT_URL = "http://127.0.0.1:2230"


def http_json(
    url: str,
    payload: dict | None = None,
    bearer: str | None = None,
    method: str | None = None,
) -> dict:
    """POST (or GET when payload is None) JSON and return the parsed body."""
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    http_method = method or ("POST" if data is not None else "GET")
    request = urllib.request.Request(url, data=data, method=http_method)
    request.add_header("Content-Type", "application/json")
    if bearer is not None:
        request.add_header("Authorization", f"Bearer {bearer}")
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        print(f"HTTP {exc.code} from {url}: {body}", file=sys.stderr)
        raise


def main(url: str = DEFAULT_URL) -> int:
    # 1. Health, no auth.
    health = http_json(f"{url}/v1/health")
    print(f"health: {health}")

    # 2. Register, no auth. The token is a secret: keep it in this process.
    registration = http_json(
        f"{url}/v1/bridge/register",
        {"mode": "root", "label": "openapi-example", "task": "synthetic handshake illustration"},
    )
    token = registration["session_token"]
    actor_handle = registration["actor_handle"]
    print(f"registered: agent={registration['agent_id']} actor={actor_handle}")

    try:
        # 3. One bounded tool call with the bearer token and actor handle.
        search = http_json(
            f"{url}/v1/tools/search",
            {"query": "synthetic handshake marker", "max_results": 1, "actor_handle": actor_handle},
            bearer=token,
        )
        print(
            f"search: ok={search.get('ok')} "
            f"total={search.get('data', {}).get('total') if search.get('ok') else search.get('error')}"
        )

        # 4. Bounded text-only evidence. No filesystem paths are accepted; the
        #    object is capped by limits.evidence_object_bytes, not by
        #    server.max_request_bytes.
        evidence = http_json(
            f"{url}/v1/tools/upload_evidence",
            {
                "text": "synthetic evidence object for the openapi illustration",
                "media_type": "text/plain",
                "name": "openapi_example.txt",
                "actor_handle": actor_handle,
            },
            bearer=token,
        )
        print(
            f"upload_evidence: ok={evidence.get('ok')} "
            "evidence_id="
            f"{evidence.get('data', {}).get('evidence_id') if evidence.get('ok') else evidence.get('error')}"
        )

        # 5. Request a mentor enrollment; the request_id is the approval
        #    credential, the code is for operator verification only.
        mentor = http_json(
            f"{url}/v1/tools/request_mentor",
            {"namespace": "shared", "scopes": ["shared"], "actor_handle": actor_handle},
            bearer=token,
        )
        request_id = mentor.get("data", {}).get("request_id") if mentor.get("ok") else None
        print(
            f"request_mentor: ok={mentor.get('ok')} "
            f"request_id={request_id} "
            f"state={mentor.get('data', {}).get('state') if mentor.get('ok') else mentor.get('error')}"
        )

        # 6. Poll the enrollment state for this session's own request.
        if request_id:
            enrollment = http_json(
                f"{url}/v1/tools/enrollment_status",
                {"request_id": request_id, "actor_handle": actor_handle},
                bearer=token,
            )
            state = (
                enrollment.get("data", {}).get("state") if enrollment.get("ok") else enrollment.get("error")
            )
            print(f"enrollment_status: ok={enrollment.get('ok')} state={state}")
    finally:
        # 7. Always close the session; a crashed client would leak the session
        #    until its idle timeout.
        closed = http_json(f"{url}/v1/bridge/close", {}, bearer=token)
        print(f"closed: {closed}")

    return 0


if __name__ == "__main__":
    base = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_URL
    raise SystemExit(main(base))
