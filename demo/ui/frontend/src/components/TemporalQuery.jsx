import React, { useState } from 'react'
import { fetchTemporal } from '../api.js'

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

export default function TemporalQuery({ tenantId }) {
  const now = new Date().toISOString().slice(0, 19)
  const [asOf, setAsOf] = useState(now)
  const [mechanism, setMechanism] = useState('bitemporal')
  const [subjectFilter, setSubjectFilter] = useState('')
  const [result, setResult] = useState(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState(null)

  const submit = async () => {
    if (!tenantId || !asOf) {
      setError('Tenant UUID and As-Of time are required.')
      return
    }
    setLoading(true)
    setError(null)
    setResult(null)
    try {
      const data = await fetchTemporal(tenantId, asOf, mechanism, subjectFilter)
      setResult(data)
    } catch (e) {
      setError(e.message)
    } finally {
      setLoading(false)
    }
  }

  return (
    <div>
      <h2 className="text-lg font-semibold text-white mb-4">Temporal Query (A10)</h2>

      <div className="bg-gray-900 border border-gray-800 rounded-lg p-4 mb-6 space-y-3">
        <div className="flex items-center gap-4">
          <label className="text-sm text-gray-400 w-28 shrink-0">As Of (ISO)</label>
          <input
            type="text"
            value={asOf}
            onChange={e => setAsOf(e.target.value)}
            placeholder="2026-08-01T12:00:00"
            className="flex-1 bg-gray-800 border border-gray-700 rounded px-3 py-1.5 text-sm text-gray-200 focus:outline-none focus:border-indigo-500"
          />
        </div>
        <div className="flex items-center gap-4">
          <label className="text-sm text-gray-400 w-28 shrink-0">Mechanism</label>
          <div className="flex gap-4">
            {['bitemporal', 'mvcc'].map(m => (
              <label key={m} className="flex items-center gap-2 text-sm text-gray-300 cursor-pointer">
                <input
                  type="radio"
                  name="mechanism"
                  value={m}
                  checked={mechanism === m}
                  onChange={() => setMechanism(m)}
                  className="accent-indigo-500"
                />
                {m === 'bitemporal' ? 'Bitemporal (unbounded)' : 'MVCC (~1h window)'}
              </label>
            ))}
          </div>
        </div>
        <div className="flex items-center gap-4">
          <label className="text-sm text-gray-400 w-28 shrink-0">Subject Filter</label>
          <input
            type="text"
            value={subjectFilter}
            onChange={e => setSubjectFilter(e.target.value)}
            placeholder="Optional subject string"
            className="flex-1 bg-gray-800 border border-gray-700 rounded px-3 py-1.5 text-sm text-gray-200 focus:outline-none focus:border-indigo-500"
          />
        </div>
        <div>
          <button
            onClick={submit}
            disabled={loading}
            className="px-5 py-2 bg-indigo-600 hover:bg-indigo-500 disabled:opacity-50 text-white text-sm rounded transition-colors"
          >
            {loading ? 'Querying…' : 'Query'}
          </button>
        </div>
        <p className="text-xs text-gray-600">
          Bitemporal uses tx_from/tx_to columns — works for any historical timestamp, no retention limit.
          MVCC uses CockroachDB AS OF SYSTEM TIME — limited to ~1h GC window on Serverless.
        </p>
      </div>

      {error && (
        <div className="bg-red-900/30 border border-red-700 rounded p-3 text-red-300 text-sm mb-4">
          {error}
        </div>
      )}

      {result?.error && (
        <div className="bg-orange-900/30 border border-orange-700 rounded p-4 mb-4">
          <p className="text-orange-300 text-sm font-medium">MVCC Error: {result.error}</p>
          {result.suggestion && (
            <p className="text-orange-400/80 text-xs mt-1">{result.suggestion}</p>
          )}
        </div>
      )}

      {result && !result.error && (
        <div>
          <div className="flex items-center gap-3 mb-3">
            <h3 className="text-white font-medium">
              {result.count} belief(s) at {result.as_of?.slice(0, 19).replace('T', ' ')}
            </h3>
            <span className="bg-indigo-900/50 text-indigo-300 text-xs px-2 py-0.5 rounded">
              {result.mechanism_used}
            </span>
          </div>

          {result.beliefs.length === 0 ? (
            <div className="text-gray-500 text-sm">No beliefs at this point in time.</div>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b border-gray-800 text-gray-400 text-xs uppercase">
                    <th className="text-left py-2 pr-4 font-medium">Subject</th>
                    <th className="text-left py-2 pr-4 font-medium">Predicate</th>
                    <th className="text-left py-2 pr-4 font-medium">Object</th>
                    <th className="text-left py-2 pr-4 font-medium">Status</th>
                    {result.mechanism_used === 'bitemporal' && (
                      <th className="text-left py-2 pr-4 font-medium">tx_from</th>
                    )}
                  </tr>
                </thead>
                <tbody>
                  {result.beliefs.map((b, i) => (
                    <tr key={i} className="border-b border-gray-800/50 hover:bg-gray-900/50">
                      <td className="py-2 pr-4 text-gray-200">{b.subject}</td>
                      <td className="py-2 pr-4 font-mono text-indigo-300 text-xs">{b.predicate}</td>
                      <td className="py-2 pr-4 text-gray-300">{b.object}</td>
                      <td className="py-2 pr-4">
                        <StatusBadge status={b.status} />
                      </td>
                      {result.mechanism_used === 'bitemporal' && (
                        <td className="py-2 pr-4 text-gray-500 text-xs">{b.tx_from}</td>
                      )}
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
