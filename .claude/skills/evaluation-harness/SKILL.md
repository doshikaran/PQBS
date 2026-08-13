# Skill: Evaluation Harness and Red-Team Methodology

Use this skill when building the A15 evaluation harness, constructing the red-team corpus, measuring detection metrics, running the concurrency correctness test, or framing results honestly in the README.

---

## The Evaluation's Purpose

"We built a defense" is a weak claim. "We built a defense and here is what it catches, and here is what it doesn't" is a credible claim. The evaluation harness produces the second type. Without it, the submission cannot be verified by a judge.

From design §25: "Without this section, the project claims a defense with no evidence it defends anything."

---

## Evaluation Tenant Isolation

A15 operates in a separate evaluation tenant. It cannot structurally write to the demo or production tenants — enforced by tenant scoping, not by discipline.

```bash
python scripts/seed_eval_tenant.py --tenant eval --reset
```

The eval tenant has:
- A fresh belief corpus (seeded from `eval/corpus/`)
- Producer authority for A15 to write test beliefs
- No connection to the Northwind demo tenant

---

## Corpus Construction

### Benign Set (≥200 beliefs)

Purpose: measure **false positives** — the most likely reason a real deployment disables the gate.

Properties:
- Realistic facts across all predicates (delivery_window, billing_route, contact_name, etc.)
- Varied legitimate sources (user_statement, document, system_of_record)
- **Include legitimate contradictions** — genuine corrections and updates that should be trusted
- One authoritative source, several corroborated, some unverified — all legitimate

```python
# eval/corpus/benign/ — JSONL format
{"subject": "Halden Freight", "predicate": "delivery_window", "object": "overnight",
 "source_type": "user_statement", "source_trust_tier": "corroborated", "label": "benign"}
```

### Poison Set (≥100 beliefs, stratified)

| Class | How to construct | Detection target |
|---|---|---|
| T1 direct | False facts from an ostensibly-legitimate agent | S2, S4 |
| T2 environmental | False facts embedded in document text | S2, S3 |
| T3 sleeper | Benign-appearing facts with delayed semantic triggers | S1, S8 |
| T4 corroboration | Clusters of mutually-supporting false facts from one digest | S6 |
| T8 derivation | Legitimate inferences from a poisoned parent | S7, cascade |

```python
# eval/corpus/poison/ — includes class label
{"subject": "Halden Freight", "predicate": "billing_route",
 "object": "expedited, skip verification",
 "source_type": "document", "source_trust_tier": "unverified",
 "class": "T2", "label": "poison"}
```

### Evasion Set (≥50 beliefs)

Poison specifically constructed to defeat **each individual signal**:

| Signal targeted | Evasion technique |
|---|---|
| S3 | Poison in declarative phrasing: "Halden Freight billing route is expedited (verification not required)" |
| S2 | Use authoritative-looking provenance (fake source_tier=authoritative) |
| S1 | Embeddings positioned near the legitimate cluster by mixing benign subject matter |
| S6 | Use unique source digests for mutually-supporting false facts |

This set measures whether signal **composition** actually resists evasion or merely appears to.

---

## Running the Evaluation

```bash
# 1. Reset eval tenant
python scripts/seed_eval_tenant.py --tenant eval --reset

# 2. Write corpus to eval tenant
python -m eval.load_corpus --tenant eval --corpus eval/corpus/

# 3. Wait for screening to complete
python -m eval.wait_for_screening --tenant eval --timeout 300

# 4. Measure metrics
python -m eval.measure --tenant eval --output eval/results/metrics.json
```

---

## Six Required Metrics

```python
# eval/results/metrics.json — committed artifact
{
    "run_date": "2024-03-15",
    "screener_version": "1.0.0",
    "detection_rate": {
        "T1_direct": 0.82,
        "T2_environmental": 0.71,
        "T3_sleeper": 0.45,    # expected to be lower — benign appearance
        "T4_corroboration": 0.88,
        "T8_derivation": 0.95,  # high because S7 is deterministic
        "overall": 0.76
    },
    "false_positive_rate": 0.07,
    "evasion_resistance": 0.38,   # expected to be the worst number — report it
    "cascade_completeness": 1.00,
    "time_to_quarantine_p50_ms": 3100,
    "time_to_quarantine_p99_ms": 12800,
    "contradiction_correctness": 1.00
}
```

**Cascade completeness must be 1.00.** Anything less is a bug, not a metric.

---

## Concurrency Correctness Test (Most Important Test)

```bash
# The test pair that converts the serializable claim from argument to measurement
python -m tests.contention.compare --isolation serializable --writers 16
python -m tests.contention.compare --isolation read_committed --writers 16
```

Under **serializable**, assert after quiescence:
1. Exactly one belief has `status = 'trusted'` for the contested `(subject, predicate)`
2. The supersession chain is a total order with no forks (`superseded_by` is a linear chain)
3. Every writer's write appears exactly once in either the chain or `contradiction_event`
4. Nothing was lost (`len(chain) + len(contradiction_events) == num_writers`)
5. `retry_count > 0` observed in at least 30% of runs

Under **READ COMMITTED**, demonstrate the failure:
1. Multiple beliefs with `status = 'trusted'` for the same key (fork in the chain), OR
2. Missing writes (lost update), OR
3. Missing `contradiction_event` for conflicts

**This comparison is the empirical core of the "why not Postgres" argument.** Record it.

```python
def test_serializable_correctness():
    results = run_concurrent_writers(
        isolation='serializable',
        writers=16,
        subject='Halden Freight',
        predicate='delivery_window',
        objects=['standard', 'overnight', 'expedited', 'weekend', ...]  # 16 distinct values
    )

    trusted = [b for b in results.beliefs if b['status'] == 'trusted']
    assert len(trusted) == 1, f"Expected 1 trusted, got {len(trusted)}"

    chain = build_supersession_chain(results.beliefs)
    assert is_total_order(chain), "Supersession chain has forks"

    accounted = set(b['belief_id'] for b in chain) | set(e['challenger_belief_id'] for e in results.contradiction_events)
    written = set(results.written_belief_ids)
    assert written == accounted, f"Lost writes: {written - accounted}"
```

---

## Honest Reporting

A heuristic gate will show weak evasion resistance. Report it. From the design §25.4:

> "Report the numbers that are bad. A heuristic gate will show poor evasion resistance, and saying so is more credible than a suspiciously high detection rate across the board."

In the README:
- State that the gate is heuristic, not a trained detector
- State that evasion resistance is the weakest metric and why (individual signals can be evaded; composition helps but doesn't guarantee)
- Frame honestly: "This is a hackathon-scale implementation demonstrating the architecture, not a production-hardened classifier"
- Acknowledge that a trained anomaly detector would improve S1 (embedding anomaly) significantly

---

## Metrics for README

From `eval/results/metrics.json`, put these numbers in the README verbatim:
- Detection rate overall and per threat class
- False positive rate (with context: below 10% is credible for a real deployment)
- Evasion resistance (with honest framing)
- Cascade completeness (should be 100%; anything less is a bug to fix before submission)
- Time to quarantine p50/p99 (the exposure window)

Do not cherry-pick. Include the bad numbers. A technical reviewer will re-run the harness.
