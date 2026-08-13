# PQBS — Global Engineering Rules

**Source of truth:** `docs/DESIGN.md` (what the system means) and `docs/BUILD-PLAN.md` (how it gets built). When any instruction in a conversation conflicts with those documents, the documents win. If genuinely ambiguous, escalate to the Lead rather than inventing an architectural decision.

---

## Ownership

| Owner | Domain | Primary files |
|---|---|---|
| E1 — Substrate | Schema, migrations, transactions, retry, canonicalization, resolution | `migrations/`, `src/pqbs/substrate/`, `src/pqbs/agents/semantics/`, `src/pqbs/agents/producer/` |
| E2 — Integrity | CDC, screening gate, signals S1–S8, verdict composition | `src/pqbs/integrity/`, `src/pqbs/agents/integrity/` (A4 only), `infra/lambda/` (screener) |
| E3 — Containment | Cascade, quarantine lifecycle, review disposition, audit sink, drift detection | `src/pqbs/agents/integrity/` (A2, A5, A6, A14), `infra/worm/`, `infra/iam/` |
| E4 — Surface | Recall path, audit queries, temporal reconstruction, demo UI | `src/pqbs/recall/`, `src/pqbs/audit/`, `src/pqbs/agents/consumer/`, `demo/ui/` |
| E5 — Evidence | Evaluation harness, red-team corpus, contention harness, observability, submission | `eval/`, `tests/`, `src/pqbs/telemetry/`, `scripts/`, `demo/scripts/` |

**No engineer modifies another's files without explicit coordination.** When a change touches a boundary, both owners must agree, and the change is logged in `CHANGELOG-interfaces.md`.

---

## Phase Gates

Phases are defined in `docs/BUILD-PLAN.md` §2–§12. The sequence is:

```
P0 (env + verifications) → P1 (scaffold + interface freeze) → P2 (schema) →
P3 (write path) → P4 (integrity path) → P5 (containment) →
P6 (recall surface) → P7 (depth) → P8 (evaluation) → P9 (extensions) → P10 (submission)
```

**Do not start phase N+1 until phase N's exit gate passes.** The Lead determines when a gate has actually passed. Exit gates are defined in the build plan; they are not paraphraseable — read them exactly.

---

## Interface Freeze

All cross-owner interfaces in `src/pqbs/contracts/` are frozen at the end of Phase 1. After freeze:
1. Identify the impact of any proposed change.
2. Notify the Lead.
3. Identify all consumers of the interface.
4. Agree on updated contract.
5. Update all consuming tests.
6. Document the change in `CHANGELOG-interfaces.md`.
7. Only then implement.

The `ChangeEvent` contract is the most fragile: it crosses the sync/async boundary between E1 and E2. Get it right in Phase 1.

---

## Completion Vocabulary

These four terms are not synonyms. Use them precisely.

- **IMPLEMENTED** — code exists
- **TESTED** — automated tests cover the behavior
- **VERIFIED** — E5 has run the verification harness against it with measured results
- **INTEGRATED** — it works end-to-end with all upstream and downstream owners

A phase is not done until all four apply to every item in its Definition of Done.

---

## Security Invariants (never violate these)

1. No belief enters the store with `status = 'trusted'`. Every write is `pending`.
2. No consumer-role query can return a `quarantined` or `pending` belief. Enforced by role-scoped views.
3. The retry wrapper must re-read state on retry. Reusing stale reads silently reintroduces the anomaly the system exists to prevent.
4. Serializable isolation is never downgraded on retry exhaustion. Fail with an explicit contention error.
5. The screening gate is fail-closed. If the worker is down, beliefs accumulate in `pending` and are unretrievable.
6. Audit records cannot be deleted or overwritten. WORM bucket with retention lock.
7. No agent with write authority can issue verdicts. No agent with verdict authority can write beliefs.

---

## Git Discipline

- Commits are focused on one logical change and one owner's domain.
- Never commit `.env` or any file containing credentials. Check `git log --all` before making the repo public.
- Never reset or discard another agent's commits.
- Tag `interface-freeze-v1` after Phase 1 contracts are committed.
- Clearly state which files are modified in every report.
- Pre-commit hook: scan for secrets before every commit.

---

## Cost Discipline

- Disable changefeeds when not actively testing. A leftover feed running overnight is the most likely way to exhaust the free-tier allowance.
- Do not write audit records into the WORM bucket during development — they cannot be deleted. Use a separate non-locked dev bucket.
- Cache model embeddings by content hash during development.
- Run `ccloud cluster usage pqbs-dev` daily.
- Track all resource consumption in `docs/BUDGET.md`.

---

## Evidence Over Assertion

"It works" is not acceptable completion evidence. Every claim must be supported by:
- A passing test that would fail if the claim were false, or
- A measured output in `eval/results/`, or
- A verification recorded in `docs/VERIFICATIONS.md`.

Honest weak numbers are more credible than suspiciously good ones. Report the bad metrics too.

---

## Escalation

Escalate to the Lead (and ultimately to the human) when:
- A design decision is ambiguous or underdefined.
- An interface between owners must change.
- A phase gate cannot pass as specified.
- A security invariant cannot be maintained.
- The free-tier budget is at risk.
- A Phase 0 verification result forces an architectural change.
- Any action would affect another owner's domain without their agreement.

Do not silently invent architectural decisions. Preserve the ambiguity and escalate.
