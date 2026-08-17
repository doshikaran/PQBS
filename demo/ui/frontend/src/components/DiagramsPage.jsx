import React, { useState } from 'react'
import { Link } from 'react-router-dom'

const SYSTEM_CONTEXT = `
                    ┌────────────────────────────────────────────────────┐
                    │                                                    │
  AI Agent ────────►│                                                    │
  (writes beliefs)  │                                                    │
                    │                  P Q B S                           │
  AI Agent ◄────────│                                                    │
  (reads trusted    │       Poison-Quarantine Belief Store               │
   beliefs only)    │                                                    │
                    │   CockroachDB Serverless  ×  AWS                   │
  Human Reviewer ──►│                                                    │
  (releases /       │                                                    │
   rejects held)    │                                                    │
                    │                                                    │
  Security Auditor ►│                                                    │
  (reconstructs     └────────────────────────────────────────────────────┘
   past beliefs)             │               │               │
                             │               │               │
                             ▼               ▼               ▼
                    CockroachDB         AWS Lambda       Amazon S3
                    Serverless          (Screener)      WORM Bucket
                    (belief store,      CDC-triggered   (immutable
                    vector index,       integrity gate  audit trail)
                    MVCC audit)

  External actors:
  ┌──────────────────┬──────────────────────────────────────────────────┐
  │ Actor            │ What they do                                     │
  ├──────────────────┼──────────────────────────────────────────────────┤
  │ AI Agent (write) │ Calls ingest API — result is always PENDING      │
  │ AI Agent (read)  │ Calls recall API — receives only TRUSTED beliefs │
  │ Human Reviewer   │ Releases or rejects QUARANTINED items            │
  │ Security Auditor │ Reads WORM trail; runs bitemporal/MVCC queries   │
  │ Attacker         │ Tries to inject false beliefs via write path     │
  │ Engineer / Admin │ Runs infra scripts; monitored by A18 posture     │
  └──────────────────┴──────────────────────────────────────────────────┘
`.trim()

const DATA_FLOW = `  
    ┌──────────────────────────────────────────────────────────────────────┐
    │  STAGE 1 — INGEST                                                    │
    │                                                                      │
    │  Raw text / document                                                 │
    │  ──────────────────────────────────────────────────────────────────► │
    │                                                                      │
    │  A1 IngestionAgent              [Bedrock Claude 3.5 Sonnet]          │
    │    → extract (subject, predicate, object) triple                     │
    │    → assign source_type, source_uri, SHA-256 digest                  │
    │                                                                      │
    │  A11 CanonicalizationAgent                                           │
    │    → normalize (10 rules) · ambiguity → sensitivity = ELEVATED       │
    │                                                                      │
    │  A12 EmbeddingAgent             [Bedrock Titan Embed v2 1024-dim]    │
    │    → compute embedding BEFORE transaction opens                      │
    └────────────────────────────────┬─────────────────────────────────────┘
                                     │
                                     ▼
    ┌─────────────────────────────────────────────────────────────────────┐
    │  STAGE 2 — RESOLVE  (SERIALIZABLE transaction)                       │
    │                                                                      │
    │  A7 ResolutionAgent — retried on SQLSTATE 40001                      │
    │                                                                      │
    │  SELECT incumbent · re-read on every retry                           │
    │    ├── No incumbent  → INSERT new belief (status = PENDING)          │
    │    └── Incumbent exists →                                            │
    │          Compare: explicit_invalidation > source_tier > recency      │
    │          INSERT contradiction_event (winner AND loser recorded)      │
    │          Winner committed · loser tx_to closed or discarded          │
    │                                                                      │
    │  INSERT provenance row · COMMIT                                      │
    └────────────────────────────────┬─────────────────────────────────────┘
                                    │  belief committed: status = PENDING
                                    │
                            CockroachDB CDC changefeed
                            webhook → AWS Lambda Function URL
                                    │
                                    ▼
    ┌─────────────────────────────────────────────────────────────────────┐
    │  STAGE 3 — SCREEN  (Lambda, async, fail-closed)                      │
    │                                                                      │
    │  A4 ScreeningGate — 8 signals:                                       │
    │                                                                      │
    │  S1 embedding anomaly ─── AVG(embedding) over cluster [CockroachDB]  │
    │  S2 source trust tier ─── lookup source_trust_tier column            │
    │  S3 imperative content ── [Bedrock Llama 3 70B classification]       │
    │  S4 author burst ──────── rolling count over agent_identity          │
    │  S5 contradiction burst ── windowed count over contradiction_event   │
    │  S6 source diversity ───── distinct source_digest count              │
    │  S7 derivation integrity ── parent belief status check               │
    │  S8 temporal plausibility ─ validity window sanity                   │
    │                                                                      │
    │  trust_score = Σ(weight_i × signal_i)                                │
    │                                                                      │
    │  score ≤ 0.40 ──────────────────────────────────► TRUSTED            │
    │  score ≥ 0.70 ──────► QUARANTINED ─────────────► A6 Cascade BFS      │
    │  0.40 < score < 0.70 ──────────────────────────► INCONCLUSIVE        │
    │                                                                      │
    │  AuditRecord → S3 WORM bucket (every verdict, every state change)    │
    └────────────────────────────────┬─────────────────────────────────────┘
                                    │  status updated in CockroachDB
                                    ▼
    ┌─────────────────────────────────────────────────────────────────────┐
    │  STAGE 4 — RECALL  (dual enforcement)                                │
    │                                                                      │
    │  Query → A12 embed → 1024-dim vector [Bedrock Titan Embed v2]        │
    │        → KNN via HNSW index (tenant_id prefix-partitioned)           │
    │        → JOIN v_trusted_current (status='trusted' AND tx_to IS NULL) │
    │                                                                      │
    │  Layer 1: DB role-scoped view (role_consumer cannot see belief table)│
    │  Layer 2: MCP Server — read-only protocol enforced                   │
    │                                                                      │
    │  → RecallResult + retrieval_log row                                  │
    └──────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
    ┌─────────────────────────────────────────────────────────────────────┐
    │  STAGE 5 — AUDIT  (temporal reconstruction)                          │
    │                                                                      │
    │  Mechanism 1 — Bitemporal                                            │
    │    WHERE tx_from <= $t AND (tx_to IS NULL OR tx_to > $t)             │
    │    Works for any historical timestamp. No retention limit.           │
    │                                                                      │
    │  Mechanism 2 — MVCC AS OF SYSTEM TIME '-30m'                         │
    │    Bounded by MVCC GC window (~30 min on Serverless free tier)       │
    │                                                                      │
    │  Mechanism 3 — ccloud backup catalog  (A19 SubstrateCustodyAgent)    │
    │    Covers history beyond MVCC GC window                              │
    └──────────────────────────────────────────────────────────────────────┘
`.trim()

const STATE_MACHINE = `
   External write (any agent)
              │
              │  A1 ingest → A11 canonicalize → A12 embed
              │  A7 resolve (SERIALIZABLE) → INSERT
              │  CHECK: status != 'trusted' at insert time  ← Security Invariant 1
              ▼
   ┌──────────────────┐
   │                  │  ← role_consumer CANNOT see this state
   │     PENDING      │  ← v_trusted_current excludes this state
   │                  │  ← ALL beliefs start here, no exceptions
   └────────┬─────────┘
            │
            │  CockroachDB CDC changefeed fires
            │  → AWS Lambda handler.py receives ChangeEvent
            │  → A4 ScreeningGate runs signals S1–S8
            │
            ├───────────────────────────────────────┐
            │  trust_score ≤ 0.40                   │  trust_score ≥ 0.70
            ▼                                       ▼
   ┌──────────────────┐               ┌──────────────────────┐
   │                  │               │                      │
   │     TRUSTED      │               │    QUARANTINED       │
   │                  │               │                      │
   │  retrievable via │               │  INSERT quarantine   │
   │  v_trusted_      │               │  row; invisible to   │
   │  current view    │               │  consumers;          │
   │                  │               │  A6 CascadeAgent     │
   └────────┬─────────┘               │  re-screens all      │
            │                         │  derived_from        │
            │                         │  descendants         │
            │  0.40 < score < 0.70    └──────────────────────┘
            │  ┌──────────────────────────────┐
            │  │    INCONCLUSIVE              │
            │  │    (stays PENDING;           │
            │  │    fail-closed;              │
            │  │    invisible to consumers)   │
            │  └──────────┬───────────────────┘
            │             │  A14 ReviewAgent (human-authorized)
            │             ├─────────────┐
            │             │             │
            │           release       reject
            │         (→ TRUSTED)  (→ QUARANTINED)
            │
            │  New contradicting belief wins resolution:
            ▼
   ┌──────────────────┐
   │                  │
   │   SUPERSEDED     │  ← tx_to set to NOW(); NEVER deleted
   │                  │  ← always queryable via bitemporal
   │  (tx_to closed;  │  ← contradiction_event records the
   │   belief stays   │     winner, loser, and rule used
   │   in the DB)     │
   └──────────────────┘

  State transition table:
  ┌──────────────┬──────────────┬──────────────────────────────────────┐
  │ From         │ To           │ Trigger                              │
  ├──────────────┼──────────────┼──────────────────────────────────────┤
  │ (none)       │ PENDING      │ Any ingest write; always             │
  │ PENDING      │ TRUSTED      │ Gate score ≤ 0.40                    │
  │ PENDING      │ QUARANTINED  │ Gate score ≥ 0.70                    │
  │ PENDING      │ PENDING      │ Gate score in (0.40, 0.70)           │
  │ TRUSTED      │ SUPERSEDED   │ Newer belief wins resolution         │
  │ INCONCLUSIVE │ TRUSTED      │ Human reviewer releases              │
  │ INCONCLUSIVE │ QUARANTINED  │ Human reviewer rejects               │
  │ SUPERSEDED   │ (terminal)   │ Never transitions further            │
  └──────────────┴──────────────┴──────────────────────────────────────┘

  Visibility by role:
  ┌──────────────┬────────────────┬────────────────────────────────────┐
  │ State        │ role_consumer  │ role_auditor                       │
  ├──────────────┼────────────────┼────────────────────────────────────┤
  │ PENDING      │ NEVER          │ Yes (direct belief table access)   │
  │ TRUSTED      │ Yes (current)  │ Yes (all versions)                 │
  │ QUARANTINED  │ NEVER          │ Yes                                │
  │ SUPERSEDED   │ NEVER          │ Yes (bitemporal query required)    │
  └──────────────┴────────────────┴────────────────────────────────────┘
`.trim()

const TRUST_BOUNDARY = `
              UNTRUSTED INPUT (any agent, any source)
                          │
                          ▼
          ┌───────────────────────────┐
          │  TB1: Agent → Memory      │  Every write lands as PENDING.
          │                           │  No path writes status='trusted'
          │  Nothing trusted          │  directly. Enforced by CHECK
          │  on arrival.              │  constraint — not application code.
          └──────────────┬────────────┘
                         │
                         ▼
          ┌───────────────────────────┐
          │  TB2: Memory → Gate       │  Gate reads committed state under
          │                           │  SERIALIZABLE isolation. No torn
          │  Verdicts sound under     │  reads. Screening is fail-closed:
          │  SERIALIZABLE.            │  if Lambda is down, beliefs stay
          │                           │  PENDING — never auto-promoted.
          └──────────────┬────────────┘
                         │
                         ▼
          ┌───────────────────────────┐
          │  TB3: Gate → Audit        │  Append-only WORM. IAM explicit
          │                           │  DENY on DeleteObject. Bucket-level
          │  Immutable, even          │  Object Lock COMPLIANCE, 365-day
          │  for the gate itself.     │  retention. Gate cannot erase
          │                           │  its own verdicts.
          └──────────────┬────────────┘
                         │
                         ▼
          ┌───────────────────────────┐
          │  TB4: Memory → Recall     │  THE critical boundary.
          │                           │
          │  Dual independent         │  Layer 1: role-scoped view
          │  enforcement.             │  (v_trusted_current — DB layer)
          │                           │
          │                           │  Layer 2: MCP Server
          │                           │  (read-only protocol — write verbs
          │                           │  raise MCPProtocolError before
          │                           │  the HTTP call is made)
          └───────────────────────────┘
                         │
                    TRUSTED OUTPUT
                (only beliefs that passed
                 all screening signals)

  Security invariants that cannot be violated:
  ┌───┬──────────────────────────────────────────────────────────────┐
  │ 1 │ No belief enters the store with status = 'trusted'           │
  │ 2 │ role_consumer cannot SELECT from the belief table directly   │
  │ 3 │ Retry wrapper re-reads state on every retry attempt          │
  │ 4 │ SERIALIZABLE isolation is never downgraded on retry          │
  │ 5 │ Screening gate is fail-closed — down = pending, not trusted  │
  │ 6 │ Audit records cannot be deleted or overwritten (WORM)        │
  │ 7 │ No agent with write authority can issue verdicts             │
  └───┴──────────────────────────────────────────────────────────────┘
`.trim()

const DIAGRAMS = [
  { id: 'context',   label: 'System Context',      content: SYSTEM_CONTEXT },
  { id: 'dataflow',  label: 'Data Flow',            content: DATA_FLOW      },
  { id: 'lifecycle', label: 'Belief Lifecycle',     content: STATE_MACHINE  },
  { id: 'trust',     label: 'Trust Boundaries',     content: TRUST_BOUNDARY },
]

const DESCRIPTIONS = {
  context:   'Who interacts with PQBS and from which direction. Shows external actors (AI agents, human reviewers, auditors, attackers) and the three AWS/CockroachDB components they touch.',
  dataflow:  'How data moves through every stage — Ingest → Resolve → Screen → Recall → Audit — with control decisions, tool invocations, and isolation guarantees annotated at each stage.',
  lifecycle: 'Formal state machine for a belief from the moment it\'s written to when it\'s superseded. Shows all states, labeled transitions, guards, and which database roles can see each state.',
  trust:     'The four trust boundaries PQBS enforces, with the mechanism at each boundary and the seven security invariants that cannot be violated.',
}

export default function DiagramsPage() {
  const [active, setActive] = useState('context')

  const diagram = DIAGRAMS.find(d => d.id === active)

  return (
    <div className="min-h-screen bg-gray-950 text-gray-100">
      <header className="border-b border-gray-800 px-6 py-4">
        <div className="flex items-center justify-between max-w-7xl mx-auto">
          <div>
            <h1 className="text-xl font-bold text-white">PQBS</h1>
            <p className="text-xs text-gray-400">Poison-Quarantine Belief Store</p>
          </div>
          <nav className="flex gap-2">
            <Link to="/" className="px-4 py-1.5 rounded text-sm text-gray-400 hover:text-white hover:bg-gray-800 transition-colors">Live Demo</Link>
            <Link to="/pqbs" className="px-4 py-1.5 rounded text-sm text-gray-400 hover:text-white hover:bg-gray-800 transition-colors">What is PQBS?</Link>
            <Link to="/diagrams" className="px-4 py-1.5 rounded text-sm font-medium bg-indigo-600 text-white">Diagrams</Link>
          </nav>
        </div>
      </header>

      <main className="max-w-7xl mx-auto px-6 py-8">
        <div className="mb-6">
          <h2 className="text-2xl font-bold text-white mb-1">Architecture Diagrams</h2>
          <p className="text-gray-400 text-sm">How PQBS is structured — from high-level system context down to individual state transitions.</p>
        </div>

        {/* Diagram tabs */}
        <div className="flex gap-1 mb-6 border-b border-gray-800">
          {DIAGRAMS.map(d => (
            <button
              key={d.id}
              onClick={() => setActive(d.id)}
              className={`px-4 py-2 text-sm font-medium transition-colors border-b-2 -mb-px ${
                active === d.id
                  ? 'border-indigo-500 text-white'
                  : 'border-transparent text-gray-400 hover:text-white'
              }`}
            >
              {d.label}
            </button>
          ))}
        </div>

        {/* Description */}
        <p className="text-gray-400 text-sm mb-4">{DESCRIPTIONS[active]}</p>

        {/* Diagram */}
        <div className="bg-gray-900 border border-gray-800 rounded-xl p-6 overflow-x-auto">
          <pre className="text-gray-300 text-xs leading-relaxed font-mono whitespace-pre">
            {diagram.content}
          </pre>
        </div>

        {/* Footer nav */}
        <div className="mt-8 flex gap-3 justify-center">
          <Link to="/" className="px-5 py-2 bg-indigo-600 hover:bg-indigo-500 text-white text-sm font-medium rounded transition-colors">
            Open Live Demo
          </Link>
          <Link to="/pqbs" className="px-5 py-2 bg-gray-800 hover:bg-gray-700 text-gray-200 text-sm font-medium rounded transition-colors">
            What is PQBS?
          </Link>
        </div>
      </main>
    </div>
  )
}
