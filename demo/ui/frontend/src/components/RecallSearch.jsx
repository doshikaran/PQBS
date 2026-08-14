import React, { useState } from 'react'
import { postRecall } from '../api.js'

export default function RecallSearch({ tenantId }) {
  const [query, setQuery] = useState('')
  const [limit, setLimit] = useState(10)
  const [result, setResult] = useState(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState(null)

  const submit = async () => {
    if (!tenantId || !query.trim()) {
      setError('Tenant UUID and query are required.')
      return
    }
    setLoading(true)
    setError(null)
    setResult(null)
    try {
      const data = await postRecall(tenantId, query, limit)
      setResult(data)
    } catch (e) {
      setError(e.message)
    } finally {
      setLoading(false)
    }
  }

  return (
    <div>
      <h2 className="text-lg font-semibold text-white mb-4">Recall Search (A9)</h2>

      <div className="bg-gray-900 border border-gray-800 rounded-lg p-4 mb-6 space-y-3">
        <div className="flex items-start gap-4">
          <label className="text-sm text-gray-400 w-20 shrink-0 mt-2">Query</label>
          <textarea
            value={query}
            onChange={e => setQuery(e.target.value)}
            rows={2}
            placeholder="What does Alice do for work?"
            className="flex-1 bg-gray-800 border border-gray-700 rounded px-3 py-2 text-sm text-gray-200 focus:outline-none focus:border-indigo-500 resize-none"
          />
        </div>
        <div className="flex items-center gap-4">
          <label className="text-sm text-gray-400 w-20 shrink-0">
            Limit: <span className="text-indigo-300 font-medium">{limit}</span>
          </label>
          <input
            type="range"
            min={1}
            max={20}
            value={limit}
            onChange={e => setLimit(Number(e.target.value))}
            className="flex-1 accent-indigo-500"
          />
        </div>
        <div>
          <button
            onClick={submit}
            disabled={loading}
            className="px-5 py-2 bg-indigo-600 hover:bg-indigo-500 disabled:opacity-50 text-white text-sm rounded transition-colors"
          >
            {loading ? 'Searching…' : 'Search'}
          </button>
        </div>
        <p className="text-xs text-gray-600">
          Searches <code className="text-gray-500">v_trusted_current</code> (status='trusted' AND tx_to IS NULL)
          using vector similarity. Requires AWS_REGION and Bedrock credentials.
        </p>
      </div>

      {error && (
        <div className="bg-red-900/30 border border-red-700 rounded p-3 text-red-300 text-sm mb-4">
          {error}
        </div>
      )}

      {result && (
        <div>
          <div className="flex items-center gap-3 mb-3">
            <h3 className="text-white font-medium">
              {result.count} result(s) — {result.query_latency_ms}ms
            </h3>
            <span className="text-gray-500 text-xs font-mono">
              retrieval_id: {result.retrieval_id?.slice(0, 12)}…
            </span>
          </div>

          {result.beliefs.length === 0 ? (
            <div className="text-gray-500 text-sm">No trusted beliefs matched the query.</div>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b border-gray-800 text-gray-400 text-xs uppercase">
                    <th className="text-left py-2 pr-4 font-medium">Subject</th>
                    <th className="text-left py-2 pr-4 font-medium">Predicate</th>
                    <th className="text-left py-2 pr-4 font-medium">Object</th>
                    <th className="text-left py-2 pr-4 font-medium">Trust</th>
                    <th className="text-left py-2 pr-4 font-medium">Belief ID</th>
                  </tr>
                </thead>
                <tbody>
                  {result.beliefs.map(b => (
                    <tr key={b.belief_id} className="border-b border-gray-800/50 hover:bg-gray-900/50">
                      <td className="py-2 pr-4 text-gray-200">{b.subject}</td>
                      <td className="py-2 pr-4 font-mono text-indigo-300 text-xs">{b.predicate}</td>
                      <td className="py-2 pr-4 text-gray-300 max-w-xs truncate" title={b.object}>
                        {b.object}
                      </td>
                      <td className="py-2 pr-4">
                        {b.trust_score != null ? (
                          <span className="text-green-400 font-medium">
                            {b.trust_score.toFixed(3)}
                          </span>
                        ) : (
                          <span className="text-gray-600">—</span>
                        )}
                      </td>
                      <td className="py-2 pr-4 text-gray-600 font-mono text-xs">
                        {b.belief_id.slice(0, 8)}…
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}
    </div>
  )
}
