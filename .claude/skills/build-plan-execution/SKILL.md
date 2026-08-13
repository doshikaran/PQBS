# Skill: Build Plan Execution

Use this skill when managing phase progression, checking exit gates, verifying definitions of done, or tracking traceability between design sections and implementation phases.

---

## Phase Structure

The full phase sequence is in `docs/BUILD-PLAN.md`. Never start phase N+1 until phase N's exit gate passes.

### Phase 0 — Environment and Verification

**Owner:** E5  
**Duration:** First working session  
**Exit gate:** V5 passes at ≥90% reliability, OR the team has explicitly decided and documented the alternative narrative.

Six verification spikes in `spikes/`. Each writes a paragraph to `docs/VERIFICATIONS.md`.

| Spike | Question | Exit criterion |
|---|---|---|
| V5 | Can we reliably force a serializable retry? | ≥90% across ≥20 runs — **run this first** |
| V1 | Vector index: GA or preview? Distance metrics? Dimension limits? | Index created, used by planner, sane neighbors |
| V2 | CDC available on this cluster tier? Cost? | 100% of 100 events arrive, latency seconds not minutes |
| V3 | MVCC retention window depth? | Reads succeed ≥30 minutes back |
| V4 | Managed MCP server write semantics? | Read/write/audit all understood |
| V6 | Row-level TTL timing? | Rows expire within demo-showable window |

If V5 fails: **stop and re-plan the narrative before proceeding.** Do not silently continue.

---

### Phase 1 — Scaffold and Interface Freeze

**Owner:** Lead + all  
**Exit gate:** Every engineer can independently import contracts and write a stub against them.

Key deliverables:
- Directory structure committed
- Public GitHub repo with license visible
- All 14 contracts in `src/pqbs/contracts/` as Pydantic models
- `interface-freeze-v1` tag pushed
- `.env.example` complete; `.env` git-ignored

The 14 contracts (from BUILD-PLAN §3.3):

| Contract | Produced by | Consumed by |
|---|---|---|
| CandidateBelief | A1/A2/A3/A16 | A11 |
| NormalizedBelief | A11 | A12 |
| EmbeddedBelief | A12 | A7 |
| ProvenanceRecord | producers | A4 |
| ResolutionOutcome | A7 | telemetry |
| ChangeEvent | CDC | A4 |
| SignalScore | S1–S8 | A4 |
| Verdict | A4 | A6, A14, audit |
| QuarantineRecord | A4 | A6, A14 |
| CascadeRequest | A6 | A4 |
| RecallRequest | caller | A9 |
| RecallResult | A9 | caller |
| TemporalQuery | caller | A10 |
| AuditRecord | all | WORM sink |

**`ChangeEvent` is the most critical.** It crosses the E1/E2 sync/async boundary. If shape is wrong, Phase 4 stalls entirely.

---

### Phase 2 — Schema (E1)

**Exit gate:** The authority matrix (design §11) is enforced by the database. Every "—" in that table has a corresponding failing negative test.

Checklist:
- [ ] All 12 migrations apply cleanly from empty
- [ ] All 12 migrations roll back cleanly
- [ ] Vector index used by query planner (V1 findings applied)
- [ ] Row-level TTL on `working_memory` (V6 findings applied)
- [ ] All four roles with correct grants
- [ ] `role_consumer` cannot read quarantined content
- [ ] `role_producer` cannot insert `status = 'trusted'`
- [ ] Seed script: 2,000+ beliefs deterministically

---

### Phase 3 — Write Path (E1)

**Exit gate:** Design §26.7 reproduces end-to-end: three writers, deterministic chain, nothing lost, retry counted.

Checklist:
- [ ] Retry wrapper re-reads on retry (not reusing stale)
- [ ] Contention harness passes with 8+ concurrent writers
- [ ] Canonicalization collapses value variants
- [ ] Ambiguous canonicalization → `elevated` sensitivity
- [ ] Embedding pre-transaction (trace-verified)
- [ ] All five resolution bases tested
- [ ] `contradiction_event` written on `incumbent_retained`
- [ ] Every belief enters as `pending`
- [ ] Write latency p50 < 400ms, p99 < 1200ms

---

### Phase 4 — Integrity Path (E2)

**Exit gate:** A poisoned belief written by a producer never becomes retrievable, and the verdict explains why in per-signal detail.

Checklist:
- [ ] CDC delivers 100% of committed writes
- [ ] Screening is idempotent on duplicate delivery
- [ ] Signals S2, S3, S7 implemented with per-signal evidence
- [ ] All three verdict outcomes produced on test data
- [ ] `integrity_verdict` has full signal breakdown
- [ ] Fail-closed test passes
- [ ] Screening lag p50 < 5s, p99 < 15s

---

### Phase 5 — Containment (E3)

**Exit gate:** Design §26.8 reproduces: quarantining a parent causes every derived belief to leave `trusted` automatically, with cascade depth recorded.

Checklist:
- [ ] A2 populates `derived_from`; empty derivations rejected
- [ ] Cascade re-screens 100% of descendants
- [ ] Cascade is idempotent
- [ ] Cascade is cycle-safe (deliberate cycle test)
- [ ] Review requires reviewer identity for release
- [ ] WORM bucket configured; delete attempt fails
- [ ] Audit records for all six transition types
- [ ] Audit-sink-unavailable blocks writes

---

### Phase 6 — Recall (E4)

**Exit gate:** Design §26.9 reproduces: bitemporal query works beyond MVCC window; Mechanism 2 failure is visible and explained. A9 and A10 read through the MCP Server; a write attempt through MCP fails at the protocol layer.

Checklist:
- [ ] Recall enforced at view layer (not application code)
- [ ] `role_consumer` cannot bypass filtering with arbitrary SQL
- [ ] `retrieval_log` populated on every recall
- [ ] Mechanism 1 (bitemporal) works with no time bound
- [ ] Mechanism 2 (MVCC) fails gracefully beyond window
- [ ] Attribution queries return agent identity and provenance chain
- [ ] Recall latency p50 < 600ms
- [ ] A9 and A10 read through the CockroachDB Cloud Managed MCP Server
- [ ] MCP write attempt fails at the PROTOCOL layer (not application layer)
- [ ] MCP audit log retrievable after a consumer session

---

### Phase 6.5 — Self-Verification: Posture and Custody (E3)

**Exit gate:** All four CockroachDB tools are integrated in load-bearing roles, and each has a test that fails if the tool is removed.

Implements: design §10 A18/A19, §16 M3, §17 two-layer audit, §19.0, threats T11/T12.

Checklist:
- [ ] Agent Skills Repo installed; security, schema-design, observability skill families identified
- [ ] Posture baseline captured and committed as `docs/posture-baseline.json`
- [ ] A18 runs on schedule via Lambda; writes attestations to WORM sink
- [ ] Drift test passes: deliberate REVOKE detected and alerted within one cycle
- [ ] Negative test: A18 confirmed cannot remediate (no DDL, no GRANT authority)
- [ ] ccloud service account created with scoped RBAC (read + backup-trigger; no restore)
- [ ] A19 ingests control-plane audit to WORM sink (substrate-layer records)
- [ ] Insider-threat test: admin action surfaces in WORM substrate audit within one polling cycle
- [ ] Negative test: A19 confirmed cannot restore (service account lacks restore authority)
- [ ] Mechanism 3 answers a query beyond the MVCC GC window using backup catalog
- [ ] Backup coverage gaps reported explicitly
- [ ] Removal test for Agent Skills Repo: `test_removal_agent_skills` fails when repo removed
- [ ] Removal test for ccloud CLI: `test_removal_ccloud` fails when ccloud removed
- [ ] (Plus removal tests for vector index and MCP Server from Phase 6, now all four confirmed at CP3.5)

---

### Phase 7 — Depth (E2/E3/E5)

No explicit exit gate; all items must be complete before Phase 8.

Checklist:
- [ ] All eight signals (S1–S8) implemented
- [ ] Each signal's marginal contribution measured
- [ ] A5 drift detection running on schedule
- [ ] All four metric families instrumented
- [ ] Full-lifecycle traces working
- [ ] All 13 failure-mode tests pass

---

### Phase 8 — Evaluation (E5)

**Exit gate:** All six metrics measured and committed to `eval/results/`; READ COMMITTED comparison demonstrates the anomaly.

Checklist:
- [ ] Three corpora built and committed to `eval/corpus/`
- [ ] All six metrics measured and written to `eval/results/`
- [ ] Concurrency correctness test passes under serializable
- [ ] READ COMMITTED comparison demonstrates the failure
- [ ] Results summarized in README with honest framing

---

### Integration Checkpoints

| CP | After | What to verify |
|---|---|---|
| CP1 | P2 | Everyone connects to schema; negative tests pass for all owners |
| CP2 | P4 | Write path feeds gate end-to-end via ChangeEvent |
| CP3 | P6 | Full path: write → screen → cascade → recall → audit; MCP read transport verified |
| CP3.5 | P6.5 | All four CockroachDB tools verified integrated; run removal test for each (each must fail when the tool is removed) |
| CP4 | P8 | Evaluation numbers reviewed; agree on README framing |

---

## Traceability: Design §1.4

Key design-doc sections → implementing phase:

| Design topic | Phase | Owner |
|---|---|---|
| §9 all nine tables | P2 | E1 |
| §13 write path | P3 | E1 |
| §14 integrity path | P4 | E2 |
| §14.4 cascade | P5 | E3 |
| §17 two-layer audit/WORM | P5, P6.5 | E3 |
| §15 recall path | P6 | E4 |
| §16 temporal reconstruction (M1, M2, M3) | P6, P6.5 | E4, E3 |
| §8.5 MCP Server as read transport | P6 | E4 |
| §10 A18/A19 agent specs | P6.5 | E3 |
| §19.0 posture baseline | P6.5 | E3 |
| §4.1 threat model (incl. T11, T12) | P6.5, P8 | E3, E5 |
| §23 observability | P7 | E5 |
| §24 failure modes | P7 | All |
| §25 evaluation plan | P8 | E5 |
| §26 worked example | P10 | E5 |

Never explicitly deferred (out of scope): trained neural anomaly detection, production review UI, differential privacy, formal isolation verification, gate self-hardening.

---

## Risk Register Summary

| Risk | When | Response |
|---|---|---|
| V5 fails | P0 | Stop. Re-plan narrative. Document before proceeding. |
| CDC unavailable | P0 V2 | Polling fallback; disclose weakened guarantee in README |
| Retry reuses stale reads | P3 | Contention harness catches it — do not skip the test |
| Cardinality misconfigured | P3 | Test both directions explicitly |
| Cascade hangs on cycle | P5 | Deliberate-cycle test before integration |
| Embedding model mismatch write vs. recall | P6 | Single shared function; assert config equality |
| Free-tier exhaustion | Any | Daily budget check; changefeed teardown discipline |
| Credentials in git history | Any | Audit `git log --all` before making repo public |
| README instructions don't work | P10 | Clean-room test by someone who didn't write them |
