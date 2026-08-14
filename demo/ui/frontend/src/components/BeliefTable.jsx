import React, { useState, useCallback } from 'react'
import { fetchBeliefs } from '../api.js'
import HealthBar from './HealthBar.jsx'

function StatusBadge({ status }) {
  const cls = {
    trusted: 'bg-green-900 text-green-300',
    quarantined: 'bg-red-900 text-red-300',
    pending: 'bg-yellow-900 text-yellow-300',
    inconclusive: 'bg-orange-900 text-orange-300',
    superseded: 'bg-gray-700 text-gray-400',
  }[status] ?? 'bg-gray-700 text-gray-400'

  return (
    <span className={`inline-block px-2 py-0.5 rounded-full text-xs font-medium ${cls}`}>
      {status}
    </span>
  )
}

export default function BeliefTable({ tenantId }) {
  const [data, setData] = useState(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState(null)
  const [limit, setLimit] = useState(50)

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const result = await fetchBeliefs(tenantId, limit)
      setData(result)
    } catch (e) {
      setError(e.message)
    } finally {
      setLoading(false)
    }
  }, [tenantId, limit])

  return (
    <div>
      <div className="flex items-center justify-between mb-4">
        <h2 className="text-lg font-semibold text-white">
          Beliefs {data ? `(${data.count})` : ''}
        </h2>
        <div className="flex items-center gap-3">
          <label className="text-sm text-gray-400">
            Limit
            <input
              type="number"
              min={1}
              max={500}
              value={limit}
              onChange={e => setLimit(Number(e.target.value))}
              className="ml-2 w-20 bg-gray-800 border border-gray-700 rounded px-2 py-1 text-sm text-gray-200 focus:outline-none focus:border-indigo-500"
            />
          </label>
          <button
            onClick={load}
            disabled={loading}
            className="px-4 py-1.5 bg-indigo-600 hover:bg-indigo-500 disabled:opacity-50 text-white text-sm rounded transition-colors"
          >
            {loading ? 'Loading…' : 'Refresh'}
          </button>
        </div>
      </div>

      {error && (
        <div className="bg-red-900/30 border border-red-700 rounded p-3 text-red-300 text-sm mb-4">
          {error}
        </div>
      )}

      {!data && !loading && (
        <div className="text-gray-500 text-sm text-center py-16">
          Enter a Tenant UUID above and click Refresh.
        </div>
      )}

      {data && data.beliefs.length === 0 && (
        <div className="text-gray-500 text-sm text-center py-16">No beliefs found.</div>
      )}

      {data && data.beliefs.length > 0 && (
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-gray-800 text-gray-400 text-xs uppercase">
                <th className="text-left py-2 pr-4 font-medium">ID</th>
                <th className="text-left py-2 pr-4 font-medium">Status</th>
                <th className="text-left py-2 pr-4 font-medium">Subject</th>
                <th className="text-left py-2 pr-4 font-medium">Predicate</th>
                <th className="text-left py-2 pr-4 font-medium">Object</th>
                <th className="text-left py-2 pr-4 font-medium">Trust</th>
                <th className="text-left py-2 pr-4 font-medium">tx_from</th>
              </tr>
            </thead>
            <tbody>
              {data.beliefs.map(b => (
                <tr key={b.belief_id} className="border-b border-gray-800/50 hover:bg-gray-900/50">
                  <td className="py-2 pr-4 text-gray-500 font-mono text-xs">
                    {b.belief_id.slice(0, 8)}…
                  </td>
                  <td className="py-2 pr-4">
                    <StatusBadge status={b.status} />
                  </td>
                  <td className="py-2 pr-4 text-gray-200">{b.subject}</td>
                  <td className="py-2 pr-4 font-mono text-indigo-300 text-xs">{b.predicate}</td>
                  <td className="py-2 pr-4 text-gray-300 max-w-xs truncate" title={b.object}>
                    {b.object}
                  </td>
                  <td className="py-2 pr-4">
                    {b.trust_score != null ? (
                      <HealthBar value={b.trust_score} max={1} />
                    ) : (
                      <span className="text-gray-600">—</span>
                    )}
                  </td>
                  <td className="py-2 pr-4 text-gray-500 text-xs">
                    {b.tx_from ? b.tx_from.slice(0, 19).replace('T', ' ') : '—'}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}
