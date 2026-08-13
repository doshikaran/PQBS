# Skill: Screening Gate and Signal Composition

Use this skill when implementing the screening gate (A4), writing individual signals (S1–S8), composing verdicts, or ensuring fail-closed behavior.

---

## The Gate's Purpose

The screening gate enforces the guarantee that no unscreened belief influences retrieval. It is fail-closed: when the gate is down, beliefs accumulate in `pending` — unreachable. A partial screening result is not a soft downgrade; a belief stays `pending` until a complete verdict exists.

**From design §14:** "If no verdict can be reached (model unavailable, timeout), the belief remains `pending` — unusable. Failure to screen is not failure to enforce."

---

## Signal Implementation Order

Build cheapest first so the gate is demonstrable before complete (see BUILD-PLAN §6.2):

1. S2 source trust tier — pure lookup, no model call
2. S3 imperative content — lexical prefilter + model call; the demo's most legible signal
3. S7 derivation integrity — pure graph check; enables cascade
4. S6 corroboration diversity — source_digest independence check
5. S1 embedding anomaly — requires corpus distribution statistics
6. S5 contradiction burst — windowed aggregation
7. S4 author behavior — requires behavior_baseline accumulation
8. S8 temporal plausibility — lowest marginal value; build last

After S2 + S3 + S7, the gate is demonstrable in the demo.

---

## Signal Output Contract

Every signal produces a `SignalScore`:

```python
class SignalScore(BaseModel):
    signal_id: str           # 'S1', 'S2', ..., 'S8'
    score: float             # 0.0 = strongly poison, 1.0 = strongly benign
    evidence: dict           # structured rationale — NOT "the model said no"
    latency_ms: int
```

The `evidence` dict must contain enough detail to explain the verdict to a human reviewer. Examples:
- S2: `{"trust_tier": "unverified", "source_uri": "https://example.com/doc.pdf"}`
- S3: `{"classification": "imperative", "confidence": 0.94, "trigger_phrases": ["should always be", "may be skipped"]}`
- S7: `{"parent_ids": ["uuid1"], "parent_statuses": ["quarantined"]}`

---

## S2 — Source Trust Tier

```python
def signal_s2_source_trust_tier(provenance: ProvenanceRecord) -> SignalScore:
    score_map = {
        'authoritative': 1.0,
        'corroborated': 0.7,
        'unverified': 0.3,
        'untrusted': 0.0,
    }
    score = score_map[provenance.source_trust_tier]
    return SignalScore(
        signal_id='S2',
        score=score,
        evidence={'trust_tier': provenance.source_trust_tier, 'source_uri': provenance.source_uri},
        latency_ms=0
    )
```

---

## S3 — Imperative Content

The distinction: "Halden Freight prefers overnight delivery" (assertion) vs. "Halden Freight accounts should always be routed to expedited billing" (instruction).

Implementation: lexical prefilter, then model call only when prefilter fires. Record rationale.

```python
IMPERATIVE_MARKERS = [
    'should always', 'must be', 'may be skipped', 'do not', 'always route',
    'never verify', 'bypass', 'override', 'ignore verification'
]

def signal_s3_imperative_content(belief: dict) -> SignalScore:
    text = belief['object']
    prefilter_match = any(marker in text.lower() for marker in IMPERATIVE_MARKERS)

    if not prefilter_match:
        return SignalScore(signal_id='S3', score=1.0,
                           evidence={'classification': 'declarative', 'prefilter': False},
                           latency_ms=0)

    # Model call — record rationale, not just result
    start = time.monotonic()
    result = classify_imperative(text)   # LLM call
    latency = int((time.monotonic() - start) * 1000)

    score = 0.0 if result['classification'] == 'imperative' else 0.8
    return SignalScore(
        signal_id='S3',
        score=score,
        evidence={
            'classification': result['classification'],
            'confidence': result['confidence'],
            'trigger_phrases': [m for m in IMPERATIVE_MARKERS if m in text.lower()],
            'rationale': result.get('rationale', '')   # from model response
        },
        latency_ms=latency
    )
```

---

## S7 — Derivation Integrity

```python
def signal_s7_derivation_integrity(provenance: ProvenanceRecord, conn) -> SignalScore:
    if not provenance.derived_from:
        return SignalScore(signal_id='S7', score=1.0,
                           evidence={'derived': False}, latency_ms=0)

    parent_statuses = conn.execute(
        "SELECT belief_id, status FROM belief WHERE belief_id = ANY(%s) AND tenant_id = %s",
        [provenance.derived_from, provenance.tenant_id]
    ).fetchall()

    quarantined_parents = [r['belief_id'] for r in parent_statuses if r['status'] == 'quarantined']

    if quarantined_parents:
        return SignalScore(
            signal_id='S7', score=0.0,
            evidence={'parent_ids': [str(p) for p in quarantined_parents], 'parent_statuses': ['quarantined']},
            latency_ms=0
        )

    return SignalScore(signal_id='S7', score=1.0,
                       evidence={'parent_ids': [str(r['belief_id']) for r in parent_statuses], 'all_trusted': True},
                       latency_ms=0)
```

---

## S6 — Corroboration Diversity

Source diversity, not volume. Same agent or same `source_digest` = one unit of corroboration regardless of count.

```python
def signal_s6_corroboration_diversity(belief: dict, conn) -> SignalScore:
    # Find other beliefs supporting the same (subject, predicate, object_normalized)
    corroborating = conn.execute(
        """SELECT DISTINCT p.source_digest, p.ingestion_agent_id
           FROM belief b JOIN provenance p ON b.provenance_id = p.provenance_id
           WHERE b.tenant_id = %s AND b.subject = %s AND b.predicate = %s
             AND b.object_normalized = %s AND b.status = 'trusted'
             AND p.source_digest != %s""",
        [belief['tenant_id'], belief['subject'], belief['predicate'],
         belief['object_normalized'], belief['source_digest']]
    ).fetchall()

    unique_digests = len(set(r['source_digest'] for r in corroborating))
    score = min(unique_digests / 3.0, 1.0)   # 3+ independent sources = full score

    return SignalScore(
        signal_id='S6', score=score,
        evidence={'unique_source_count': unique_digests, 'required_for_full_score': 3},
        latency_ms=0
    )
```

---

## Verdict Composition

```python
SIGNAL_WEIGHTS = {
    'S1': 0.20, 'S2': 0.25, 'S3': 0.30, 'S4': 0.05,
    'S5': 0.05, 'S6': 0.10, 'S7': 0.00, 'S8': 0.05
}
# Note: S7 weight is 0 because it triggers automatic quarantine, not a score contribution
# Document rationale for weights in docs/decisions/

TRUST_THRESHOLD = 0.7
QUARANTINE_THRESHOLD = 0.4

def compose_verdict(signal_scores: dict[str, SignalScore]) -> tuple[str, float, str | None]:
    # S7 check first: automatic quarantine regardless of other signals
    if signal_scores.get('S7') and signal_scores['S7'].score == 0.0:
        return 'quarantined', 0.0, 'derived_from_quarantined'

    weighted_sum = sum(
        signal_scores[sid].score * weight
        for sid, weight in SIGNAL_WEIGHTS.items()
        if sid in signal_scores
    )
    total_weight = sum(w for sid, w in SIGNAL_WEIGHTS.items() if sid in signal_scores)
    trust_score = weighted_sum / total_weight if total_weight > 0 else 0.5

    if trust_score >= TRUST_THRESHOLD:
        return 'trusted', trust_score, None
    elif trust_score <= QUARANTINE_THRESHOLD:
        # Find dominant failing signal
        triggering = min(signal_scores.values(), key=lambda s: s.score)
        return 'quarantined', trust_score, triggering.signal_id
    else:
        return 'inconclusive', trust_score, None
```

---

## Fail-Closed Invariants

1. If any signal evaluation raises an exception → keep belief in `pending`, log the error, retry later.
2. If the model service is unavailable → keep belief in `pending`, no verdict written.
3. `inconclusive` → belief stays `pending`, queued for A14 review. It is NOT retrievable.
4. A `pending` belief is never returned by `role_consumer` — enforced by the view, not by the screening worker.
5. Never write a partial verdict (some signals scored, others missing) as if it were complete.

---

## Idempotency Check Pattern

```python
def screen_belief(event: ChangeEvent, conn) -> Optional[Verdict]:
    # Gate: already screened by this version?
    existing = conn.execute(
        "SELECT 1 FROM integrity_verdict WHERE belief_id = %s AND screener_version = %s",
        [event.belief_id, SCREENER_VERSION]
    ).first()
    if existing:
        return None   # skip duplicate

    signal_scores = run_all_signals(event, conn)
    verdict_str, trust_score, triggering = compose_verdict(signal_scores)

    # Write verdict
    write_verdict(event.belief_id, event.tenant_id, verdict_str, trust_score,
                  signal_scores, triggering, conn)

    # Update belief status
    conn.execute(
        "UPDATE belief SET status = %s, trust_score = %s, screened_at = NOW() WHERE belief_id = %s",
        [verdict_str, trust_score, event.belief_id]
    )

    return Verdict(...)
```
