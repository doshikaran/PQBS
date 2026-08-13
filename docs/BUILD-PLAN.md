# PQBS — Build Plan
### Poison-Quarantine Belief Store: Implementation Roadmap

**Companion to:** `poison-quarantine-belief-store-design.md` v2.0
**Version:** 1.0
**Team:** 5 engineers
**Deliverable:** Public repo (open-source license), functional demo, <3 min video, submission writeup

---

## 0. How to use this document

**Relationship to the design doc.** The design document defines *what the system means*. This document defines *how it gets built, in what order, by whom, and how we know a phase is done*. §1.4 below is a traceability matrix mapping every design-doc section to the phase that implements it — if something in the design doc has no phase, it is either explicitly deferred or it is a gap, and both are marked.

**Command conventions.**
- Commands marked `[VERIFY]` have syntax I could not confirm against live documentation. Check the vendor docs before running; do not paste blind.
- Commands marked `[EXACT]` are standard tooling I'm confident about.
- Version numbers are marked `[PIN-ON-INSTALL]` — capture the actual resolved version into the lockfile and the README rather than trusting a number written here.

**Phase gates.** Each phase ends with a **Definition of Done** and an **Exit Gate**. Do not start phase *n+1* until phase *n*'s exit gate passes. The gates exist because this project has one structural failure mode: building breadth before the spine is proven.

**Language choice.** Python is assumed throughout (best ecosystem fit for the model/embedding tooling and the agent skills repo). Where the choice matters, it's noted. If the team prefers TypeScript, the phase structure is unchanged; only §2.3 tooling differs.

---

## 1. Planning

### 1.1 Ownership map

Five engineers. Assignments are by *vertical slice with a clear interface*, not by layer — layer-based splits create the integration cliff where nothing works until everything works.

| Owner | Primary domain | Design doc sections | Agents owned |
|---|---|---|---|
| **E1 — Substrate** | Schema, migrations, transactions, resolution logic, retry semantics | §9, §13, §19 | A7, A11 |
| **E2 — Integrity** | CDC wiring, screening gate, signals, verdict logic | §14, §23 | A4, A12 |
| **E3 — Containment** | Cascade, quarantine lifecycle, review disposition, audit sink | §14.4, §17 | A6, A14, A5 |
| **E4 — Surface** | Recall path, audit agent, temporal reconstruction, demo UI | §15, §16, §22 | A9, A10 |
| **E5 — Evidence** | Evaluation harness, red-team corpus, contention harness, observability, README/video | §25, §23, §30 | A15, A17, A13 |

**E5 is the most important role and the one most likely to be under-resourced.** E5 owns the contention harness (Phase 0's exit gate), the evaluation numbers (the project's evidence), and the video (what judges actually grade). Assign your strongest generalist.

### 1.2 Interface freeze policy

All cross-owner interfaces (§3) are frozen at the end of Phase 1. After freeze, interface changes require agreement from both sides and a note in `CHANGELOG-interfaces.md`. This is the single highest-leverage process rule for parallel work.

### 1.3 Performance targets

The design doc names metrics but no targets. These are the targets. They are deliberately modest — hackathon scale, honest numbers.

| Metric | Target | Rationale |
|---|---|---|
| Write path latency (p50) | < 400 ms | Excludes embedding; embedding is pre-transaction |
| Write path latency (p99) | < 1200 ms | Includes up to 2 serializable retries |
| Screening lag (p50) | < 5 s | The fail-closed window — must be small enough to feel acceptable |
| Screening lag (p99) | < 15 s | Under demo load |
| Recall latency (p50) | < 600 ms | Includes query embedding |
| Serializable retry rate | < 5% under normal load | Higher indicates a hot-key design problem |
| Retry rate under contention test | > 30% | This is a *floor* — the test exists to force retries |
| Cascade completeness | 100% | Anything less is a bug, not a tuning issue |
| Belief corpus size (demo) | 2,000–5,000 | Enough for vector index to be meaningful |
| Embedding dimensions | Match model default `[PIN-ON-INSTALL]` | Do not truncate |
| Concurrent writers (contention test) | 8–16 | Enough to force conflicts reliably |

### 1.4 Traceability matrix

Every design-doc section mapped to its implementing phase. **This is the anti-discrepancy control.**

| Design §  | Topic | Phase | Owner | Status |
|---|---|---|---|---|
| §3 | Problem statement | — | — | Narrative only; goes in README |
| §4.1 T1–T10 | Threat model | P8 | E5 | Each threat becomes an eval class |
| §4.3 | Trust boundaries | P2, P6 | E1, E4 | Enforced via roles + views |
| §5 | Prior art comparison | P10 | E5 | README differentiation section |
| §6 P1–P8 | Design principles | All | All | Each principle has an assertion test |
| §7 | Architecture diagrams | P10 | E5 | Reproduce in README + video |
| §8 | Lifecycle state machine | P3 | E1 | Encoded as DB constraints |
| §9.1 | `belief` | P2 | E1 | |
| §9.2 | `provenance` | P2 | E1 | |
| §9.3 | `integrity_verdict` | P2 | E2 | |
| §9.4 | `quarantine` | P2 | E3 | |
| §9.5 | `contradiction_event` | P2 | E1 | |
| §9.6 | `working_memory` | P2 | E1 | TTL — depends on V6 |
| §9.7 | `agent_identity` | P2 | E1 | |
| §9.8 | `predicate_policy` | P2 | E1 | |
| §9.9 | `retrieval_log` | P2 | E4 | |
| §10 A1 | Ingestion | P3 | E1 | |
| §10 A2 | Inference | P5 | E3 | Required for cascade demo |
| §10 A3 | Correction | P3 | E1 | |
| §10 A16 | Federation | P9 | E3 | Optional |
| §10 A11 | Canonicalization | P3 | E1 | |
| §10 A12 | Embedding | P3 | E2 | |
| §10 A7 | Resolution | P3 | E1 | **Spine** |
| §10 A8 | Consolidation | P9 | E1 | Optional |
| §10 A4 | Screening gate | P4 | E2 | **Spine** |
| §10 A5 | Drift detection | P7 | E3 | |
| §10 A6 | Cascade | P5 | E3 | **Spine** |
| §10 A13 | Rate limiting | P9 | E5 | Optional |
| §10 A14 | Review disposition | P5 | E3 | |
| §10 A15 | Red-team | P8 | E5 | **Evidence** |
| §10 A9 | Recall | P6 | E4 | **Spine** |
| §10 A10 | Audit | P6 | E4 | **Spine** |
| §10 A17 | Telemetry | P7 | E5 | |
| §11 | Authority matrix | P2 | E1 | DB roles + grants |
| §12 | Collapse plan | P9 | Lead | Decision checkpoint |
| §13 | Write path | P3 | E1 | |
| §14 | Integrity path | P4 | E2 | |
| §15 | Recall path | P6 | E4 | |
| §16 | Temporal reconstruction | P6 | E4 | Both mechanisms |
| §17 | Audit / non-repudiation | P5 | E3 | WORM sink |
| §18 | Sequence diagrams | P10 | E5 | Video storyboard |
| §19 | Substrate mapping | P10 | E5 | README "why not Postgres" |
| §20 | Compute mapping | P1 | E5 | Infra scaffolding |
| §21 | Deployment topology | P1, P9 | E5 | Multi-region optional |
| §22 | Access control | P2 | E1 | |
| §23 | Observability | P7 | E5 | |
| §24 | Failure modes | P7 | All | Each row = a test |
| §25 | Evaluation plan | P8 | E5 | **Evidence** |
| §26 | Worked example | P10 | E5 | Demo script basis |
| §27 | Additional use cases | P10 | E5 | README |
| §28 V1–V6 | Verifications | P0 | All | **Gate** |
| §29 | Risks | All | Lead | Reviewed at each gate |
| §30 | Build sequence | This doc | — | Superseded by this doc |
| §31 | Scope boundaries | P9 | Lead | Cut decisions |
| §32 | Glossary | P10 | E5 | README |

**Explicitly deferred (in design doc, not built):** trained neural anomaly detection, production review UI, differential privacy, formal isolation verification, gate self-hardening. These are listed in §31 of the design doc as out of scope and remain out of scope here.

---

## 2. Phase 0 — Environment, Accounts, and Risk Retirement

**Duration target:** the first working session, before any application code.
**Why first:** §28 lists six unverified facts. Four of them can invalidate architectural decisions. V5 can invalidate the demo. Discovering this on day six is a project-ending event; discovering it on day one is a planning input.

### 2.1 Accounts to create

| Account | Purpose | Notes |
|---|---|---|
| CockroachDB Cloud | The substrate | Free tier; payment method may be required for the free allowance `[VERIFY]` |
| AWS | Compute, models, storage | Free tier |
| GitHub | Public repo | Must be public with a visible license |
| YouTube or Vimeo | Demo video | Must be public before submission |
| Devpost | Submission | Register before the deadline |

**Free-tier budget discipline (see §7 cost model).** Capture the actual resource allowance at signup and record it in `docs/BUDGET.md`. Track burn daily.

### 2.2 Local toolchain

```bash
# [EXACT] Verify base tooling
python3 --version          # expect 3.11+
git --version
docker --version           # optional, for local Postgres comparison harness

# [EXACT] Create working directory
mkdir -p ~/dev/pqbs && cd ~/dev/pqbs
```

```bash
# [VERIFY] CockroachDB CLI — check current install method in vendor docs
# macOS
brew install cockroachdb/tap/cockroach
# Linux (verify current URL and version before running)
# curl https://binaries.cockroachdb.com/cockroach-<VERSION>.linux-amd64.tgz | tar -xz

cockroach version
```

```bash
# [VERIFY] ccloud CLI — the agent-ready control plane tool
# Check vendor docs for current install command
brew install cockroachdb/tap/ccloud
ccloud version
ccloud auth login
```

```bash
# [VERIFY] AWS CLI
brew install awscli        # or per-platform installer
aws --version
aws configure              # region, credentials
```

```bash
# [EXACT] Node (only if using npx-based skill installation)
node --version             # expect 20+
```

### 2.3 Python environment

```bash
# [EXACT]
cd ~/dev/pqbs
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
python -m pip install --upgrade pip
```

```bash
# [EXACT] Core dependencies — pin resolved versions into requirements.txt
pip install \
  psycopg[binary]        `# Postgres-wire driver; CockroachDB is wire-compatible` \
  sqlalchemy             `# optional; raw SQL is fine and arguably clearer here` \
  alembic                `# migrations` \
  boto3                  `# AWS SDK` \
  pydantic               `# interface contracts — see §3` \
  python-dotenv \
  structlog              `# structured logging for §23` \
  pytest pytest-asyncio \
  numpy                  `# embedding math for signal S1` \
  fastapi uvicorn        `# demo API surface`

pip freeze > requirements.txt   # [PIN-ON-INSTALL]
```

**Driver note.** CockroachDB speaks the Postgres wire protocol, so standard Postgres drivers work. `[Certain]` The consequence that matters: **your driver will not retry serializable conflicts for you.** Retry logic is application responsibility and is implemented explicitly in Phase 3.

### 2.4 Cluster provisioning

```bash
# [VERIFY] Create a free-tier cluster via ccloud
ccloud cluster create serverless pqbs-dev --region <REGION> --plan basic

# [VERIFY] Retrieve connection string
ccloud cluster sql pqbs-dev --echo-sql

# [VERIFY] Create a SQL user and capture the connection URL
ccloud cluster user create pqbs-dev <USERNAME>
```

Store the connection string in `.env` (git-ignored). Never commit credentials — a public repo with a live database URL is a disqualification-grade mistake, not just a security one.

### 2.5 The six verification spikes

Each spike is a throwaway script in `spikes/`. **None of this code survives into the product.** The output of each spike is a paragraph in `docs/VERIFICATIONS.md` recording what was found and which fallback (if any) is now active.

---

**V5 — Serializable retry determinism** *(run this first; it is the exit gate)*

*Question:* Can we reliably force an observable serializable retry on demand?

*Method:* Two concurrent transactions that read the same row, then both write it. Instrument for the retryable error class.

```bash
# [EXACT]
mkdir -p spikes && touch spikes/v5_contention.py
python spikes/v5_contention.py
```

*What to record:* the exact SQLSTATE returned, whether it fires reliably across ≥20 runs, and the timing window required to force it.

*Pass criterion:* the retry fires on **≥90% of runs** with a scripted timing pattern.

*If it fails:* **stop and re-plan.** The demo's central moment does not exist. Fall back per §28 V5 — restructure the narrative around quarantine and temporal reconstruction, which are deterministic. This decision must be made now, not later.

---

**V1 — Vector index status and distance metrics**

*Question:* Is the vector index generally available or preview? Which distance metrics does the *index* support (as opposed to the vector type)? Dimension limits? Minimum row count before index creation?

*Method:* Create a table with a vector column, insert ≥200 rows of realistic dimensionality, create a prefixed vector index, run nearest-neighbour queries with each available distance operator. Check query plans to confirm the index is actually used rather than silently falling back to a scan.

*Pass criterion:* index creates, is used by the planner, and returns sane neighbours.

*Likely finding:* `[Likely]` cosine may be unsupported at the index level. If so, normalize all embeddings to unit length and use Euclidean — mathematically equivalent for unit vectors. **Record this in the README rather than glossing it.**

---

**V2 — Change feed availability and cost**

*Question:* Is log-driven change data capture available on this cluster tier? What sinks are supported? What does it cost against the free allowance?

*Method:* Enable a changefeed to a simple sink, generate 100 writes, confirm all 100 events arrive. Measure resource consumption before and after.

*Pass criterion:* all events arrive, latency is seconds not minutes, and cost extrapolates acceptably to demo volume.

*If it fails:* fall back to a polling worker over `status = 'pending'`. **This weakens the guarantee from "no committed write escapes screening" to "no write escapes screening within the poll interval."** That is a real degradation. Disclose it in the README; do not quietly ship a polling loop while claiming log-driven guarantees.

---

**V3 — MVCC retention window**

*Question:* How far back can as-of-timestamp reads reach on this tier? Is the retention setting configurable?

*Method:* Write a row, wait, modify it, then attempt as-of-timestamp reads at increasing distances into the past until they fail.

*Pass criterion:* reads succeed at least far enough back to cover the demo timeline (target: 30+ minutes).

*Consequence either way:* this bounds Mechanism 2 (design §16). Record the measured window and cite it in the README. **The design already anticipates this**, which is why bitemporal columns exist as the durable record — but the claim in the writeup must match the measurement.

---

**V4 — Managed access layer write semantics**

*Question:* What does the managed MCP server expose? Read-only by default? How is write access granted? What does the audit log record and in what format?

*Method:* Configure the MCP server per vendor docs, connect an agent client, attempt a read, attempt a write, retrieve the audit log.

*Pass criterion:* reads work; write-consent flow is understood; audit log is retrievable and contains agent attribution.

*If it fails:* fall back to direct connection with database-role enforcement. The governed-access narrative weakens; the security model holds.

---

**V6 — Row-level TTL behavior**

*Question:* Does TTL expiry run promptly enough to demonstrate? What is the job schedule?

*Method:* Create a table with a short TTL, insert rows, observe expiry timing.

*Pass criterion:* rows expire within a window short enough to show in a demo.

*If it fails:* explicit deletion job; the "forgetting" claim weakens from storage-enforced to policy-enforced.

---

### 2.6 Phase 0 Definition of Done

- [ ] All accounts created; free-tier allowance recorded in `docs/BUDGET.md`
- [ ] Local toolchain installed; versions captured
- [ ] Cluster provisioned; connection verified; credentials in `.env`, `.env` in `.gitignore`
- [ ] All six spikes run; `docs/VERIFICATIONS.md` written with findings and active fallbacks
- [ ] Any architectural changes forced by findings are reflected in the design doc

### 2.7 Phase 0 Exit Gate

> **V5 passes at ≥90% reliability, OR the team has explicitly decided and documented the alternative narrative.**

This is the only gate that can end the project. Everything else has a fallback; V5 determines whether the central demonstration exists.

---

## 3. Phase 1 — Repository Scaffolding and Interface Freeze

**Duration target:** short. This phase is plumbing, but the interface freeze at the end is what makes five-way parallelism possible.

### 3.1 Directory structure

```bash
# [EXACT]
cd ~/dev/pqbs

mkdir -p \
  src/pqbs/{substrate,agents,integrity,recall,audit,contracts,telemetry} \
  src/pqbs/agents/{producer,semantics,integrity,consumer,platform} \
  migrations/versions \
  spikes \
  tests/{unit,integration,contention,evaluation} \
  eval/{corpus,results} \
  infra/{lambda,worm,iam} \
  demo/{ui,scripts,seed} \
  docs/{diagrams,decisions} \
  scripts

touch README.md LICENSE .env.example .gitignore CHANGELOG-interfaces.md
```

Resulting layout:

```
pqbs/
├── README.md                    # Judged artifact — see Phase 10
├── LICENSE                      # MIT or Apache-2.0, visible in repo About
├── .env.example                 # Never .env
├── requirements.txt
├── docs/
│   ├── VERIFICATIONS.md         # Phase 0 findings
│   ├── BUDGET.md                # Free-tier tracking
│   ├── ARCHITECTURE.md          # Diagrams from design §7
│   ├── decisions/               # ADRs — one per non-obvious choice
│   └── diagrams/
├── migrations/
│   └── versions/                # Alembic revisions, one per table group
├── src/pqbs/
│   ├── contracts/               # Pydantic models — THE FROZEN INTERFACES
│   ├── substrate/               # Connection, retry, transaction helpers
│   ├── agents/
│   │   ├── producer/            # A1, A2, A3, A16
│   │   ├── semantics/           # A7, A11, A12, A8
│   │   ├── integrity/           # A4, A5, A6, A13, A14, A15
│   │   ├── consumer/            # A9, A10
│   │   └── platform/            # A17
│   ├── integrity/               # Signal implementations S1–S8
│   ├── recall/                  # Query path, filtering views
│   ├── audit/                   # WORM sink, temporal reconstruction
│   └── telemetry/               # Metrics, structured logging
├── infra/
│   ├── lambda/                  # Screening worker, cascade worker
│   ├── worm/                    # Object storage + retention config
│   └── iam/                     # Roles, policies
├── eval/
│   ├── corpus/                  # Benign, poison, evasion sets
│   └── results/                 # Measured numbers — judged evidence
├── demo/
│   ├── seed/                    # Deterministic demo data
│   ├── scripts/                 # Scripted demo moments
│   └── ui/                      # Minimal visual surface
├── tests/
│   ├── unit/
│   ├── integration/
│   ├── contention/              # The V5 harness, productionized
│   └── evaluation/
└── scripts/                     # Operational helpers
```

### 3.2 Git initialization

```bash
# [EXACT]
git init
cat > .gitignore <<'EOF'
.venv/
__pycache__/
*.pyc
.env
.env.local
*.log
eval/results/*.raw
.DS_Store
EOF

git add .
git commit -m "Phase 1: repository scaffolding"
gh repo create pqbs --public --source=. --push   # [VERIFY] gh CLI syntax
```

**License immediately.** `[Certain]` The submission rules require an open-source license detectable at the top of the repo page. Add it now, not at the end, because "add license" is exactly the task that gets forgotten at 2am.

```bash
# [EXACT]
curl -o LICENSE https://raw.githubusercontent.com/licenses/license-templates/master/templates/mit.txt
# Then edit the year and copyright holder
```

### 3.3 Interface contracts — the freeze

`src/pqbs/contracts/` defines every cross-owner boundary as a typed model. This is the highest-value artifact of Phase 1.

**Contracts to define (as data shapes, not implementations):**

| Contract | Produced by | Consumed by | Fields |
|---|---|---|---|
| `CandidateBelief` | A1/A2/A3/A16 | A11 | subject, predicate, object, confidence, valid_from, valid_to, provenance_stub, author_agent_id |
| `NormalizedBelief` | A11 | A12 | CandidateBelief + object_normalized, sensitivity |
| `EmbeddedBelief` | A12 | A7 | NormalizedBelief + embedding |
| `ProvenanceRecord` | producers | A4 | source_type, source_uri, source_digest, episode_id, derived_from, trust_tier |
| `ResolutionOutcome` | A7 | telemetry | resolution, basis, incumbent_id, challenger_id, retry_count |
| `ChangeEvent` | CDC | A4 | belief_id, tenant_id, operation, before, after, commit_timestamp |
| `SignalScore` | S1–S8 | A4 | signal_id, score, evidence, latency_ms |
| `Verdict` | A4 | A6, A14, audit | verdict, trust_score, signal_scores, triggering_rule, screener_version |
| `QuarantineRecord` | A4 | A6, A14 | belief_id, reason_code, quarantined_at |
| `CascadeRequest` | A6 | A4 | belief_ids, reason, depth |
| `RecallRequest` | caller | A9 | query, tenant_id, temporal_context, limit |
| `RecallResult` | A9 | caller | beliefs[], provenance[], trust_scores[], retrieval_id |
| `TemporalQuery` | caller | A10 | tenant_id, as_of, mechanism (bitemporal \| mvcc) |
| `AuditRecord` | all | WORM sink | event_type, agent_id, timestamp, before, after, reason |

**The `ChangeEvent` contract is the most important one to get right**, because it crosses the sync/async boundary between E1's world and E2's world. If E2 assumes it contains the full row and E1 emits only primary keys, Phase 4 stalls entirely.

```bash
# [EXACT]
git add src/pqbs/contracts/
git commit -m "Phase 1: interface contracts frozen"
git tag interface-freeze-v1
```

### 3.4 Configuration scaffolding

`.env.example` (committed) documents every required variable without values:

```
COCKROACH_URL=
AWS_REGION=
BEDROCK_MODEL_ID=
BEDROCK_EMBEDDING_MODEL_ID=
WORM_BUCKET=
CHANGEFEED_SINK_URL=
SCREENER_VERSION=
TENANT_ID_DEMO=
```

### 3.5 Phase 1 Definition of Done

- [ ] Directory structure created and committed
- [ ] Public GitHub repo exists with license visible in the About section
- [ ] All 14 contracts defined as typed models
- [ ] `interface-freeze-v1` tag pushed
- [ ] `.env.example` complete; `.env` git-ignored
- [ ] Each engineer can run `pytest` successfully against an empty test suite

### 3.6 Phase 1 Exit Gate

> **Every engineer can independently import the contracts and write a stub that satisfies them. No one is blocked on anyone else's implementation.**

---

## 4. Phase 2 — Substrate: Schema, Roles, Migrations

**Owner:** E1 (lead), E2/E3/E4 review their own tables
**Implements:** design §9 (all nine tables), §11 authority matrix, §22 access control, §8 lifecycle invariants

### 4.1 Migration strategy

```bash
# [EXACT]
alembic init migrations
# Configure migrations/env.py to read COCKROACH_URL from environment
```

One migration per logical group, in dependency order:

| Revision | Contents | Depends on |
|---|---|---|
| `0001_enums` | All enum types (status, source_type, trust_tier, reason_code, resolution, cardinality, disposition) | — |
| `0002_policy` | `predicate_policy` | 0001 |
| `0003_identity` | `agent_identity` | 0001 |
| `0004_provenance` | `provenance` | 0001 |
| `0005_belief` | `belief` + primary key + FK to provenance | 0004 |
| `0006_vector_index` | Prefixed vector index on `(tenant_id, embedding)` | 0005, **V1** |
| `0007_integrity` | `integrity_verdict`, `quarantine` | 0005 |
| `0008_contradiction` | `contradiction_event` | 0005 |
| `0009_retrieval_log` | `retrieval_log` | 0005 |
| `0010_working_memory` | `working_memory` + row-level TTL | 0001, **V6** |
| `0011_roles` | Four database roles + grants | all |
| `0012_views` | Role-scoped views enforcing status filtering | 0011 |

### 4.2 Encoding the lifecycle invariants as constraints

Design §8 defines a state machine. **It must be enforced by the database, not by agent discipline** (design principle P6). Specifically:

- A check constraint ensuring `status` is one of the six valid values.
- A check constraint ensuring producer-role inserts can only set `status = 'pending'`. `[VERIFY]` — if a check constraint cannot reference the current role, enforce via a role-specific view with `WITH CHECK OPTION` or an insert-only view.
- A constraint that `superseded_by` is non-null only when `status = 'superseded'`.
- A constraint that `trust_score` and `screened_at` are both null or both non-null.
- A constraint that `tx_to` non-null implies the belief is no longer current.

**These constraints are the difference between a design and a control.** If a compromised producer agent can write `status = 'trusted'` directly, the entire integrity path is decorative.

### 4.3 Roles and grants (design §22)

Four roles, created in `0011_roles`:

| Role | Grants |
|---|---|
| `role_producer` | INSERT on `belief` (via pending-only view), INSERT on `provenance` |
| `role_semantics` | UPDATE on supersession columns of `belief`, INSERT on `contradiction_event`, SELECT on trusted view |
| `role_integrity` | SELECT on all belief statuses, INSERT on `integrity_verdict` and `quarantine`, UPDATE on `belief.status` |
| `role_consumer` | SELECT **only** on the trusted-current view |
| `role_auditor` | SELECT on all tables including history |

**Verification test (belongs in `tests/integration/`):** connect as `role_consumer` and attempt to select a quarantined belief. **It must fail.** This test is the empirical proof of design §15's claim that filtering is structural rather than application-level, and it is worth showing in the video.

### 4.4 Seed data

`demo/seed/` contains deterministic seed data supporting the §26 worked example: the Northwind Logistics tenant, a predicate policy set, agent identities, and a baseline belief corpus of 2,000+ facts so the vector index is meaningful.

**Seed determinism matters.** The demo must be re-runnable from a clean state, because you will re-record the video more than once.

```bash
# [EXACT]
python scripts/seed_demo.py --tenant northwind --reset
```

### 4.5 Phase 2 Definition of Done

- [ ] All 12 migrations apply cleanly from empty
- [ ] All 12 migrations roll back cleanly
- [ ] Vector index created and confirmed used by the query planner (per V1 findings)
- [ ] Row-level TTL configured on `working_memory` (per V6 findings)
- [ ] All four roles created with grants
- [ ] **Negative test passes:** `role_consumer` cannot read quarantined content
- [ ] **Negative test passes:** `role_producer` cannot insert `status = 'trusted'`
- [ ] Seed script produces 2,000+ beliefs deterministically

### 4.6 Phase 2 Exit Gate

> **The authority matrix (design §11) is enforced by the database. Every "—" in that table has a corresponding failing negative test.**

---

## 5. Phase 3 — Write Path

**Owner:** E1
**Implements:** design §13 (all 11 steps), agents A1, A3, A7, A11, A12

### 5.1 Build order within the phase

1. **Connection + retry helper** (`substrate/`). The retry wrapper is the single most reused component; build it first and test it against the V5 harness.
2. **A11 Canonicalization.** Predicate-specific normalization rules driven by `predicate_policy.normalization_rule`.
3. **A12 Embedding.** Wraps the embedding model. **Enforces the invariant that write-path and recall-path use identical model configuration** — expose one function used by both, not two call sites.
4. **A7 Resolution.** The contradiction detection and supersession logic, inside the serializable transaction.
5. **A1 Ingestion.** Extraction from source content into candidate triples.
6. **A3 Correction.** Explicit-invalidation path.

### 5.2 The retry wrapper — critical detail

`[Certain]` Serializable conflicts surface as a specific retryable error class that the application must handle. The wrapper must:

- Catch **only** the retryable class; other errors propagate immediately.
- Re-execute the **entire transaction body**, including the reads. A retry that reuses stale read results defeats the purpose — this is the most common implementation error and it silently reintroduces the anomaly the design exists to prevent.
- Apply bounded exponential backoff with jitter.
- Count attempts and surface `retry_count` for `contradiction_event`.
- Fail with an explicit contention error on exhaustion. **Never fall back to a weaker isolation level** (design §24).

**Test:** run the wrapper against the V5 contention harness and assert the final state matches the design §26.7 expectation — a total-ordered supersession chain with nothing lost.

### 5.3 Resolution precedence (design §13 Step 8)

Implement the precedence exactly as specified, in order:

1. `explicit_invalidation` — always wins
2. `source_tier` — authoritative beats unverified regardless of recency
3. `recency` — later `valid_from` wins
4. `confidence` — **tiebreak only, never primary**
5. Undecidable → `deferred`, both retained, drift notified

**Write the `contradiction_event` row regardless of outcome**, including when the incumbent is retained. This is the design's answer to silent last-write-wins and it must not be optimized away as "nothing changed, nothing to log."

### 5.4 Cardinality handling

`predicate_policy.cardinality` gates contradiction detection entirely. `multi_valued` predicates skip resolution. Getting this wrong produces one of two failures:

- Treating everything as single-valued → false contradictions everywhere, supersession chains that destroy legitimate parallel facts.
- Treating everything as multi-valued → contradiction detection never fires, and the project's central demo does not work.

**Test both directions explicitly.**

### 5.5 Phase 3 Definition of Done

- [ ] Retry wrapper handles serializable conflicts correctly; re-reads on retry
- [ ] Contention harness produces a clean total-ordered chain with 8+ concurrent writers
- [ ] Canonicalization collapses value variants ("Gold" / "gold tier" / "GOLD")
- [ ] Ambiguous canonicalization sets `sensitivity = elevated` rather than guessing
- [ ] Embedding produced pre-transaction (verified by trace inspection)
- [ ] All five resolution bases exercised by tests
- [ ] `contradiction_event` written on `incumbent_retained` outcomes
- [ ] Every belief enters as `pending`; no path writes `trusted`
- [ ] Write path p50 < 400 ms, p99 < 1200 ms

### 5.6 Phase 3 Exit Gate

> **Design §26.7 (the concurrency moment) reproduces end-to-end: three writers, deterministic chain, nothing lost, retry counted.**

---

## 6. Phase 4 — Integrity Path: CDC and the Gate

**Owner:** E2
**Implements:** design §14, agent A4, signals S1–S8

### 6.1 CDC wiring

Per V2 findings, either:
- **Log-driven changefeed** to a sink that triggers the screening worker, or
- **Polling fallback** over `status = 'pending'` with a documented interval

```bash
# [VERIFY] Changefeed creation — check current syntax and supported sinks
# Sink target depends on V2 findings: webhook to Lambda URL, or object storage
```

**Idempotency is mandatory.** Change feeds may deliver duplicates. The screening worker must produce the same verdict for a repeated event without appending a duplicate verdict row — key on `(belief_id, screener_version)` for the initial screening.

### 6.2 Signal implementation order

Build in this order — cheapest and most legible first, so the gate is demonstrable before it is complete:

| Order | Signal | Why this position |
|---|---|---|
| 1 | **S2 source trust tier** | Pure lookup; no model call; immediately demonstrable |
| 2 | **S3 imperative content** | The most legible signal in a demo — "this is an order, not a fact" |
| 3 | **S7 derivation integrity** | Pure graph check; enables Phase 5 cascade |
| 4 | **S6 corroboration diversity** | Requires the `source_digest` independence check |
| 5 | **S1 embedding anomaly** | Requires distribution statistics over the corpus |
| 6 | **S5 contradiction burst** | Requires windowed aggregation |
| 7 | **S4 author behavior** | Requires `behavior_baseline` accumulation |
| 8 | **S8 temporal plausibility** | Lowest marginal value; build last |

**After signals 1–3, the gate works and the demo is possible.** Everything after that is depth.

### 6.3 S3 — imperative content detection

Worth a specific note because it is the demo's most legible moment. The signal distinguishes *assertion* from *instruction*:

- Assertion: "prefers overnight delivery", "is on the premium plan"
- Instruction: "should always be routed to expedited", "verification may be skipped"

Implementation approach: a model call with a focused prompt classifying the object text, plus a cheap lexical prefilter for high-signal imperative markers. **Record the classification rationale in `signal_scores`** — design principle P8 requires explainability, and "the model said no" is not an auditable verdict.

### 6.4 Verdict composition

- Compose signals into a trust score. Document the weighting in `docs/decisions/`.
- Above trust threshold → `trusted`
- Below quarantine threshold → `quarantined` with reason code
- Between → `inconclusive`, stays `pending`, queued for review

**`inconclusive` resolves to unusable.** This is the fail-closed principle at its sharpest and must be tested explicitly: an inconclusive belief must not be retrievable via `role_consumer`.

### 6.5 Fail-closed enforcement test

The most important test in the phase:

1. Kill the screening worker.
2. Write 10 beliefs.
3. Assert all 10 are `pending`.
4. Assert `role_consumer` retrieves **zero** of them.
5. Restart worker; assert they screen and become retrievable.

This test *is* design §24's "screening worker down" row, and it is worth showing in the video.

### 6.6 Phase 4 Definition of Done

- [ ] CDC delivers 100% of committed writes to the screener (or polling fallback documented)
- [ ] Screening worker is idempotent on duplicate delivery
- [ ] Signals S2, S3, S7 implemented with recorded per-signal scores
- [ ] Verdict composition produces all three outcomes on test data
- [ ] `integrity_verdict` rows contain full signal breakdown
- [ ] **Fail-closed test passes**
- [ ] Screening lag p50 < 5 s, p99 < 15 s

### 6.7 Phase 4 Exit Gate

> **A poisoned belief written by a producer never becomes retrievable, and the verdict explains why in per-signal detail.**

---

## 7. Phase 5 — Containment: Cascade, Quarantine, Review, Audit

**Owner:** E3
**Implements:** design §14.4, §17, agents A2, A6, A14

### 7.1 A2 Inference — built here, not in Phase 3

A2 belongs in this phase because **its only purpose in the demo is to make cascade demonstrable.** Building it earlier adds surface area without adding a demonstrable property.

A2 reads trusted beliefs, derives new ones, and **must** populate `derived_from`. A test asserts that a derivation with empty `derived_from` is rejected.

### 7.2 A6 Cascade

On quarantine, traverse the `derived_from` graph transitively and request re-screening of every descendant.

**Two non-negotiable properties:**
- **Idempotent** — the same quarantine event processed twice produces the same result.
- **Cycle-safe** — `[Certain]` derivation graphs are not reliably acyclic. An unguarded traversal hangs. Maintain a visited set; on cycle detection, halt and flag for review.

**Record cascade depth.** A quarantine with depth 40 is a very different incident from one with depth 0, and the metric is worth surfacing.

### 7.3 A14 Review disposition

- Presents quarantined and inconclusive items with their evidence.
- Records disposition (`released` / `rejected`) with reviewer identity.
- **Release requires a recorded reviewer.** No autonomous release, no timeout-to-release. Held is the safe state (design §24).
- Every disposition writes an audit record.

Minimal UI is acceptable — a table with two buttons. The design explicitly scopes out a production review UI.

### 7.4 WORM audit sink

```bash
# [VERIFY] Object storage bucket with retention lock enabled
aws s3api create-bucket --bucket pqbs-audit-<suffix> --region <REGION>
aws s3api put-object-lock-configuration --bucket pqbs-audit-<suffix> \
  --object-lock-configuration '<CONFIG>'
```

Every state transition emits an audit record: creation, supersession, verdict, quarantine, release, rejection. Each carries agent identity, timestamp, before/after, and reason.

**Verification test:** attempt to delete or overwrite an audit object. **It must fail.** This is the empirical proof of non-repudiation and takes four seconds of video.

### 7.5 Design §24's hardest row

"Audit sink unavailable → belief writes blocked." Implement it. It is a deliberate availability sacrifice and the row a reviewer will attack — having it actually implemented, with a test, is much stronger than having it described.

### 7.6 Phase 5 Definition of Done

- [ ] A2 populates `derived_from`; empty derivations rejected
- [ ] Cascade re-screens 100% of descendants (completeness metric = 100%)
- [ ] Cascade is idempotent and cycle-safe (tested with a deliberate cycle)
- [ ] Review requires reviewer identity for release
- [ ] WORM bucket configured; **delete/overwrite attempt fails**
- [ ] Audit records emitted for all six transition types
- [ ] Audit-sink-unavailable behavior implemented and tested

### 7.7 Phase 5 Exit Gate

> **Design §26.8 reproduces: quarantining a parent causes every derived belief to leave `trusted` automatically, with the cascade depth recorded.**

---

## 8. Phase 6 — Recall and Audit Surface

**Owner:** E4
**Implements:** design §15, §16, agents A9, A10

### 8.1 A9 Recall

Five steps per design §15. The critical implementation detail:

**Filtering happens in the role-scoped view, not in application code.** If `role_consumer`'s view exposes only trusted-current beliefs, then no application bug — and no attacker who fully rewrites the application — can retrieve quarantined content. This is design principle P6 made concrete, and it is the difference between a policy and a control.

`retrieval_log` records what was **actually returned**, not what existed. Post-incident analysis needs this; without it, "what was in context when it decided that" is unanswerable even with perfect belief history.

### 8.2 A10 Audit — both temporal mechanisms

**Mechanism 1 (bitemporal, unbounded).** Filter on transaction-time columns. Works arbitrarily far back. **This is the product.**

**Mechanism 2 (MVCC as-of-timestamp, bounded).** Reconstructs the exact committed snapshot. Bounded by the retention window measured in V3.

**Both must be implemented, and the README must distinguish them.** `[Certain]` MVCC history is compacted after the retention period. Claiming arbitrary historical replay via Mechanism 2 is the single most likely factual error in the submission, and a knowledgeable reviewer will catch it. Design §16 already anticipates this; the implementation and the writeup must match.

A10 also answers attribution queries: who wrote this, why was it quarantined, what changed between T1 and T2, what did this belief influence (join `retrieval_log`).

### 8.3 Demo surface

`demo/ui/` — minimal. Enough to show:
- A belief table with visible status
- The quarantine list with reason codes and signal breakdowns
- A temporal query control ("show me state as of…")
- Live screening lag

Resist building more. The video is the deliverable, not the UI.

### 8.4 Phase 6 Definition of Done

- [ ] Recall returns only trusted-current beliefs, enforced at the view layer
- [ ] `role_consumer` cannot bypass filtering even with arbitrary SQL
- [ ] `retrieval_log` populated on every recall
- [ ] Mechanism 1 answers queries beyond the MVCC window
- [ ] Mechanism 2 answers queries within the window; fails gracefully beyond it
- [ ] Attribution queries return agent identity and provenance chain
- [ ] Recall latency p50 < 600 ms

### 8.5 Phase 6 Exit Gate

> **Design §26.9 reproduces: a bitemporal query answers "what did it believe on day 5" *after* the MVCC window has aged out, and the failure of Mechanism 2 at that range is visible and explained rather than hidden.**

---

## 9. Phase 7 — Depth: Remaining Signals, Drift, Observability

**Owner:** E2 (signals), E3 (drift), E5 (observability)
**Implements:** design §14.2 signals S1/S4/S5/S6/S8, agent A5, A17, §23, §24

### 9.1 Remaining signals

Complete S1, S4, S5, S6, S8 per §6.2 ordering. Each addition must be accompanied by a re-run of the Phase 8 evaluation to confirm it improves detection without inflating false positives — **an added signal that raises the false-positive rate without raising detection is a net negative** and should be reverted.

### 9.2 A5 Drift detection

Scheduled population-level analysis. Detects what per-write screening structurally cannot:
- Contradiction bursts within a predicate (T5)
- Agents whose write character has shifted (T1, T7)
- Clusters of semantically similar beliefs sharing a source origin (T4)
- Sleeper patterns surfacing over time (T3)

Outputs re-screening requests and `trust_multiplier` adjustments. **May not quarantine directly** — authority matrix.

### 9.3 A17 Telemetry and §23 metrics

Implement all four metric families. The two that matter most, per design §23.1:

- **Screening lag** — the width of the fail-closed window
- **Retry rate** — the measured cost of serializable isolation

**Report both honestly in the README.** A reviewer who asks "what does fail-closed cost you?" should get a number.

Traces span the full belief lifecycle: ingestion → canonicalization → embedding → commit → change event → verdict → first retrieval.

### 9.4 Failure mode tests

Design §24 has thirteen rows. Each becomes an integration test:

| Row | Test |
|---|---|
| Screening worker down | Phase 4 test (already exists) |
| CDC lag | Inject delay; assert alert fires, trusted beliefs unaffected |
| Embedding service down | Assert write rejected, recall unaffected |
| Retry exhaustion | Force; assert explicit error, no isolation downgrade |
| Contradiction unresolvable | Assert `deferred`, both retained, drift notified |
| Quarantine false positive | Assert review can release, release audited |
| Cascade cycle | Deliberate cycle; assert halt and flag |
| Review queue unattended | Assert indefinite hold, no timeout-release |
| Audit sink unavailable | Assert writes blocked |
| Canonicalization ambiguous | Assert `elevated` sensitivity |
| Federation unverifiable | Assert rejection (if A16 built) |
| Tenant isolation | **Adversarial test** — attempt cross-tenant retrieval |

**The tenant isolation row deserves the most attention.** Design §24 claims it is "structurally prevented." That is a strong claim, and the design doc itself notes it should be tested adversarially rather than asserted.

### 9.5 Phase 7 Definition of Done

- [ ] All eight signals implemented
- [ ] Each signal's marginal contribution measured against the eval corpus
- [ ] A5 drift detection running on schedule
- [ ] All four metric families instrumented
- [ ] Full-lifecycle traces working
- [ ] All 13 failure-mode tests pass

---

## 10. Phase 8 — Evaluation and Red Team

**Owner:** E5
**Implements:** design §25 in full, agent A15

**This phase produces the project's evidence.** Without it, the submission claims a defense with no measurement that it defends anything.

### 10.1 Isolated evaluation tenant

A15 operates in a separate tenant with producer authority. **Structurally cannot write to demo or production tenants** — enforced by tenant scoping, not by discipline.

```bash
# [EXACT]
python scripts/seed_eval_tenant.py --tenant eval --reset
```

### 10.2 Corpus construction

**Benign set — ≥200 beliefs.** Realistic facts across the predicate space, varied legitimate sources, **including legitimate contradictions** (genuine corrections and updates). Measures false positives, which is the most likely reason a real deployment would disable the gate.

**Poison set — ≥100 beliefs, stratified:**

| Class | Construction |
|---|---|
| T1 direct | False facts written by an ostensibly-legitimate agent |
| T2 environmental | False facts embedded in ingested document content |
| T3 sleeper | Benign-appearing facts with delayed semantic triggers |
| T4 corroboration | Clusters of mutually-supporting false facts from one origin |
| T8 derivation | Legitimate inferences from a poisoned parent |

**Evasion set — ≥50.** Poison specifically constructed to defeat each individual signal: declarative phrasing to evade S3, authoritative-looking provenance to evade S2, embeddings positioned near the legitimate cluster to evade S1. **This set measures whether signal composition actually resists evasion or merely appears to.**

### 10.3 Metrics

| Metric | Definition |
|---|---|
| Detection rate | Poison quarantined / total poison — **report per threat class** |
| False positive rate | Benign quarantined / total benign |
| Evasion resistance | Evasion-set detection rate |
| Cascade completeness | Descendants re-screened / total descendants — should be 100% |
| Time to quarantine | Commit → quarantine, p50/p99 — the exposure window |
| Contradiction correctness | Resolutions matching expectation under known ordering — should be 100% |

Results written to `eval/results/` as committed artifacts. **These numbers go in the README.**

### 10.4 The concurrency correctness test

Separate from poison detection and arguably more important, because it tests the foundational claim.

**Procedure:** N concurrent writers assert conflicting values for the same subject-predicate. After quiescence, assert:
1. Exactly one belief is `trusted` (single-valued case)
2. The supersession chain is a total order with no forks
3. Every write appears exactly once in the chain or in `contradiction_event`
4. Nothing was lost

**Then run the identical harness against READ COMMITTED and demonstrate the failure.**

```bash
# [EXACT]
python -m tests.contention.compare --isolation serializable --writers 16
python -m tests.contention.compare --isolation read_committed --writers 16
```

`[Likely]` This comparison is the single most valuable artifact the project can produce. It converts "CockroachDB is load-bearing" from an argument into a measurement, and it is the empirical core of the "why not Postgres" section.

### 10.5 Honest reporting

`[Likely]` A heuristic gate will show poor evasion resistance. **Report it.** A measured weakness is credible; a suspiciously high detection rate across every class is not. State plainly that the gate is heuristic rather than a trained detector, and that the evaluation measures a hackathon-scale implementation.

### 10.6 Phase 8 Definition of Done

- [ ] Three corpora built, committed to `eval/corpus/`
- [ ] All six metrics measured and written to `eval/results/`
- [ ] Concurrency correctness test passes under serializable
- [ ] **READ COMMITTED comparison demonstrates the failure**
- [ ] Results summarized in README with honest framing

---

## 11. Phase 9 — Optional Extensions and Scope Decisions

**Owner:** Lead, with §12 collapse plan in hand
**Implements:** design §12, §31, agents A8, A13, A16; multi-region topology

### 11.1 The scope decision checkpoint

Before starting anything in this phase, review design §12's collapse table against actual remaining capacity. **The default is to build nothing here.**

| Extension | Build if | Skip if |
|---|---|---|
| Multi-region REGIONAL BY ROW | Budget healthy, ≥1 day spare | Any doubt about free-tier burn |
| A16 Federation | Cross-org narrative adds to the pitch | Time-constrained |
| A13 Rate limiting | Production-readiness claim needs T10 covered | Demo doesn't show it |
| A8 Consolidation | Forgetting story needs more than TTL | TTL already demonstrates it |

### 11.2 Multi-region, if built

```bash
# [VERIFY] Add regions to the cluster
ccloud cluster region add pqbs-dev --region <REGION2>
ccloud cluster region add pqbs-dev --region <REGION3>
```

Then convert `belief` to a regional-by-row table with tenant-based homing. **Cross-region traffic is resource-expensive.** Add last, record the demo shot, and consider tearing down after.

### 11.3 Phase 9 Definition of Done

- [ ] Explicit written decision on each extension, recorded in `docs/decisions/`
- [ ] Anything built is tested to the same standard as core phases
- [ ] Budget re-checked after any multi-region work

---

## 12. Phase 10 — Submission Assets

**Owner:** E5 (lead), all contribute
**Implements:** design §5, §7, §18, §19, §26, §27, §32

**Start this phase earlier than feels comfortable.** `[Likely]` Judges may grade from the video and README alone without running the code, which makes these artifacts weight-bearing rather than cosmetic.

### 12.1 README structure

The README is a judged artifact. Structure:

1. **One-sentence pitch** — "most memory systems solve remembering; this one solves whether what you remember can be trusted"
2. **The problem** — condensed design §3, with the OWASP/attack-paper citations
3. **Architecture diagram** — from design §7
4. **How it works** — the three paths, briefly
5. **What's prior art and what isn't** — condensed design §5. **State the Graphiti overlap explicitly.** A reviewer who discovers it themselves concludes derivative; a reviewer told up front concludes the authors know the landscape.
6. **Why not single-node Postgres** — design §19.1's three legs, **backed by the Phase 8 measurement**
7. **Evaluation results** — the honest numbers, including the bad ones
8. **CockroachDB tools used and how** — required by submission rules; be specific about what the agent actually did with each
9. **AWS services used and how** — required by submission rules
10. **Known limitations** — MVCC window bound, heuristic gate, any active fallbacks from Phase 0
11. **Setup and run instructions** — must actually work from a clean clone
12. **Glossary** — design §32

### 12.2 Setup instructions must be tested

```bash
# [EXACT] Clean-room verification — run on a machine that has never seen the project
git clone <repo-url> /tmp/pqbs-clean && cd /tmp/pqbs-clean
# Follow the README verbatim. Any step that fails is a README bug.
```

Assign this to whoever wrote the *least* of the setup code. Familiarity hides gaps.

### 12.3 Video storyboard

Under three minutes. Derived from design §18 sequence diagrams and §26's worked example.

| Time | Shot | Proves |
|---|---|---|
| 0:00–0:20 | The problem: a poisoned note in a shared notebook, firing weeks later | Motivation |
| 0:20–1:00 | **Concurrency:** two agents write contradictory facts; retry fires; clean chain; nothing lost. Split-screen with READ COMMITTED losing a write | Memory Design + why-not-Postgres |
| 1:00–1:40 | **Poison quarantine:** imperative-content injection → gate catches → quarantine with signal breakdown → cascade | The contribution |
| 1:40–2:10 | **Structural invisibility:** recall query returns correct answer; the poisoned belief is not merely filtered but unreachable by role | Production readiness |
| 2:10–2:40 | **Temporal reconstruction:** "what did it believe at T" — bitemporal answer; WORM audit record; delete attempt fails | Audit + non-repudiation |
| 2:40–3:00 | Architecture diagram + evaluation numbers + one-line pitch | Close |

**Record with seeded, deterministic data.** Every moment must be reproducible on demand, because you will re-record.

### 12.4 Submission checklist

- [ ] Repo public
- [ ] License file present and **visible in the repo About section**
- [ ] README complete, setup instructions verified from clean clone
- [ ] Demo app URL live and reachable
- [ ] Video under 3 minutes, public on YouTube or Vimeo
- [ ] Video shows the memory layer at work (explicit rule requirement)
- [ ] Written identification of ≥2 CockroachDB tools and how they were used
- [ ] Written identification of ≥1 AWS service and how it was used
- [ ] Architecture diagram included (optional per rules, but do it)
- [ ] All dependencies and example configuration in repo
- [ ] No credentials committed — **audit git history, not just the working tree**

---

## 13. Cost Model

Design doc gap. Track in `docs/BUDGET.md`, updated daily.

| Cost driver | Consumption pattern | Control |
|---|---|---|
| Belief writes | Per write, small | Bounded by demo corpus size |
| Vector index maintenance | Per write; index build is bursty | Build once on seeded corpus, avoid rebuilds |
| Vector search | Per recall query | Small |
| Changefeed | **Continuous while enabled** | Highest sustained risk — disable when not testing |
| Row-level TTL job | Periodic scan | Keep `working_memory` small |
| Multi-region traffic | **Per cross-region operation** | Add last; tear down after recording |
| Model inference (extraction) | Per ingested source | Cache aggressively during development |
| Model inference (embedding) | Per belief + per query | Cache embeddings by content hash |
| Model inference (screening S3) | Per screened belief | Lexical prefilter before model call |
| Object storage (WORM) | Per audit record; retention-locked | **Cannot be deleted** — size the retention window deliberately |

**Two specific traps:**
1. **A leftover changefeed running overnight** is the most likely way to burn the allowance without noticing. Add a teardown step to every test session.
2. **WORM objects cannot be deleted** — that is the point of them. Do not write test audit data into the retention-locked bucket. Use a separate non-locked bucket for development.

**Daily check:**
```bash
# [VERIFY] Check consumption against allowance
ccloud cluster usage pqbs-dev
```

---

## 14. Integration Checkpoints

Parallel work across five owners needs scheduled convergence. Four checkpoints:

**CP1 — after Phase 2.** Everyone connects to the shared schema and runs their negative tests. Catches schema misunderstandings before code depends on them.

**CP2 — after Phase 4.** E1's write path feeds E2's gate end-to-end. **This is the highest-risk integration** because it crosses the sync/async boundary and depends on the `ChangeEvent` contract being right.

**CP3 — after Phase 6.** Full path: write → screen → cascade → recall → audit. First end-to-end demo rehearsal.

**CP4 — after Phase 8.** Evaluation numbers reviewed as a group. Decide what goes in the README and how to frame the weak results.

---

## 15. Risk Register (build-specific)

Design §29 covers design risks. These are execution risks.

| Risk | Trigger | Response |
|---|---|---|
| V5 fails | Phase 0 | **Stop. Re-plan narrative.** Documented alternative required before proceeding. |
| CDC unavailable on tier | Phase 0 V2 | Polling fallback; disclose the weakened guarantee in README |
| Retry wrapper reuses stale reads | Phase 3 | Silent correctness failure — the contention test catches it. **Do not skip that test.** |
| Cardinality misconfigured | Phase 3 | Contradiction detection never fires; demo doesn't work. Test both directions. |
| Cascade hangs on cycle | Phase 5 | Deliberate-cycle test before integration |
| Embedding model mismatch write vs. recall | Phase 6 | Silent recall degradation with no error. Single shared function; assert config equality in a test. |
| Free-tier exhaustion | Any | Daily budget check; changefeed teardown discipline |
| Credentials in git history | Any | Pre-commit hook; audit history before making repo public |
| README instructions don't work | Phase 10 | Clean-room test by someone who didn't write them |
| Video re-record needed late | Phase 10 | Deterministic seed data from Phase 2 makes this cheap |
| Evaluation shows weak results | Phase 8 | **Report honestly.** Frame as measured limitation of a heuristic gate. |

---

## 16. Definition of Done — Project

The project is complete when:

- [ ] Every "spine" component (A4, A6, A7, A9, A10) is implemented and tested
- [ ] Every design §11 authority-matrix restriction has a failing negative test
- [ ] Every design §24 failure mode has a passing integration test
- [ ] Design §25 evaluation is run and results are committed
- [ ] The READ COMMITTED comparison demonstrates the anomaly serializable prevents
- [ ] Design §26's worked example reproduces end-to-end from seed
- [ ] All Phase 0 verification findings are reflected in both code and README
- [ ] Submission checklist (§12.4) is fully green
- [ ] No claim in the README exceeds what the implementation actually does

**The last item is the one to guard.** `[Likely]` The gap between what a submission claims and what its code does is the first thing a technical reviewer probes, and it is the cheapest possible way to lose points on a project that otherwise earned them.
