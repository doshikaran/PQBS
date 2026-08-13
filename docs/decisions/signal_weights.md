# Signal Weights — Rationale

| Signal | ID | Weight | Rationale |
|---|---|---|---|
| S3 Imperative Content | S3 | 0.25 | Highest-weight signal. Prompt-injection attacks are the primary threat vector for an agentic memory system. Imperative content (instructions disguised as facts) is the clearest observable indicator of a poisoning attempt and is human-readable without model training. |
| S1 Embedding Anomaly | S1 | 0.20 | A belief whose semantic embedding is an outlier relative to the trusted corpus for its predicate is anomalous regardless of surface content. Catches adversarial beliefs crafted to avoid S3's lexical triggers. |
| S2 Source Trust Tier | S2 | 0.20 | Source provenance is the strongest single prior for belief reliability. `authoritative` sources (system-of-record) carry near-zero risk; `untrusted` sources are the dominant source of poisoned beliefs in practice. |
| S4 Author Behavior | S4 | 0.10 | Write-velocity bursts (>20 writes/hour) indicate automated flooding. Lower weight because a single compromised agent can mask this signal, and legitimate batch ingestion creates false positives. |
| S5 Contradiction Burst | S5 | 0.10 | Coordinated contradiction storms indicate adversarial activity. Lower weight than S3/S1/S2 because contradiction events are a lagging indicator — the poisoned beliefs have already entered the pipeline by the time the burst is visible. |
| S6 Corroboration Diversity | S6 | 0.05 | Low distinct-source diversity is a weak signal: many legitimate beliefs have a single authoritative source. Used as a tiebreaker and to flag single-source propagation chains. |
| S7 Derivation Integrity | S7 | 0.05 | Cascade check: a belief derived from a quarantined parent is immediately high-risk. Low base weight because most beliefs have no derivation chain; when S7 fires at 1.0 it dominates the composite even at 0.05 weight. |
| S8 Temporal Plausibility | S8 | 0.05 | Temporal sanity checks (future dates, inverted ranges) are rare anomalies, not primary attack vectors. Catches malformed or adversarially timestamped beliefs without over-penalising legitimate historical data. |

## Threshold rationale

- `TRUST_THRESHOLD = 0.4`: Beliefs scoring ≤ 0.4 are trusted. With all signals at benign values (~0.05–0.1), the composite is well below this bound. The threshold is conservative to prevent false positives on legitimate content from unverified sources.
- `QUARANTINE_THRESHOLD = 0.7`: Beliefs scoring ≥ 0.7 are quarantined. This requires at least two medium-weight signals to fire, preventing single-signal quarantine of legitimate beliefs with minor anomalies.
- The `INCONCLUSIVE` band (0.4–0.7) represents ambiguous cases routed to A14 for human review.
