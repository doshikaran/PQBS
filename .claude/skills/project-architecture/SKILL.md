# Skill: PQBS Project Architecture

Use this skill when you need to understand what PQBS is, what problem it solves, how the system is structured, and how the three major paths work.

---

## What PQBS Is

PQBS (Poison-Quarantine Belief Store) is a shared memory layer for multi-agent systems. It treats agent memory as a security-critical asset where:

- Every belief is **bitemporal** (world-time and transaction-time), never destructively overwritten
- Every contradiction is resolved **deterministically** under serializable isolation
- Every write is **screened** by an asynchronous integrity gate before it can influence retrieval
- Every state transition is **attributable** to a specific agent identity and **immutably recorded**

**The thesis:** memory integrity is a database problem, not a prompt problem. Enforcement lives in transaction semantics and index-level visibility, not in application code a compromised client can bypass.

---

## The Problem

**Temporal decoupling:** a poisoned memory written today fires weeks later in a different session, triggered by a semantically-near query. Session-boundary defenses cannot see it.

**Concurrency compounds it:** under READ COMMITTED, two agents can each read a belief, each decide to supersede it, and both commit — producing a state neither intended, with no record a conflict occurred.

**Stale-state screening:** an integrity check run against a snapshot that excludes concurrently-committing contradictory writes reaches the wrong verdict.

**Formal guarantee (design §3.3):** no belief is destructively lost; every contradiction resolution is deterministic and reconstructable; no unscreened belief can influence retrieval; every state transition is attributable and recorded immutably.

---

## System Architecture — Three Paths

### Write Path (synchronous, serializable, < 1s)

```
Source → A1/A2/A3 (Producers) → A11 (Canonicalizer) → A12 (Embedder)
                                                              │
                                              SERIALIZABLE TRANSACTION
                                              A7 (Resolution)
                                              ├── policy lookup
                                              ├── contradiction detection
                                              └── supersession
                                              INSERT status=PENDING
                                              COMMIT (with retry)
                                                              │
                                                        CDC event emitted
```

### Integrity Path (asynchronous, CDC-driven, seconds)

```
CDC event → A4 (Screening Gate)
             ├── S1 embedding anomaly
             ├── S2 source trust tier
             ├── S3 imperative content
             ├── S4 author behavior
             ├── S5 contradiction burst
             ├── S6 corroboration diversity
             ├── S7 derivation integrity
             └── S8 temporal plausibility
              │
     ┌────────┼────────┐
     ▼        ▼        ▼
  TRUSTED  QUARANTINED  INCONCLUSIVE
           │            │
           ▼            ▼
       A6 Cascade   A14 Review
```

### Recall Path (synchronous, read-only)

```
Query → A12 (same embedding model) → Vector search
         FILTER: status=trusted AND tx_to IS NULL AND valid window
         VIA: role_consumer → trusted_current_beliefs view
         VIA: CockroachDB Cloud Managed MCP Server (second enforcement layer)
         → A9 Recall → answer + citations
         → retrieval_log (what was ACTUALLY returned)
```

---

## Belief Lifecycle States

```
                    PENDING  ←── re-screen requested
                   /    |   \
              TRUSTED  QUARANTINED  INCONCLUSIVE
                │          │          │
                │          ▼          ▼
                │      under review (A14)
                │         /       \
                │     TRUSTED    REJECTED (terminal)
                ▼
            SUPERSEDED (terminal for recall; readable by auditors)

RETRIEVABLE: TRUSTED only
```

**Invariants:**
- Enters only as PENDING
- REJECTED is never deleted
- SUPERSEDED remains queryable by auditors

---

## Data Model (Nine Tables)

| Table | Owner | Purpose |
|---|---|---|
| `belief` | E1 | Central table; bitemporal; all state |
| `provenance` | E1 | Source tracking; `derived_from` for cascade |
| `integrity_verdict` | E2 | Append-only verdicts with full signal breakdown |
| `quarantine` | E2/E3 | Quarantine record with disposition |
| `contradiction_event` | E1 | Every contradiction, including incumbent-retained |
| `working_memory` | E1 | Ephemeral scratch; row-level TTL |
| `agent_identity` | E1 | Agent registry; behavior baseline; trust multiplier |
| `predicate_policy` | E1 | Cardinality, resolution strategy, normalization rules |
| `retrieval_log` | E4 | What was actually retrieved (forensic anchor) |

---

## Authority Model (Design §11)

Four database roles:
- `role_producer` — INSERT `pending` beliefs only
- `role_semantics` — UPDATE supersession fields, INSERT contradiction_events
- `role_integrity` — SELECT all, INSERT verdicts/quarantine, UPDATE status
- `role_consumer` — SELECT only on trusted-current view (most constrained)
- `role_auditor` — SELECT all including history, no write

**The security properties live in what each role CANNOT do.** No agent can both write beliefs and issue verdicts. No agent can release from quarantine without a recorded human reviewer.

---

## Bitemporal Model

Two independent axes:
- `valid_from` / `valid_to` — when this was true **in the world** ("was on Gold tier from January to June")
- `tx_from` / `tx_to` — when **the system** believed it ("we learned this on March 3rd")

**MVCC reconstruction:** "what would this query have returned at instant T" — exact, bounded by GC window.

**Bitemporal query:** "what did we believe about the world as of T" — curated view, unbounded.

Do not conflate them. MVCC is the demo/short-horizon tool. Bitemporal columns are the production mechanism. A knowledgeable reviewer will notice if the README conflates them.

---

## The "Why Not Postgres" Answer (Three Legs)

1. **Isolation default:** Postgres defaults to READ COMMITTED; serializable is opt-in. CockroachDB defaults to serializable.
2. **No native CDC:** Postgres CDC requires logical replication add-ons. CockroachDB CDC is native, driven by the transaction log.
3. **No native as-of-timestamp reads:** Postgres has no `AS OF SYSTEM TIME` syntax. CockroachDB has native MVCC time-travel.

---

## Agent Roster Summary

| Class | Agents | What they do |
|---|---|---|
| Producer | A1, A2, A3, A16 | Write `pending` beliefs; cannot trust them |
| Semantics | A11, A12, A7, A8 | Structure beliefs; run resolution |
| Integrity | A4, A5, A6, A13, A14, A15, A18, A19 | Decide trust; cascade; review; evaluate; verify posture; monitor substrate |
| Consumer | A9, A10 | Read-only via MCP Server; cannot write or touch quarantine |
| Platform | A17 | Telemetry only |

Never collapse: A4 (gate), A6 (cascade), A7 (resolution), A9 (recall), A10 (audit). These five are the project. A18 and A19 may be reduced in scope but cannot be removed — they are the integration points for Agent Skills Repo and ccloud CLI (required for four-tool submission threshold).

---

## Phase Sequence

P0 (verifications) → P1 (interfaces) → P2 (schema) → P3 (write path) → P4 (integrity) → P5 (containment) → P6 (recall + MCP) → P6.5 (posture + custody) → P7 (depth) → P8 (evaluation) → P9 (extensions) → P10 (submission)

Do not advance past a phase without its exit gate passing. The Lead determines gate passage.

## Four CockroachDB Tools (Submission Requirement)

All four must be integrated in load-bearing roles, each with a named failing removal test:

| Tool | Load-bearing use | Owner | Removal test |
|---|---|---|---|
| Distributed Vector Indexing | Nearest-neighbor recall search; structural tenant isolation via prefix | E1 | `test_removal_vector_index` |
| Managed MCP Server | A9/A10 read transport; second enforcement layer on TB4 | E4 | `test_removal_mcp_server` |
| ccloud CLI | A19 control-plane audit ingestion; backup catalog; Mechanism 3 | E3 | `test_removal_ccloud` |
| Agent Skills Repo | A18 posture verification (security, schema-design, observability families) | E3 | `test_removal_agent_skills` |

## Two Threat Model Additions (Design v3.0)

| Threat | Defense |
|---|---|
| T11 — Control drift (unauthorized REVOKE or DDL change) | A18 detects within one polling cycle; alert + WORM record |
| T12 — Substrate tampering (admin action on control plane) | A18 + A19; A19 surfaces admin actions in substrate-layer WORM audit |

## Two Audit Layers (Design §17)

| Layer | What it captures | Who emits | Where |
|---|---|---|---|
| Belief-layer | Every belief state transition | E1, E2, E3 agents | WORM bucket (S3 Object Lock) |
| Substrate-layer | Control-plane events (admin actions, role changes, backup operations) | A19 via ccloud CLI | Same WORM bucket, distinct key prefix |
