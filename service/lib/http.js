// http.js : shared request wrapper for the serverless functions. GET only, read only, JSON or plain text.

import { ApiError, requestStats } from './store.js'

// The data changes only when a new build is published, so answers can be cached for a long time.
const CACHE_OK = 'public, max-age=3600, s-maxage=86400, stale-while-revalidate=604800'
const FOOTER = 'Source: public IRS Form 990-PF and Form 990 Schedule I e-file records. No warranty.'

export function endpoint(run, toText) {
  return async function handler(req, res) {
    const t0 = performance.now()
    const url = new URL(req.url, 'http://localhost')
    const params = Object.fromEntries(url.searchParams.entries())
    const wantText = (params.format || '').toLowerCase() === 'text'
    res.setHeader('Access-Control-Allow-Origin', '*')
    res.setHeader('X-Content-Type-Options', 'nosniff')
    if (req.method === 'OPTIONS') { res.setHeader('Access-Control-Allow-Methods', 'GET, OPTIONS'); res.statusCode = 204; return res.end() }
    if (req.method !== 'GET' && req.method !== 'HEAD') return send(res, 405, wantText, { error: 'Only GET is supported.' }, null, t0)
    const stats = { requests: 0, bytes: 0 }
    try {
      const out = await requestStats.run(stats, () => run(params))
      send(res, 200, wantText, out, toText, t0, stats)
    } catch (e) {
      if (e instanceof ApiError) send(res, e.status, wantText, { error: e.message, ...(e.extra || {}) }, null, t0, stats)
      else { console.error(e); send(res, 500, wantText, { error: 'Internal error: ' + (e?.message || String(e)) }, null, t0, stats) }
    }
  }
}

function send(res, status, wantText, obj, toText, t0, stats) {
  const ms = Math.round(performance.now() - t0)
  res.statusCode = status
  res.setHeader('Cache-Control', status === 200 ? CACHE_OK : 'no-store')
  res.setHeader('Server-Timing', `total;dur=${ms}`)
  if (stats) res.setHeader('X-Data-Requests', `${stats.requests}; bytes=${stats.bytes}`)
  if (wantText) {
    res.setHeader('Content-Type', 'text/plain; charset=utf-8')
    let body
    if (status === 200 && toText) body = toText(obj) + '\n\n' + FOOTER + '\n'
    else {
      body = 'ERROR: ' + obj.error + '\n'
      for (const [k, v] of Object.entries(obj)) if (k !== 'error') body += `${k}: ${Array.isArray(v) ? v.join(', ') : v}\n`
    }
    return res.end(body)
  }
  res.setHeader('Content-Type', 'application/json; charset=utf-8')
  if (status === 200) obj = { ...obj, _meta: { elapsed_ms: ms, data_requests: stats?.requests ?? 0, data_bytes: stats?.bytes ?? 0, source: FOOTER } }
  res.end(JSON.stringify(obj))
}
