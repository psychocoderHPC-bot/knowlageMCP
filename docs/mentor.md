<!--
SPDX-FileCopyrightText: 2026 René Widera
SPDX-License-Identifier: ISC
-->

# Mentor operation

The mentor is an external AI session the operator starts manually. The gateway
does not launch, bill, or manage a model. It provides nothing more than a
normal bridge connection plus a time-limited review role that the operator
grants. Without a mentor, all normal read and write work continues; reviews
simply stay queued.

## Starting a mentor

1. Start the operator's chosen AI session on any machine that can reach the
   gateway, using the same `knowlageMCP-mcp` bridge (host/port configured as
   in the agent setup) and the instruction file
   [`skills/knowlageMCP/mentor.md`](../skills/knowlageMCP/mentor.md).
2. In that session, have the mentor call `register_agent` and then
   `request_mentor` with the namespace and scopes it needs.
3. The response contains a request ID and a short human comparison code.

## Approving on the host

```bash
knowlageMCP-gatewayctl enrollments
knowlageMCP-gatewayctl approve REQUEST --role mentor --ttl 2h --namespace shared
```

`REQUEST` is the request ID. The short comparison code shown in `enrollments`
is for operator verification only: it lets you confirm that the row you are
about to approve is the one the agent reported. It is not an approval
credential; `approve` always takes the request ID. `watch` streams new
requests live, which is the comfortable way to operate this step.

Approval rules:

- The approval binds to the exact pending client session and the requested
  scopes. It never binds to a display name or an agent label.
- A request expires after `mentor_request_lifetime` (default 10 minutes).
  Expired requests cannot be approved; the agent must request again.
- Mentor rights apply only to calls made after the approval, last for the
  given TTL (default 2 hours from `mentor_approved_lifetime`), and are
  revocable at any time with `knowlageMCP-gatewayctl revoke SESSION`.

## Scopes and TTL

Scopes are the namespaces and review areas the mentor may touch. Grant the
narrowest scopes the task needs (usually one shared namespace). A short TTL is
the norm: a few hours of review work, then revoke or let it expire. Re-approval
is a fresh decision, not an extension.

## Lease etiquette

- `review_next` returns one leased item at a time with a lease
  (`review.lease_seconds`, default 600 s). The mentor takes one item, does the
  work, submits, and takes the next.
- A lease that expires (for example across a restart) re-opens the item. It is
  not marked successful and is not counted against the mentor.
- The per-session budget is `review.mentor_items_per_session` (default 20).
  After that, `review_next` reports the queue empty for that session.
- If reproduction or the needed environment is unavailable, submit
  `inconclusive` or let the lease lapse. There is no penalty for deferring.

## Evidence requirements

A `submit_review` needs, for the exact leased revision:

- `result`: `supports`, `contradicts`, `inconclusive`, `duplicate`, or
  `out_of_scope`;
- `method`: how the check was done (for example a test command or a source
  reference);
- `evidence`: the observed result, short and bounded;
- optionally `conditions` and `limitations`.

`supports` or `contradicts` on an eligible revision is what can move the
lifecycle (publication or dispute). `inconclusive` keeps the entry a candidate
and stores the reason.

## No self-approval

A mentor may not independently approve a revision it authored or modified, nor
one to which it contributed. Eligibility is re-checked at submission, including
the author/contributor family restriction. A mentor-generated rewrite becomes
a candidate revision for a different eligible reviewer or the operator.
Operator overrides are explicit and auditable, and are not presented as
independent review.

## Budget

- One lease at a time; 20 items per session by default; 2 hour TTL by default.
- The gateway does not pay for tokens. The operator's model budget is an
  external constraint the operator manages.

## Revocation

```bash
knowlageMCP-gatewayctl revoke SESSION
```

Revocation stops review eligibility immediately. Completed reviews survive
revocation and mentor expiry.

## What happens without a mentor

- Agents record candidates, search, correct, and report outcomes normally.
- Reviews accumulate in the queue with priority (unresolved contradictions
  first, then demonstrated use, weak evidence in frequently used entries, and
  age with starvation prevention).
- The operator can always publish, retract, or forget directly via
  `gatewayctl memory` commands; that path is operator action, not independent
  review.
