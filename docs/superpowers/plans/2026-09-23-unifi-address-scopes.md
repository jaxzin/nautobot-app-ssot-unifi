# UniFi address scopes implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Steps use checkbox syntax.

**Goal:** Import controller-reported addresses into explicit native routing namespaces without ambiguous native lookups or changing unrelated inventory.

**Architecture:** Native Job inputs select existing Namespaces. A small immutable prefix resolver selects the namespace before source models are built. Native SSoT and DiffSync retain collection, diff, write and history responsibilities. Primary IP changes are applied only after interface assignments exist.

**Tech Stack:** Python 3.12, Nautobot 3.2.4, SSoT 4.6.1, DiffSync 2, uv.

**Spec:** The accepted contract and constraints below are the self-contained specification. The operator delegated recommended decisions and asked for the alternatives to be recorded.

## Global Constraints

- Optional native Job `namespace` selects an existing Namespace; omitted means existing Global. Do not create namespaces in the connector.
- Optional native Job JSON input `address_scopes` is null/omitted or a list of objects containing exactly `prefixes` (nonempty canonical IPv4/IPv6 CIDR strings) and `namespace_id` (UUID). Resolve all UUIDs to existing namespaces before collection. Empty list admits no addresses. Unknown keys/malformed types are rejected safely.
- Without rules, use `namespace`. With rules, select the unique namespace matching the address host. Overlapping rules selecting the same namespace are valid; distinct-namespace matches or no match fail source collection before writes. No fallback when rules are present.
- Recommendation accepted under operator delegation: fail the native Job before any writes for unmatched/ambiguous source mapping. Alternative is custom partial-import/held-dependent-object handling; not selected because source collection is already a native SSoT phase and inventory must not lose primary references. Native retained-destination behavior stays enabled.
- Namespace participates in Prefix, IPAddress, assignment, and primary-IP identity/lookups. IP masks are attributes, not identity. Keep the existing uncommitted namespace groundwork, which belongs to this task; do not recreate it.
- Preserve first-owner-wins: never adopt untagged/foreign records. Preserve read-only UniFi transport and existing TLS choice. Do not delete inventory, move namespaces, provision secrets, access live source systems, or change deployment.
- Source facts cannot be overridden locally. Existing and new devices select primary IPs only after actual assignments exist; failure must remain visible through native Job failure. Preserve explicit clearing and IPv4/IPv6 independence.
- Reusable code/docs/examples must contain no homelab addresses, hostnames, credentials or local paths. Plan/test execution details are not operator reference content.
- Use apply_patch, uv, focused TDD. Only one disposable local native suite at a time. No broad repeated suites, no subagents, no push/merge by implementer. Commit all this task's existing/new source changes and plan with Codex coauthor.

## Task 1: Native namespace selection and safe primary updates

**Files:** modify `nautobot_ssot_unifi/jobs.py`, `ssot/adapters.py`, `ssot/models.py`, existing namespace/primary/transport tests and `ci/check_runtime.py`; create focused `nautobot_ssot_unifi/ssot/address_scopes.py` and tests; update `docs/user/app_getting_started.md`. Add focused native regression tests/config in the existing development/test layout as needed; no live CI credentials.

**Interfaces:** `UnifiDataSource.run(..., namespace=None, address_scopes=None, **kwargs)` consumes the native Job JSON list and optional Namespace. Resolver internally maps host strings to verified Namespace name/UUID; internal helper names are implementer-owned. Downstream native IDs use namespace-qualified model fields. No separate importer or custom persistent ledger.

- [x] Read the existing task changes and controller-provided checkpoint. Base is `3fa515605e1e20926e64fedb95a98a71957be6e4` on dedicated `codex/namespace-aware-unifi`; remote maintained base is `codex/persistent-interfaces`, not upstream main/develop. Baseline31 runtime tests and one real native duplicate-IP/parent-update check passed before this extension.
- [x] Add failing parser/resolution and source integration tests, using UUID `00000000-0000-4000-8000-000000000004` for one namespace and `...0005` for another. Example rules: one maps `192.0.2.0/24` to the first; one maps `198.51.100.0/24` to the second. Assert selected prefix/IP/assignment identities differ, unmatched `203.0.113.1` rejects without a default, conflicting maps reject, same-namespace overlap works, omitted rules preserve selected namespace, empty rules admit none, invalid/missing namespace references reject before source reads.
- [x] Run focused tests and record expected RED before implementing new routing. Add minimum parser/resolver/native Job wiring, using existing Namespace objects and sanitizing configuration errors; no raw integration URL or credentials in new errors.
- [x] Integrate resolution into `_assign_ip` so all native identifiers share the selected namespace. Carry namespace in both primary-IP family attributes and defer create/update primary changes to native adapter completion, after links exist. Include the existing-device ordering issue tracked by controller as deployment issue101; a namespace-aware primary implementation must not retain that known ordering defect.
- [x] Write and run a focused native RED/GREEN regression: existing device changes to a newly imported primary in namespace A while the same host exists in namespace B; correct address assigned before primary selection, stable IDs on repeat, IPv4/IPv6/clearing correct. When assignment creation fails, primary must not become an unassigned/foreign IP. Keep native validation rather than bypassing it.
- [x] Reuse the prepared disposable `unifi_namespace_check` database with the controller-provided local settings. Promote the scoped native parent check into durable repo tests. `ci/check_native.py test` requires an explicit test configuration, restricts access to the loopback disposable database, and rolls fixtures back without migrations. Never touch production or another integration's test database.
- [x] Document native Job inputs, one complete synthetic two-namespace example, no-rule behavior, fail-before-write mapping errors, and how to repair rules and rerun. Record recommended decision versus alternative in an architecture decision note if needed, not development-history prose in reference docs.
- [x] Run full fast runtime suite, focused native regressions, Ruff and whitespace check; inspect diff. Commit, append full evidence and any remaining concerns to the report, return concise status/SHA. Do not push or modify the deployment checkout.

Task-1 verification: 39 fast tests and 6 native tests pass. Native tests cover real scoped parent updates, deferred device create/update primaries, independent family clearing, repeated stable IDs, assignment validation failure, and native Job pre-write mapping refusal/Dryrun. Accepted decision: native whole-source failure on invalid/unmatched/ambiguous mapping; no custom partial importer. Internal decisions: retain empty JSON collections through native Job validation, reject IPv6 zone suffixes in canonical CIDRs, and use per-family scoped keys in the existing completion queue. Controller owns delivery and migration.

## Controller follow-through

Independent task/final review, GitHub PR against maintained deployed base, CI, merge, then deployment pin/image via Gitea CI. Live namespace prerequisites and identity-preserving migration are owned by the deployment repository and must be verified there. Connector runtime support alone is not a completed live migration.
