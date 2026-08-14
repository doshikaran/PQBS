import React, { useState } from 'react'
import BeliefTable from './components/BeliefTable.jsx'
import QuarantinePanel from './components/QuarantinePanel.jsx'
import TemporalQuery from './components/TemporalQuery.jsx'
import RecallSearch from './components/RecallSearch.jsx'
import MetricsDashboard from './components/MetricsDashboard.jsx'

const TABS = ['Beliefs', 'Quarantine', 'Temporal', 'Recall', 'Metrics']

export default function App() {
  const [tab, setTab] = useState('Beliefs')
  const [tenantId, setTenantId] = useState('')

  return (
    <div className="min-h-screen bg-gray-950 text-gray-100">
      <header className="border-b border-gray-800 px-6 py-4">
        <div className="flex items-center justify-between max-w-7xl mx-auto">
          <div>
            <h1 className="text-xl font-bold text-white">PQBS</h1>
            <p className="text-xs text-gray-400">Poison-Quarantine Belief Store</p>
          </div>
          <input
            className="bg-gray-800 border border-gray-700 rounded px-3 py-1.5 text-sm text-gray-200 w-80 focus:outline-none focus:border-indigo-500"
            placeholder="Tenant UUID"
            value={tenantId}
            onChange={e => setTenantId(e.target.value)}
          />
        </div>
        <nav className="max-w-7xl mx-auto mt-3 flex gap-1">
          {TABS.map(t => (
            <button
              key={t}
              onClick={() => setTab(t)}
              className={`px-4 py-1.5 rounded text-sm font-medium transition-colors ${
                tab === t
                  ? 'bg-indigo-600 text-white'
                  : 'text-gray-400 hover:text-white hover:bg-gray-800'
              }`}
            >
              {t}
            </button>
          ))}
        </nav>
      </header>
      <main className="max-w-7xl mx-auto px-6 py-6">
        {tab === 'Beliefs' && <BeliefTable tenantId={tenantId} />}
        {tab === 'Quarantine' && <QuarantinePanel tenantId={tenantId} />}
        {tab === 'Temporal' && <TemporalQuery tenantId={tenantId} />}
        {tab === 'Recall' && <RecallSearch tenantId={tenantId} />}
        {tab === 'Metrics' && <MetricsDashboard tenantId={tenantId} />}
      </main>
    </div>
  )
}
