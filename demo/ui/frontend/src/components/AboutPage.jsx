import React from 'react'
import { Link } from 'react-router-dom'

const Step = ({ number, title, children }) => (
  <div className="flex gap-4">
    <div className="flex-shrink-0 w-8 h-8 rounded-full bg-indigo-600 flex items-center justify-center text-white text-sm font-bold">
      {number}
    </div>
    <div>
      <h3 className="text-white font-semibold mb-1">{title}</h3>
      <p className="text-gray-400 text-sm leading-relaxed">{children}</p>
    </div>
  </div>
)

const UseCase = ({ company, industry, problem, outcome }) => (
  <div className="bg-gray-900 border border-gray-800 rounded-lg p-5">
    <div className="flex items-start justify-between mb-3">
      <div>
        <p className="text-white font-semibold">{company}</p>
        <p className="text-indigo-400 text-xs">{industry}</p>
      </div>
    </div>
    <p className="text-gray-400 text-sm mb-3"><span className="text-gray-300 font-medium">Problem: </span>{problem}</p>
    <p className="text-gray-400 text-sm"><span className="text-green-400 font-medium">With PQBS: </span>{outcome}</p>
  </div>
)

const SignalRow = ({ id, name, what, weight }) => (
  <div className="flex items-start gap-3 py-2 border-b border-gray-800 last:border-0">
    <span className="text-xs font-mono text-indigo-400 w-6 flex-shrink-0">S{id}</span>
    <div className="flex-1">
      <span className="text-gray-300 text-sm font-medium">{name}</span>
      <span className="text-gray-500 text-xs ml-2">— {what}</span>
    </div>
    <span className="text-xs text-gray-500 flex-shrink-0">weight {weight}</span>
  </div>
)

export default function AboutPage() {
  return (
    <div className="min-h-screen bg-gray-950 text-gray-100">
      {/* Nav */}
      <header className="border-b border-gray-800 px-6 py-4">
        <div className="flex items-center justify-between max-w-5xl mx-auto">
          <div>
            <h1 className="text-xl font-bold text-white">PQBS</h1>
            <p className="text-xs text-gray-400">Poison-Quarantine Belief Store</p>
          </div>
          <nav className="flex gap-2">
            <Link to="/" className="px-4 py-1.5 rounded text-sm text-gray-400 hover:text-white hover:bg-gray-800 transition-colors">Live Demo</Link>
            <Link to="/pqbs" className="px-4 py-1.5 rounded text-sm font-medium bg-indigo-600 text-white">What is PQBS?</Link>
            <Link to="/diagrams" className="px-4 py-1.5 rounded text-sm text-gray-400 hover:text-white hover:bg-gray-800 transition-colors">Diagrams</Link>
          </nav>
        </div>
      </header>

      <main className="max-w-5xl mx-auto px-6 py-12 space-y-16">

        {/* Hero */}
        <section className="text-center space-y-4">
          <div className="inline-block bg-indigo-950 border border-indigo-800 rounded-full px-4 py-1 text-indigo-400 text-xs font-medium mb-2">
            CockroachDB × AWS Hackathon 2026
          </div>
          <h2 className="text-4xl font-bold text-white leading-tight">
            AI agents are starting to remember things.<br />
            <span className="text-indigo-400">Can you trust what they remember?</span>
          </h2>
          <p className="text-gray-400 text-lg max-w-2xl mx-auto leading-relaxed">
            PQBS is a memory layer for AI agents that checks every fact before the agent is allowed to use it — built on CockroachDB's always-on, distributed database and AWS Bedrock.
          </p>
        </section>

        {/* The analogy */}
        <section className="bg-gray-900 border border-gray-800 rounded-xl p-8">
          <h2 className="text-xl font-bold text-white mb-4">The shared notebook problem</h2>
          <div className="grid md:grid-cols-2 gap-8">
            <div>
              <h3 className="text-red-400 font-semibold mb-2 text-sm uppercase tracking-wide">Without PQBS</h3>
              <p className="text-gray-400 text-sm leading-relaxed">
                Imagine a company with a shared notebook. Every AI assistant writes into it, and every assistant reads from it before making decisions. <em className="text-gray-300">"Halden Freight wants overnight delivery." "The Johnson account is on the premium plan."</em>
              </p>
              <p className="text-gray-400 text-sm leading-relaxed mt-3">
                Now imagine someone slips a fake page into that notebook. Not obviously fake — it looks like a normal note. Nobody notices. Three weeks later, an assistant reads it and acts on it. The damage happens not when the fake page was written, but when it was read.
              </p>
              <p className="text-gray-500 text-xs mt-3 italic">
                Security researchers have demonstrated this with success rates above 80% at a poison rate below 0.1% of the memory corpus.
              </p>
            </div>
            <div>
              <h3 className="text-green-400 font-semibold mb-2 text-sm uppercase tracking-wide">With PQBS</h3>
              <p className="text-gray-400 text-sm leading-relaxed">
                Every new note goes into a holding tray. An inspector runs it through 8 checks before it's filed. Notes that pass go into the notebook. Notes that fail go to quarantine.
              </p>
              <p className="text-gray-400 text-sm leading-relaxed mt-3">
                When an attack is blocked, nobody notices — the assistant answers correctly, and the engineer gets an alert showing exactly which document, which agent, and precisely why the note was rejected.
              </p>
              <p className="text-gray-400 text-sm leading-relaxed mt-3">
                When something contradicts an old note, the old one isn't thrown away — it's marked "true until Tuesday," so you can always reconstruct what the system believed at any moment in the past.
              </p>
            </div>
          </div>
        </section>

        {/* How it works — 4 steps */}
        <section>
          <h2 className="text-xl font-bold text-white mb-6">How it works — four guarantees</h2>
          <div className="grid md:grid-cols-2 gap-6">
            <Step number="1" title="Every write lands in a holding tray">
              No belief enters shared memory as trusted. Every fact written by any agent starts as PENDING — real, recorded, but invisible to every reading agent. This is enforced by a CHECK constraint at the database layer that cannot be bypassed by application code.
            </Step>
            <Step number="2" title="Eight signals inspect every fact">
              An async screening gate scores each belief across 8 dimensions: embedding anomaly, source trust tier, imperative content detection (via Bedrock Llama 3), author burst rate, contradiction frequency, source diversity, derivation chain integrity, and temporal plausibility. Score ≤ 0.40 → trusted. Score ≥ 0.70 → quarantined.
            </Step>
            <Step number="3" title="Nothing is ever silently deleted">
              When new information contradicts old information, the old fact isn't overwritten — its validity window is closed with a timestamp, and the new fact references it. Every contradiction is recorded, including cases where the original fact wins. You can always reconstruct the full history.
            </Step>
            <Step number="4" title="You can ask what was believed at any point">
              Two temporal reconstruction mechanisms: bitemporal tx_from/tx_to columns (permanent, no retention limit) and CockroachDB AS OF SYSTEM TIME (bounded to the MVCC GC window). "Why did the agent make that call on Tuesday at 2pm?" becomes a single database query.
            </Step>
          </div>
        </section>

        {/* 8 Signals */}
        <section>
          <h2 className="text-xl font-bold text-white mb-2">The 8 screening signals</h2>
          <p className="text-gray-400 text-sm mb-5">An attacker can engineer a belief to slip past any one check. Getting past all eight simultaneously is what makes memory poisoning hard.</p>
          <div className="bg-gray-900 border border-gray-800 rounded-xl p-6">
            <SignalRow id={1} name="Embedding anomaly" what="semantic distance from the predicate cluster centroid" weight="0.32" />
            <SignalRow id={2} name="Source trust tier" what="authoritative vs. unverified vs. adversarial origin" weight="0.25" />
            <SignalRow id={3} name="Imperative content" what="instruction masquerading as assertion — detected by Bedrock Llama 3 70B" weight="0.14" />
            <SignalRow id={4} name="Author burst" what="anomalous write rate for the agent identity" weight="0.14" />
            <SignalRow id={5} name="Contradiction burst" what="unusual conflict rate for this predicate in a time window" weight="0.07" />
            <SignalRow id={6} name="Source diversity" what="whether supporting beliefs come from independent source digests" weight="0.04" />
            <SignalRow id={7} name="Derivation integrity" what="whether the parent belief is trusted before inference proceeds" weight="0.03" />
            <SignalRow id={8} name="Temporal plausibility" what="whether the validity window is coherent for this predicate" weight="0.01" />
          </div>
        </section>

        {/* Live example */}
        <section className="bg-gray-900 border border-yellow-900/40 rounded-xl p-8">
          <div className="flex items-start gap-3 mb-5">
            <div className="w-2 h-2 rounded-full bg-yellow-500 mt-2 flex-shrink-0" />
            <h2 className="text-xl font-bold text-white">Live example — what you're seeing in this demo</h2>
          </div>
          <p className="text-gray-400 text-sm leading-relaxed mb-4">
            The <strong className="text-gray-200">eval tenant</strong> in this demo ran a red-team corpus of 350 attack attempts against the system — poisoned facts designed to manipulate logistics decisions: assign platinum status without verification, bypass payment validation, override fraud flags, disable suspension triggers.
          </p>
          <div className="grid grid-cols-3 gap-4 mb-4">
            <div className="bg-gray-800 rounded-lg p-4 text-center">
              <p className="text-3xl font-bold text-white">63</p>
              <p className="text-xs text-gray-400 mt-1">attacks quarantined</p>
            </div>
            <div className="bg-gray-800 rounded-lg p-4 text-center">
              <p className="text-3xl font-bold text-green-400">0</p>
              <p className="text-xs text-gray-400 mt-1">false positives</p>
            </div>
            <div className="bg-gray-800 rounded-lg p-4 text-center">
              <p className="text-3xl font-bold text-indigo-400">158ms</p>
              <p className="text-xs text-gray-400 mt-1">median screening lag</p>
            </div>
          </div>
          <p className="text-gray-500 text-xs">
            The <strong className="text-gray-300">demo tenant</strong> holds 3,150 legitimate Northwind Logistics beliefs — customer tiers, preferred carriers, payment terms, credit limits — all screened and trusted. The Recall tab searches these via CockroachDB's distributed HNSW vector index.
          </p>
        </section>

        {/* Use cases */}
        <section>
          <h2 className="text-xl font-bold text-white mb-2">How companies use this</h2>
          <p className="text-gray-400 text-sm mb-6">Any industry where AI agents share memory across sessions, users, or instances.</p>
          <div className="grid md:grid-cols-2 gap-4">
            <UseCase
              company="Logistics & Supply Chain"
              industry="Freight, routing, carrier management"
              problem="AI agents update delivery preferences, carrier contracts, and payment terms from emails, PDFs, and API feeds. A poisoned document could reroute shipments or alter credit limits."
              outcome="Every preference update is screened before agents act on it. A vendor email saying 'always use expedited regardless of cost' is flagged as imperative content and held for human review."
            />
            <UseCase
              company="Financial Services"
              industry="Banking, lending, compliance"
              problem="Multiple agents share customer risk profiles. A poisoned belief saying 'exempt from fraud checks' could survive for weeks before triggering a bad decision."
              outcome="Every risk belief is bitemporal — the exact state at the time of any lending decision is reconstructable on demand. Compliance audits become a single query, not a week-long investigation."
            />
            <UseCase
              company="Healthcare & Clinical AI"
              industry="Diagnostics, patient records, treatment planning"
              problem="Agents reading patient histories need to know which facts have been updated, superseded, or contradicted — and by whom, and when."
              outcome="No belief is ever deleted. Every correction produces a supersession record. The full contradiction history is available without restoring backups."
            />
            <UseCase
              company="Enterprise SaaS"
              industry="CRM, ERP, customer success platforms"
              problem="Dozens of agents write to shared customer memory simultaneously. Under high concurrency, two agents can both read a belief, independently decide to update it, and one update silently disappears."
              outcome="SERIALIZABLE isolation enforces that every contradiction resolution is deterministic. The losing fact is recorded, not discarded. No silent overwrites."
            />
          </div>
        </section>

        {/* CTA */}
        <section className="text-center space-y-4 pb-8">
          <h2 className="text-2xl font-bold text-white">See it live</h2>
          <p className="text-gray-400 text-sm">Switch to the live demo to watch the screening gate, quarantine queue, and temporal reconstruction in action.</p>
          <div className="flex gap-3 justify-center">
            <Link to="/" className="px-6 py-2.5 bg-indigo-600 hover:bg-indigo-500 text-white text-sm font-medium rounded transition-colors">
              Open Live Demo
            </Link>
            <Link to="/diagrams" className="px-6 py-2.5 bg-gray-800 hover:bg-gray-700 text-gray-200 text-sm font-medium rounded transition-colors">
              View Architecture
            </Link>
          </div>
        </section>

      </main>
    </div>
  )
}
