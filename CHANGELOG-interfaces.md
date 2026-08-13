# Interface Changelog

All cross-owner interface changes must be logged here. See CLAUDE.md § Interface Freeze
for the full change protocol.

---

## interface-freeze-v1 — 2026-08-13

**Status:** FROZEN

All contracts in `src/pqbs/contracts/` are frozen as of Phase 1 completion.

### Contracts frozen

| Contract | File | Owner boundary |
|---|---|---|
| `ProvenanceStub` | `contracts/provenance.py` | E1 → E1 (producer to write path) |
| `ProvenanceRecord` | `contracts/provenance.py` | E1 → E2 (A4 reads provenance) |
| `CandidateBelief` | `contracts/beliefs.py` | E1 producer → A11 (normalization) |
| `NormalizedBelief` | `contracts/beliefs.py` | A11 → A12 (embedding) |
| `EmbeddedBelief` | `contracts/beliefs.py` | A12 → A7 (resolution) |
| `ResolutionOutcome` | `contracts/resolution.py` | A7 → write path + telemetry |
| `BeliefSnapshot` | `contracts/cdc.py` | E1 DB → E2 screener (embedded in ChangeEvent) |
| `ChangeEvent` | `contracts/cdc.py` | **CRITICAL** E1 → E2 sync/async boundary |
| `SignalEvidence` | `contracts/signals.py` | Signal implementations → A4 |
| `SignalScore` | `contracts/signals.py` | S1–S8 → A4 verdict composition |
| `Verdict` | `contracts/verdicts.py` | A4 → A6, A14, audit sink |
| `QuarantineRecord` | `contracts/verdicts.py` | A4 → A6 cascade trigger |
| `CascadeRequest` | `contracts/cascade.py` | A4/A6 → A6 (recursive) + screening queue |
| `TemporalContext` | `contracts/recall.py` | Caller → A9 |
| `RecalledBelief` | `contracts/recall.py` | A9 → caller |
| `RecallRequest` | `contracts/recall.py` | Caller → A9 |
| `RecallResult` | `contracts/recall.py` | A9 → caller |
| `TemporalQuery` | `contracts/temporal.py` | Caller → A10 (audit agent) |
| `AuditRecord` | `contracts/audit.py` | All agents → WORM sink |

### Key design decisions recorded at freeze

1. **`ChangeEvent.after` is a full `BeliefSnapshot`**, not just a primary key. This ensures A4 can compute all 8 signals without a secondary DB read. Emitting PKs only would create a read-under-write hazard.
2. **`Verdict.signal_scores` must contain all 8 `SignalId` values.** The validator enforces this at construction time. No partial verdicts are permitted.
3. **`EMBEDDING_DIM = 1024`** matches `amazon.titan-embed-text-v2:0`. Confirmed in V1 spike (`spikes/v1_vector_index.py`). Changing this requires a schema migration and re-embedding of all stored beliefs.
4. **All contracts use `frozen=True`** — they are immutable value objects. Modification after construction raises `ValidationError`.
5. **`extra="forbid"` on all contracts** — unknown fields are rejected. This prevents silent data loss when contracts evolve.
6. **`CascadeRequest.max_depth = 20`** is the safety limit for recursive cascade. Exceeding it raises `ValidationError` at construction time.

### Change protocol reminder

After this freeze, any change to a contract requires:
1. Identify the impact (which agents/tests are affected).
2. Notify the Lead.
3. Identify all consumers of the interface.
4. Agree on updated contract.
5. Update all consuming tests.
6. Document the change in THIS file with a new versioned entry.
7. Only then implement.

---

*No changes have been made to frozen interfaces since freeze-v1.*
