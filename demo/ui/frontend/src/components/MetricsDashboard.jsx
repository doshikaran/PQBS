import React, { useState, useCallback } from 'react'
import { fetchMetrics } from '../api.js'

function MetricCard({ title, children }) {
  return (
    <div className="bg-gray-900 border border-gray-800 rounded-lg p-4">
      <h3 className="text-xs font-medium text-gray-500 uppercase tracking-wider mb-3">{title}</h3>
      {children}
    </div>
  )
}

function LatencyRow({ label, p50, p99, targetMs = 5000 }) {
  const p50Ok = p50 < targetMs
  const p99Ok = p99 < targetMs * 2
  return (
    <div className="flex items-center justify-between text-sm py-1">
      <span className="text-gray-400">{label}</span>
      <div className="flex gap-4 text-xs tabular-nums">
        <span>
          p50:{' '}
          <span className={p50Ok ? 'text-green-400' : 'text-red-400'}>
            {p50.toFixed(0)}ms
          </span>
        </span>
        <span>
          p99:{' '}
          <span className={p99Ok ? 'text-green-400' : 'text-red-400'}>
            {p99.toFixed(0)}ms
          </span>
        </span>
      </div>
    </div>
  )
}

function RetryRateIndicator({ rate }) {
  const pct = Math.round(rate * 100)
  const color = rate < 0.05 ? 'text-green-400' : rate < 0.30 ? 'text-orange-400' : 'text-red-400'
  const barColor = rate < 0.05 ? 'bg-green-500' : rate < 0.30 ? 'bg-yellow-500' : 'bg-red-500'
  return (
    <div>
      <div className="flex items-center justify-between mb-1">
        <span className="text-sm text-gray-400">Retry Rate</span>
        <span className={`text-sm font-medium ${color}`}>{pct}%</span>
      </div>
      <div className="bg-gray-800 rounded-full h-2">
        <div
          className={`${barColor} h-2 rounded-full transition-all`}
          style={{ width: `${Math.min(pct, 100)}%` }}
        />
      </div>
      <p className="text-xs text-gray-600 mt-1">
        {rate < 0.05 ? 'Healthy' : rate < 0.30 ? 'Elevated — monitor' : 'Critical — investigate'}
      </p>
    </div>
  )
}

function QuarantineByReason({ data }) {
  if (!data || Object.keys(data).length === 0) {
    return <p className="text-gray-600 text-sm">No quarantine data</p>
  }
  const max = Math.max(...Object.values(data), 1)
  return (
    <div className="space-y-2">
      {Object.entries(data)
        .sort(([, a], [, b]) => b - a)
        .map(([reason, count]) => (
          <div key={reason}>
            <div className="flex items-center justify-between text-xs mb-0.5">
              <span className="text-orange-300 font-mono">{reason}</span>
              <span className="text-gray-400">{count}</span>
            </div>
            <div className="bg-gray-800 rounded h-1.5">
              <div
                className="bg-orange-500 h-1.5 rounded"
                style={{ width: `${(count / max) * 100}%` }}
              />
            </div>
          </div>
        ))}
    </div>
  )
}

function TrustScoreHistogram({ p50, p99, sampleCount }) {
  // Simple visual: show a scale 0→1 with p50 and p99 markers
  const p50Pct = Math.round(p50 * 100)
  const p99Pct = Math.round(p99 * 100)
  return (
    <div>
      <div className="relative h-8 bg-gray-800 rounded mb-2">
        {/* p50 marker */}
        <div
          className="absolute top-0 h-full w-0.5 bg-green-400"
          style={{ left: `${p50Pct}%` }}
          title={`p50: ${p50.toFixed(3)}`}
        />
        {/* p99 marker */}
        <div
          className="absolute top-0 h-full w-0.5 bg-blue-400"
          style={{ left: `${p99Pct}%` }}
          title={`p99: ${p99.toFixed(3)}`}
        />
        {/* Trust zone bands */}
        <div className="absolute top-0 left-0 h-full w-2/5 bg-red-900/30 rounded-l" />
        <div className="absolute top-0 h-full bg-yellow-900/20" style={{ left: '40%', width: '30%' }} />
        <div className="absolute top-0 h-full bg-green-900/20 rounded-r" style={{ left: '70%', right: 0 }} />
      </div>
      <div className="flex justify-between text-xs text-gray-600">
        <span>0 (quarantine)</span>
        <span>0.4</span>
        <span>0.7</span>
        <span>1.0 (trust)</span>
      </div>
      <div className="flex gap-4 text-xs mt-2">
        <span>
          p50: <span className="text-green-400 font-medium">{p50.toFixed(3)}</span>
        </span>
        <span>
          p99: <span className="text-blue-400 font-medium">{p99.toFixed(3)}</span>
        </span>
        <span className="text-gray-600">{sampleCount} samples</span>
      </div>
    </div>
  )
}

function BeliefCountGrid({ counts }) {
  const items = [
    { label: 'Total', value: counts.total, color: 'text-white' },
    { label: 'Trusted', value: counts.trusted, color: 'text-green-400' },
    { label: 'Quarantined', value: counts.quarantined, color: 'text-red-400' },
    { label: 'Pending', value: counts.pending, color: 'text-yellow-400' },
    { label: 'Inconclusive', value: counts.inconclusive, color: 'text-orange-400' },
    { label: 'Superseded', value: counts.superseded, color: 'text-gray-500' },
  ]
  return (
    <div className="grid grid-cols-3 gap-3">
      {items.map(({ label, value, color }) => (
        <div key={label} className="bg-gray-800 rounded p-3 text-center">
          <div className={`text-2xl font-bold tabular-nums ${color}`}>{value}</div>
          <div className="text-xs text-gray-500 mt-0.5">{label}</div>
        </div>
      ))}
    </div>
  )
}

export default function MetricsDashboard({ tenantId }) {
  const [data, setData] = useState(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState(null)

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const result = await fetchMetrics(tenantId)
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
        <h2 className="text-lg font-semibold text-white">Metrics Dashboard (A17)</h2>
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
          Click Refresh to load metrics (optionally enter a Tenant UUID above for DB-loaded data).
        </div>
      )}

      {data && (
        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
          {/* Belief counts */}
          <MetricCard title="Belief Counts">
            <BeliefCountGrid counts={data.belief_counts} />
          </MetricCard>

          {/* Latency */}
          <MetricCard title="Latency">
            <LatencyRow
              label="Screening lag"
              p50={data.health.screening_lag_p50_ms}
              p99={data.health.screening_lag_p99_ms}
              targetMs={5000}
            />
            <LatencyRow
              label="Write latency"
              p50={data.health.write_latency_p50_ms}
              p99={data.health.write_latency_p99_ms}
              targetMs={500}
            />
            <LatencyRow
              label="Recall latency"
              p50={data.health.recall_latency_p50_ms}
              p99={data.health.recall_latency_p99_ms}
              targetMs={600}
            />
          </MetricCard>

          {/* Retry rate */}
          <MetricCard title="Contention">
            <RetryRateIndicator rate={data.health.retry_rate} />
            <div className="flex gap-4 text-xs text-gray-500 mt-3">
              <span>Retries: {data.health.retry_count}</span>
              <span>Total attempts: {data.health.retry_total_attempts}</span>
            </div>
          </MetricCard>

          {/* Trust score distribution */}
          <MetricCard title="Trust Score Distribution">
            <TrustScoreHistogram
              p50={data.integrity.trust_score_p50}
              p99={data.integrity.trust_score_p99}
              sampleCount={data.integrity.trust_score_sample_count}
            />
          </MetricCard>

          {/* Quarantine by reason */}
          <MetricCard title="Quarantine by Reason">
            <QuarantineByReason data={data.integrity.quarantine_by_reason} />
            <div className="mt-3 text-xs text-gray-500">
              Review queue: <span className="text-yellow-300 font-medium">{data.integrity.review_queue_depth}</span> held
            </div>
          </MetricCard>

          {/* Integrity summary */}
          <MetricCard title="Integrity">
            <div className="space-y-2 text-sm">
              <div className="flex justify-between">
                <span className="text-gray-400">Inconclusive beliefs</span>
                <span className="text-orange-300 font-medium tabular-nums">
                  {data.integrity.inconclusive_count}
                </span>
              </div>
              <div className="flex justify-between">
                <span className="text-gray-400">Re-screenings</span>
                <span className="text-blue-300 font-medium tabular-nums">
                  {data.integrity.rescreening_count}
                </span>
              </div>
              <div className="flex justify-between">
                <span className="text-gray-400">Max cascade depth</span>
                <span className="text-gray-200 font-medium tabular-nums">
                  {data.integrity.cascade_depth_max}
                </span>
              </div>
              <div className="flex justify-between">
                <span className="text-gray-400">Avg cascade depth</span>
                <span className="text-gray-200 font-medium tabular-nums">
                  {data.integrity.cascade_depth_avg}
                </span>
              </div>
              <div className="flex justify-between">
                <span className="text-gray-400">Imperative detections</span>
                <span className="text-red-300 font-medium tabular-nums">
                  {data.security.imperative_detections}
                </span>
              </div>
            </div>
          </MetricCard>
        </div>
      )}
    </div>
  )
}
