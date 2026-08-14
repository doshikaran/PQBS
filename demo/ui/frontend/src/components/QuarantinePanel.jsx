import React, { useState, useCallback } from 'react'
import { fetchQuarantine } from '../api.js'

function StatusBadge({ status }) {
  const cls = {
    held: 'bg-yellow-900 text-yellow-300',
    released: 'bg-green-900 text-green-300',
    rejected: 'bg-red-900 text-red-300',
  }[status] ?? 'bg-gray-700 text-gray-400'
  return (
    <span className={`inline-block px-2 py-0.5 rounded-full text-xs font-medium ${cls}`}>
      {status}
    </span>
  )
}

function SignalBar({ label, score }) {
  const pct = Math.round((score ?? 0) * 100)
  const color = score >= 0.7 ? 'bg-red-500' : score >= 0.4 ? 'bg-yellow-500' : 'bg-green-500'
  return (
    <div className="flex items-center gap-2 text-xs">
      <span className="text-gray-500 w-6 shrink-0">{label}</span>
      <div className="flex-1 bg-gray-800 rounded-full h-1.5">
        <div className={`${color} h-1.5 rounded-full`} style={{ width: `${pct}%` }} />
      </div>
      <span className="text-gray-400 w-8 text-right">{(score ?? 0).toFixed(2)}</span>
    </div>
  )
}

export default function QuarantinePanel({ tenantId }) {
  const [data, setData] = useState(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState(null)

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const result = await fetchQuarantine(tenantId)
      setData(result)
    } catch (e) {
      setError(e.message)
    } finally {
      setLoading(false)
    }
  }, [tenantId])

  return (
    <div>
      <div className="flex items-center justify-between mb-4">
        <h2 className="text-lg font-semibold text-white">
          Quarantine {data ? `(${data.count})` : ''}
        </h2>
        <button
          onClick={load}
          disabled={loading}
          className="px-4 py-1.5 bg-indigo-600 hover:bg-indigo-500 disabled:opacity-50 text-white text-sm rounded transition-colors"
        >
          {loading ? 'Loading…' : 'Refresh'}
        </button>
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

      {data && data.quarantine.length === 0 && (
        <div className="text-gray-500 text-sm text-center py-16">No quarantined beliefs.</div>
      )}

      {data && data.quarantine.length > 0 && (
        <div className="space-y-4">
          {data.quarantine.map(q => {
            const signals = q.signal_scores
              ? (typeof q.signal_scores === 'string' ? JSON.parse(q.signal_scores) : q.signal_scores)
              : {}
            return (
              <div key={q.quarantine_id} className="bg-gray-900 border border-gray-800 rounded-lg p-4">
                <div className="flex items-start justify-between mb-3">
                  <div>
                    <span className="text-white font-medium">{q.subject}</span>
                    <span className="text-gray-500 mx-2">·</span>
                    <span className="font-mono text-indigo-300 text-sm">{q.predicate}</span>
                    <span className="text-gray-500 mx-2">·</span>
                    <span className="text-gray-300 text-sm">{q.object}</span>
                  </div>
                  <StatusBadge status={q.disposition} />
                </div>
                <div className="flex items-center gap-4 text-xs text-gray-500 mb-3">
                  <span>
                    Reason: <code className="text-orange-300">{q.reason_code}</code>
                  </span>
                  {q.trust_score != null && (
                    <span>
                      Trust: <span className="text-red-400 font-medium">{q.trust_score.toFixed(3)}</span>
                    </span>
                  )}
                  {q.quarantined_at && (
                    <span>{q.quarantined_at.slice(0, 19).replace('T', ' ')}</span>
                  )}
                </div>
                {Object.keys(signals).length > 0 && (
                  <div className="space-y-1">
                    <p className="text-xs text-gray-600 mb-1">Signal scores</p>
                    {Object.entries(signals).map(([k, v]) => (
                      <SignalBar key={k} label={k} score={typeof v === 'number' ? v : v?.score ?? 0} />
                    ))}
                  </div>
                )}
              </div>
            )
          })}
        </div>
      )}
    </div>
  )
}
