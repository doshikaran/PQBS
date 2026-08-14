const BASE = '/api'

export async function fetchBeliefs(tenantId, limit = 50) {
  const params = new URLSearchParams({ limit })
  if (tenantId) params.set('tenant_id', tenantId)
  const r = await fetch(`${BASE}/beliefs?${params}`)
  if (!r.ok) throw new Error(await r.text())
  return r.json()
}

export async function fetchQuarantine(tenantId, limit = 50) {
  const params = new URLSearchParams({ limit })
  if (tenantId) params.set('tenant_id', tenantId)
  const r = await fetch(`${BASE}/quarantine?${params}`)
  if (!r.ok) throw new Error(await r.text())
  return r.json()
}

export async function fetchTemporal(tenantId, asOf, mechanism, subjectFilter = '') {
  const params = new URLSearchParams({
    tenant_id: tenantId,
    as_of: asOf,
    mechanism,
  })
  if (subjectFilter) params.set('subject_filter', subjectFilter)
  const r = await fetch(`${BASE}/temporal?${params}`)
  // 422 is a domain error (MVCC out of range) — still parse JSON
  if (!r.ok && r.status !== 422) throw new Error(await r.text())
  return r.json()
}

export async function postRecall(tenantId, query, limit = 10) {
  const r = await fetch(`${BASE}/recall`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ query, tenant_id: tenantId, limit }),
  })
  if (!r.ok) throw new Error(await r.text())
  return r.json()
}

export async function fetchMetrics(tenantId) {
  const params = new URLSearchParams()
  if (tenantId) params.set('tenant_id', tenantId)
  const r = await fetch(`${BASE}/metrics?${params}`)
  if (!r.ok) throw new Error(await r.text())
  return r.json()
}

export async function fetchHealth() {
  const r = await fetch(`${BASE}/health`)
  if (!r.ok) throw new Error(await r.text())
  return r.json()
}
