<!--
SPDX-FileCopyrightText: 2026 René Widera
SPDX-License-Identifier: ISC
-->

# Security checklist

This checklist maps the trust-model claims in `SECURITY.md` to the code that
enforces each guarantee and the regression test that pins it. "Enforcement" is
the production code path that rejects a violating request; "Regression test" is
the test that fails if that enforcement is removed or weakened.

Run the whole set with `scripts/check.sh`. Each row lists the test module and
the test functions that exercise that guarantee.

## 1. Admin surface

| Guarantee | Enforcement | Regression test |
|---|---|---|
| Admin socket is created with mode 0600 | `src/knowlage_mcp/admin.py` `AdminServer.start` | `tests/integration/test_admin.py::test_admin_socket_permissions_and_status` |
| Admin socket rejects a peer whose uid differs from the service owner | `src/knowlage_mcp/admin.py` `peer_uid`, `peer_uid_allowed` | `tests/integration/test_admin.py::test_peer_uid_check_when_available` |
| Admin socket refuses a symlinked or pre-existing regular-file path | `src/knowlage_mcp/admin.py` `AdminServer.start` | `tests/security/test_transport_security.py::test_admin_socket_refuses_symlink_live_socket_and_regular_file` |
| A second server cannot bind the same admin socket | `src/knowlage_mcp/admin.py` `AdminServer.start` | `tests/integration/test_admin.py::test_second_server_on_same_socket_is_refused` |
| Admin paths and commands are not reachable over the agent TCP surface | `src/knowlage_mcp/gateway.py` (no admin routes) | `tests/security/test_agent_boundaries.py::test_admin_paths_and_commands_are_not_served_over_tcp` |

## 2. Identity and sessions

| Guarantee | Enforcement | Regression test |
|---|---|---|
| Registration ignores or rejects forged `agent_id`/`session_id`/`root_agent_id` | `src/knowlage_mcp/identity.py` `register` | `tests/security/test_identity_forgery.py::test_http_registration_ignores_or_rejects_forged_ids`, `tests/security/test_identity_forgery.py::test_mcp_register_agent_does_not_accept_forged_ids` |
| Child registration cannot choose its own root | `src/knowlage_mcp/identity.py` `register` | `tests/security/test_identity_forgery.py::test_child_registration_cannot_choose_root` |
| Tool schemas reject forged identity fields in arguments | `src/knowlage_mcp/gateway.py` tool argument validation | `tests/security/test_identity_forgery.py::test_tool_schema_rejects_forged_identity_fields` |
| An actor handle from another session is rejected | `src/knowlage_mcp/identity.py` `authenticate` | `tests/security/test_identity_forgery.py::test_actor_handle_of_another_session_is_rejected` |
| A multiplexed bridge with no actor fails closed | `src/knowlage_mcp/bridge.py` | `tests/security/test_identity_forgery.py::test_multiplexed_bridge_without_actor_fails_closed` |
| Role fields in payloads cannot self-grant authority | `src/knowlage_mcp/identity.py` `approve_mentor` | `tests/security/test_mentor_and_review.py::test_role_fields_in_payloads_cannot_self_grant` |
| Revoking a parent cascades to descendants | `src/knowlage_mcp/identity.py` `revoke` | `tests/unit/test_identity.py::test_revoking_parent_cascades_to_descendants` |
| Idle and absolute session expiry are enforced | `src/knowlage_mcp/identity.py` `authenticate` | `tests/unit/test_identity.py::test_idle_and_absolute_session_expiry`, `tests/security/test_sessions.py::test_idle_timeout_expires_a_session`, `tests/security/test_sessions.py::test_absolute_lifetime_expires_a_session` |
| Session close and revoke are not replayable | `src/knowlage_mcp/identity.py` `close_session`, `revoke` | `tests/security/test_sessions.py::test_session_close_replay_is_rejected`, `tests/security/test_sessions.py::test_gatewayctl_revoke_replay_is_rejected` |
| Bootstrap revokes live sessions and is idempotent | `src/knowlage_mcp/identity.py` `bootstrap` | `tests/unit/test_identity.py::test_bootstrap_is_idempotent_and_revokes_live_sessions` |

## 3. Namespace ACLs and retrieval

| Guarantee | Enforcement | Regression test |
|---|---|---|
| The personal namespace is not readable without a privileged explicit grant | `src/knowlage_mcp/policy.py` `readable_namespaces`, `require_read` | `tests/unit/test_policy.py::test_personal_namespace_requires_privileged_explicit_grant`, `tests/unit/test_retrieval.py::test_personal_namespace_is_not_readable_without_privilege` |
| A shared-only agent sees no foreign content | `src/knowlage_mcp/retrieval.py` search ACL filter | `tests/security/test_namespace_isolation.py::test_shared_only_agent_sees_no_foreign_content` |
| A semantic hit payload namespace is not trusted for ACL; it is re-checked | `src/knowlage_mcp/retrieval.py` hybrid merge | `tests/unit/test_semantic_retrieval.py::test_payload_namespace_is_not_trusted_for_acl` |
| Archived and superseded hits are dropped from results | `src/knowlage_mcp/retrieval.py` lifecycle filter | `tests/unit/test_semantic_retrieval.py::test_archived_and_superseded_hits_are_dropped` |
| Candidates are labelled low-trust and can be excluded | `src/knowlage_mcp/retrieval.py` candidate filter | `tests/unit/test_retrieval.py::test_candidates_are_labelled_low_trust`, `tests/unit/test_semantic_retrieval.py::test_candidate_is_low_trust_and_excluded_when_configured` |
| A reranker reorders but cannot add an entry | `src/knowlage_mcp/retrieval.py` rerank | `tests/unit/test_semantic_retrieval.py::test_reranker_reorders_but_cannot_add_an_entry` |
| Retract excludes an entry from retrieval immediately | `src/knowlage_mcp/knowledge.py` `memory_retract` | `tests/unit/test_knowledge.py::test_retract_excludes_entry_immediately` |

## 4. Evidence

| Guarantee | Enforcement | Regression test |
|---|---|---|
| Traversal and absolute evidence IDs are rejected | `src/knowlage_mcp/evidence.py` `EvidenceStore.get` | `tests/security/test_evidence_paths.py::test_traversal_and_absolute_evidence_ids_are_rejected` |
| A symlink inside the evidence root is rejected | `src/knowlage_mcp/evidence.py` `EvidenceStore.get` | `tests/security/test_evidence_paths.py::test_symlink_inside_evidence_root_is_rejected` |
| Uploaded text is redacted for credential patterns | `src/knowlage_mcp/evidence.py` `redact_text`, `scan_for_secrets` | `tests/unit/test_evidence.py::test_secret_redaction_on_put`, `tests/unit/test_evidence.py::test_redact_text_is_idempotent` |
| Evidence size and quota are enforced | `src/knowlage_mcp/evidence.py` `EvidenceStore.put` | `tests/unit/test_evidence.py::test_size_cap`, `tests/unit/test_evidence.py::test_quota_enforced` |
| Unreferenced evidence deletion respects the deletion ledger | `src/knowlage_mcp/evidence.py` `EvidenceStore.delete_unreferenced` | `tests/unit/test_evidence.py::test_delete_unreferenced_respects_ledger` |
| Evidence uses its own byte budget, not the generic JSON limit | `src/knowlage_mcp/gateway.py` evidence route | `tests/security/test_transport_security.py::test_evidence_uses_its_own_byte_budget`, `tests/integration/test_gateway_http.py::test_evidence_cap_is_object_limit_not_generic_json_limit` |

## 5. Secrets

| Guarantee | Enforcement | Regression test |
|---|---|---|
| Only the SHA-256 hash of a session token is stored | `src/knowlage_mcp/identity.py` `register`, `authenticate` | `tests/security/test_secrets.py::test_tokens_never_appear_in_observable_outputs` |
| Tokens never appear in observable HTTP or MCP output | `src/knowlage_mcp/gateway.py`, `src/knowlage_mcp/bridge.py` | `tests/security/test_secrets.py::test_tokens_never_appear_in_observable_outputs`, `tests/integration/test_gateway_http.py::test_health_reveals_no_secrets` |
| API key values are never stored in config | `src/knowlage_mcp/config.py` `redact` | `tests/unit/test_config.py::test_api_key_values_are_never_stored`, `tests/unit/test_config.py::test_redaction_hides_url_credentials_and_keeps_env_names` |
| Config redaction masks credential keys | `src/knowlage_mcp/config.py` `redact` | `tests/unit/test_config.py::test_redaction_masks_credential_keys` |
| Integration API keys are read from the environment, never echoed | `src/knowlage_mcp/integrations/` adapters | `tests/unit/test_graphiti.py::test_api_key_is_read_from_environment_and_never_leaked`, `tests/unit/test_mem0.py::test_api_key_is_read_from_environment_and_never_leaked`, `tests/unit/test_rerank.py::test_openai_reranker_key_header_and_no_leak`, `tests/unit/test_embeddings.py::test_api_key_never_appears_in_errors` |

## 6. Rate limiting

| Guarantee | Enforcement | Regression test |
|---|---|---|
| Registration is limited per source and globally | `src/knowlage_mcp/identity.py` `register` limiter | `tests/unit/test_identity.py::test_registration_source_rate_limit`, `tests/unit/test_identity.py::test_registration_global_rate_limit` |
| Per-session and global request buckets are enforced | `src/knowlage_mcp/policy.py` `TokenBucketLimiter`, `SlidingWindowLimiter` | `tests/unit/test_identity.py::test_global_request_rate_limit_spans_sessions`, `tests/integration/test_concurrency.py::test_request_rate_limit_rejects_excess_with_retry_after` |
| Candidate writes are limited per agent and globally | `src/knowlage_mcp/knowledge.py` `record` limiter | `tests/unit/test_knowledge.py::test_candidate_quota_per_family`, `tests/unit/test_knowledge.py::test_global_candidate_quota` |
| The sliding-window limiter advances correctly | `src/knowlage_mcp/policy.py` `SlidingWindowLimiter` | `tests/unit/test_policy.py::test_sliding_window_limiter_with_fake_clock` |

## 7. Forgetting and the deletion ledger

| Guarantee | Enforcement | Regression test |
|---|---|---|
| `forget` purges active content and writes a ledger row | `src/knowlage_mcp/knowledge.py` `memory_forget` | `tests/unit/test_knowledge.py::test_forget_dry_run_purges_and_writes_ledger` |
| Forget survives index bookkeeping failure | `src/knowlage_mcp/knowledge.py` `memory_forget` | `tests/unit/test_semantic_retrieval.py::test_forget_survives_index_bookkeeping_failure` |
| Restore re-applies the deletion ledger so forgotten stays forgotten | `src/knowlage_mcp/backup.py` `Backup.restore` | `tests/unit/test_service.py::test_backup_verify_restore_and_forgotten_stays_forgotten` |

## 8. Import trust downgrade

| Guarantee | Enforcement | Regression test |
|---|---|---|
| Imported revisions are forced to `candidate`/`unverified` | `src/knowlage_mcp/service.py` `import_jsonl` | `tests/unit/test_service.py::test_jsonl_import_reports_downgraded_trust` |
| Path-traversal revisions in an import are skipped as malformed | `src/knowlage_mcp/service.py` `import_jsonl` | `tests/unit/test_service.py::test_jsonl_import_skips_path_traversal_revision_and_commits` |

## 9. Transport and protocol

| Guarantee | Enforcement | Regression test |
|---|---|---|
| Foreign `Host` and `Origin` headers are rejected on the HTTP and MCP surfaces | `src/knowlage_mcp/gateway.py` `HostOriginMiddleware` | `tests/security/test_transport_security.py::test_host_and_origin_rejected_on_v1_and_mcp`, `tests/integration/test_gateway_http.py::test_host_and_origin_rejection`, `tests/security/test_transport_security.py::test_allowed_host_succeeds_on_mcp` |
| `doctor` warns when the listener bind address is not loopback | `src/knowlage_mcp/service.py` `KnowledgeService.doctor` (`listener_exposure`) | `tests/unit/test_service.py::test_doctor_reports_listener_exposure` |
| Incompatible or malformed protocol headers are rejected | `src/knowlage_mcp/protocol.py`, `src/knowlage_mcp/gateway.py` `require_protocol_header` | `tests/integration/test_gateway_http.py::test_incompatible_protocol_major_is_rejected`, `tests/integration/test_gateway_http.py::test_malformed_protocol_header_is_rejected`, `tests/unit/test_protocol.py::test_version_compatibility_matrix` |
| An absent protocol header is accepted | `src/knowlage_mcp/gateway.py` `require_protocol_header` | `tests/integration/test_gateway_http.py::test_absent_protocol_header_is_accepted` |
| Oversized request bodies are rejected | `src/knowlage_mcp/gateway.py` request size check | `tests/integration/test_gateway_http.py::test_oversized_body_is_rejected`, `tests/security/test_transport_security.py::test_json_body_over_generic_limit_is_rejected` |
| Data routes require a bearer token over HTTP and MCP | `src/knowlage_mcp/gateway.py` bearer dependency | `tests/security/test_unauth_access.py::test_http_data_routes_require_bearer`, `tests/security/test_unauth_access.py::test_mcp_data_tools_require_bearer`, `tests/integration/test_gateway_http.py::test_bearer_required_and_wrong_token`, `tests/integration/test_gateway_http.py::test_revoked_token_is_rejected` |

## 10. Stored content as data, not instructions

| Guarantee | Enforcement | Regression test |
|---|---|---|
| Stored instructions are inert data; no shell or network primitive is invoked | `src/knowlage_mcp/retrieval.py` (no execution) | `tests/security/test_agent_boundaries.py::test_stored_instructions_are_inert_data` |
| Path and file keys in tool arguments are rejected | `src/knowlage_mcp/gateway.py` tool argument validation | `tests/security/test_agent_boundaries.py::test_path_and_file_keys_are_rejected` |
| FTS injection is inert and the query builder emits no raw syntax | `src/knowlage_mcp/retrieval.py` FTS query builder | `tests/security/test_injection_unicode.py::test_fts_injection_is_inert`, `tests/security/test_injection_unicode.py::test_fts_query_builder_never_emits_raw_syntax`, `tests/security/test_injection_unicode.py::test_hostile_unicode_and_bounds_are_structured` |

## 11. Review and mentor boundaries

| Guarantee | Enforcement | Regression test |
|---|---|---|
| A reviewer cannot publish and admin commands are unreachable from it | `src/knowlage_mcp/review.py`, `src/knowlage_mcp/gateway.py` | `tests/security/test_mentor_and_review.py::test_reviewer_cannot_publish_and_admin_commands_unreachable` |
| A foreign session cannot poll or approve a mentor request | `src/knowlage_mcp/identity.py` `approve_mentor` | `tests/security/test_mentor_and_review.py::test_foreign_session_cannot_poll_or_approve_request` |
| An expired mentor request cannot be approved | `src/knowlage_mcp/identity.py` `approve_mentor` | `tests/security/test_mentor_and_review.py::test_expired_mentor_request_cannot_be_approved`, `tests/unit/test_identity.py::test_expired_request_cannot_be_approved` |
| A mentor cannot review its own contributed revision | `src/knowlage_mcp/review.py` | `tests/security/test_mentor_and_review.py::test_mentor_cannot_review_own_contributed_revision` |
| Peer review adds evidence but cannot publish | `src/knowlage_mcp/review.py` `submit_review` | `tests/unit/test_review.py::test_peer_review_adds_evidence_but_cannot_publish` |

## 12. Durability, restart, and single writer

| Guarantee | Enforcement | Regression test |
|---|---|---|
| A restart is durable and invalidates live state while keeping data | `src/knowlage_mcp/service.py` start | `tests/integration/test_restart.py::test_restart_is_durable_and_invalidates_live_state` |
| A second gateway process refuses the database lock | `src/knowlage_mcp/db.py` lock | `tests/security/test_faults.py::test_second_gateway_process_refuses_the_database_lock`, `tests/unit/test_db.py::test_lock_file_rejected_in_second_process` |
| Audit events persist and are streamable after restart | `src/knowlage_mcp/policy.py` `audit` | `tests/integration/test_admin.py::test_watch_receives_audit_event`, `tests/unit/test_service.py::test_event_bus_publishes_registration_events` |
| SIGTERM exits zero and removes the socket | `src/knowlage_mcp/admin.py` `AdminServer.stop` | `tests/security/test_faults.py::test_sigterm_exits_zero_and_removes_the_socket` |

## Operator exposure checklist

When exposing the gateway beyond loopback, follow the steps in
[operations.md](operations.md#exposing-the-gateway-beyond-loopback). Every
non-loopback binding must keep `allowed_hosts` and `allowed_origins` explicit
and must not widen the admin surface, which stays on the Unix socket only.
