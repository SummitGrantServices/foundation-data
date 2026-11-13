// core.js : the five queries. Each one mirrors the matching command of the fdb.py command-line tool.

import { ApiError, getIndex, getDirectory, getState, getProfilesTable, getEinTable } from './store.js'

const MAX_LIMIT = 100
const MAX_PEERS = 12
const MAX_STATES_PER_EIN = 12        // funders-of by EIN reads at most this many state partitions
const GRANT_COLUMNS = ['funder_id', 'is_pf', 'fiscal_year', 'amount', 'tag_mask', 'county_id', 'rid', 'rk', 'purpose_id', 'rein']

// ----------------------------------------------------------------------------- helpers
export function nameKey(s) {
  if (s == null) return null
  s = s.normalize('NFKD').replace(/\p{M}/gu, '').toUpperCase().replace(/&/g, ' AND ')
  s = s.replace(/['’`.]/g, '').replace(/[^A-Z0-9 ]/g, ' ').replace(/ +/g, ' ').trim()
  s = s.replace(/^THE /, '')
  return s || null
}

export function asEin(s) {
  s = (s || '').trim()
  const d = s.replace(/[^0-9]/g, '')
  return d.length === 9 && /^[0-9\- ]+$/.test(s) ? d : null
}

const ein9 = n => String(n).padStart(9, '0')
const nan = v => (v === null || v === undefined || Number.isNaN(v) ? null : v)
const tri = v => (v === 1 ? true : v === 0 ? false : null)

function parseYears(s) {
  if (!s) return null
  const m = /^\s*(\d{4})\s*(?:-\s*(\d{4}))?\s*$/.exec(s)
  if (!m) throw new ApiError(400, 'years must look like 2022-2025 or 2024')
  const a = Number(m[1]), b = Number(m[2] || m[1])
  return [Math.min(a, b), Math.max(a, b)]
}

function parseLimit(v, dflt) {
  if (v === undefined || v === null || v === '') return dflt
  const n = parseInt(v, 10)
  if (!Number.isFinite(n) || n < 1) throw new ApiError(400, 'limit must be a positive whole number')
  return Math.min(n, MAX_LIMIT)
}

function parseState(v) {
  if (!v) return null
  const s = String(v).trim().toUpperCase()
  if (!/^[A-Z]{2}$/.test(s)) throw new ApiError(400, 'state must be a two-letter code, for example FL. Use XX for recipients with no US state.')
  return s
}

function median(a) {
  if (!a.length) return null
  a.sort((x, y) => x - y)
  const m = a.length >> 1
  return a.length % 2 ? a[m] : (a[m - 1] + a[m]) / 2
}

function modeOf(map) {   // Map(value -> count) -> value with the highest count
  let best = null, n = 0
  for (const [k, c] of map) if (c > n) { best = k; n = c }
  return best
}

function card(dir, funderId, isPf) {
  const i = (isPf ? dir.pf : dir.pc).get(funderId)
  return i === undefined ? null : i
}

function fmtDollars(v) { return '$' + Math.round(v).toLocaleString('en-US') }

// ----------------------------------------------------------------------------- info
export async function info() {
  const idx = await getIndex()
  return {
    dataset: 'US grantmakers and their grants, compiled from public IRS Form 990-PF and Form 990 Schedule I e-file records',
    build_date: idx.build_date,
    index_generated: idx.generated,
    row_counts: idx.row_counts,
    states: Object.keys(idx.states),
    tags: idx.tags.map(t => ({ tag: t.tag, label: t.label, grants: t.grants })),
    endpoints: {
      '/api/foundation': 'q = funder EIN or name fragment; optional state, pick (1 = first name match). Profile, giving by year, application information, officers, top recipients, latest grants.',
      '/api/funders-of': 'q = recipient EIN or name fragment (a name needs state); optional state, years, limit. Who has funded this organization.',
      '/api/search': 'state (required); optional county, tags (comma separated tag names; a single word such as aging also works when only one tag fits), all_tags=1, text, years (2022-2025), preselected (yes, no, any), min_grant, funder_type (pf, pc, any), sort (dollars, count), limit, examples. Funders ranked by matching grants.',
      '/api/peers-funders': 'eins = comma separated recipient EINs (up to 12); optional min_peers (default 2), years, limit. Funders that support several of these organizations.',
      '/api/info': 'This description.',
    },
    formats: 'Every endpoint returns JSON. Add format=text for compact plain text.',
    notes: [
      'Only grants with an amount above zero and no amount flag are searched.',
      'preselected=no keeps private foundations whose latest return does not check the "preselected organizations only" box, plus public charities.',
      'A grant can carry several tags. Tags are keyword matches and contain false positives and misses.',
      'Compiled from public IRS records. No warranty. Verify against the funder before relying on a figure.',
    ],
    caveats: idx.caveats,
  }
}

// ----------------------------------------------------------------------------- foundation
export async function foundation(p) {
  const term = (p.q || '').trim()
  if (!term) throw new ApiError(400, 'q is required: a 9-digit EIN or part of a funder name.')
  if (term.length > 120) throw new ApiError(400, 'q is too long (120 characters at most).')
  const state = parseState(p.state)
  let ein = asEin(term), cands = [], wantPf = null
  if (!ein) {
    const dir = await getDirectory()
    const pattern = term.split(' ').map(w => w.replace(/[.*+?^${}()|[\]\\]/g, '\\$&').replace(/%/g, '.*').replace(/_/g, '.')).join('.*')
    const re = new RegExp(pattern, 'is')
    const hits = []
    for (let i = 0; i < dir.n; i++) {
      if (state && dir.state[i] !== state) continue
      if (re.test(dir.name[i])) hits.push(i)
    }
    const a = i => (Number.isNaN(dir.assets_any[i]) ? -Infinity : dir.assets_any[i])
    hits.sort((x, y) => a(y) - a(x) || (dir.is_pf[y] ? 1 : 0) - (dir.is_pf[x] ? 1 : 0) || dir.ein[x] - dir.ein[y])
    cands = hits.slice(0, 25).map(i => ({
      funder_type: dir.is_pf[i] ? 'private_foundation' : 'public_charity', ein: ein9(dir.ein[i]), name: dir.name[i], city: dir.city[i] || null, state: dir.state[i] || null,
      assets: nan(dir.assets_any[i]), giving: nan(dir.giving_latest[i]), fiscal_year_latest: dir.fiscal_year_latest[i] || null, efile_missing: !!dir.efile_missing[i],
    }))
    if (!cands.length) return { query: term, state, matches: [], total_name_matches: 0 }
    const pick = cands[Math.min(Math.max(parseInt(p.pick || '1', 10) || 1, 1), cands.length) - 1]
    ein = pick.ein; wantPf = pick.funder_type === 'private_foundation'
    cands.total = hits.length
  }
  const t = await getProfilesTable()
  const rows = await t.readByKey('ein', ['is_pf', 'profile', 'top_recipients', 'top_tags', 'latest_grants'], [Number(ein)])
  let row = wantPf === null ? (rows.find(r => r.is_pf) || rows[0]) : rows.find(r => !!r.is_pf === wantPf)
  if (!row) return { query: term, state, matches: [] }
  const prof = JSON.parse(row.profile)
  let byYear = {}
  try { byYear = prof.giving_by_year ? JSON.parse(prof.giving_by_year) : {} } catch { byYear = {} }
  return {
    funder_type: row.is_pf ? 'private_foundation' : 'public_charity',
    profile: prof,
    giving_by_year: byYear,
    top_recipients: JSON.parse(row.top_recipients),
    top_tags: JSON.parse(row.top_tags),
    latest_grants: JSON.parse(row.latest_grants),
    other_matches: cands.filter(c => !(c.ein === ein && (c.funder_type === 'private_foundation') === !!row.is_pf)),
    total_name_matches: cands.total,
    also_listed_as: rows.length > 1 ? (row.is_pf ? 'public_charity' : 'private_foundation') : undefined,
  }
}

// ----------------------------------------------------------------------------- shared: aggregate grant rows by funder
function yearOk(yrs, y) { return !yrs || (y >= yrs[0] && y <= yrs[1]) }

// ----------------------------------------------------------------------------- funders-of
export async function fundersOf(p) {
  const term = (p.q || '').trim()
  if (!term) throw new ApiError(400, 'q is required: a recipient EIN or part of a recipient name.')
  if (term.length > 120) throw new ApiError(400, 'q is too long (120 characters at most).')
  const state = parseState(p.state), yrs = parseYears(p.years), limit = parseLimit(p.limit, 40)
  const ein = asEin(term)
  const notes = []
  // parts: [{ st, cols }] where cols hold the candidate grant rows of one state
  const parts = []
  if (ein) {
    const et = await getEinTable()
    let hits = await et.readByKey('ein', ['state', 'rid', 'row_start', 'n_rows', 'n_grants'], [Number(ein)])
    if (state) hits = hits.filter(h => h.state === state)
    const byState = new Map()
    for (const h of hits) { if (!byState.has(h.state)) byState.set(h.state, { n: 0, ranges: [] }); const s = byState.get(h.state); s.n += h.n_grants; s.ranges.push([h.row_start, h.row_start + h.n_rows]) }
    const order = [...byState.entries()].sort((a, b) => b[1].n - a[1].n)
    if (order.length > MAX_STATES_PER_EIN) {
      const skipped = order.slice(MAX_STATES_PER_EIN)
      notes.push(`This EIN appears in ${order.length} states. Read the ${MAX_STATES_PER_EIN} with the most grants; skipped ${skipped.map(s => s[0]).join(', ')} (${skipped.reduce((a, s) => a + s[1].n, 0)} grants). Add state=XX to read one of them.`)
    }
    await Promise.all(order.slice(0, MAX_STATES_PER_EIN).map(async ([st, v]) => {
      const store = await getState(st)
      const cols = await store.grants.readRanges(GRANT_COLUMNS, v.ranges.sort((a, b) => a[0] - b[0]))
      parts.push({ st, store, cols, test: i => cols.rein[i] === Number(ein) })
    }))
  } else {
    if (!state) throw new ApiError(400, 'A recipient name needs a state, for example state=FL. A 9-digit recipient EIN works without a state.', { hint: 'Run the name with a state first; the answer lists each matched recipient with its EIN.' })
    const key = nameKey(term)
    if (!key) throw new ApiError(400, 'Give a recipient name fragment or a 9-digit EIN.')
    const store = await getState(state)
    if (!store) throw new ApiError(404, `No grants index for state ${state}.`, { states: Object.keys((await getIndex()).states) })
    const k = await store.keys.readAll(['recipient_key', 'row_start', 'n_rows'])
    const ranges = []
    for (let i = 0; i < k.recipient_key.length; i++) if (k.recipient_key[i].includes(key)) ranges.push([k.row_start[i], k.row_start[i] + k.n_rows[i]])
    if (ranges.length) {
      const rowCount = ranges.reduce((a, r) => a + r[1] - r[0], 0)
      const cols = rowCount > store.grants.rows * 0.3 ? null : await store.grants.readRanges(GRANT_COLUMNS, ranges)
      if (cols) parts.push({ st: state, store, cols, test: () => true })
      else {
        // very broad fragment: scan the whole state once
        const all = await store.grants.readAll(GRANT_COLUMNS)
        const ok = new Uint8Array(store.grants.rows)
        for (const r of ranges) ok.fill(1, r[0], r[1])
        parts.push({ st: state, store, cols: all, test: i => ok[i] === 1 })
      }
    }
  }
  // aggregate
  const funders = new Map()     // funder_id -> agg
  const recips = new Map()      // st + ':' + rk -> agg
  let totGrants = 0, totDollars = 0
  for (const part of parts) {
    const c = part.cols, n = c.funder_id.length
    for (let i = 0; i < n; i++) {
      if (!part.test(i) || !yearOk(yrs, c.fiscal_year[i])) continue
      const amt = c.amount[i], fid = c.funder_id[i]
      totGrants++; totDollars += amt
      let f = funders.get(fid)
      if (!f) { f = { fid, isPf: !!c.is_pf[i], dollars: 0, grants: 0, first: 9999, last: 0, years: new Map(), purposes: new Map(), rks: new Set() }; funders.set(fid, f) }
      f.dollars += amt; f.grants++
      const y = c.fiscal_year[i]
      if (y < f.first) f.first = y
      if (y > f.last) f.last = y
      f.years.set(y, (f.years.get(y) || 0) + amt)
      if (c.purpose_id[i] >= 0) { const pk = part.st + ':' + c.purpose_id[i]; f.purposes.set(pk, (f.purposes.get(pk) || 0) + 1) }
      const rkey = part.st + ':' + c.rk[i]
      f.rks.add(rkey)
      let r = recips.get(rkey)
      if (!r) { r = { st: part.st, grants: 0, dollars: 0, funders: new Set(), rids: new Map(), eins: new Map() }; recips.set(rkey, r) }
      r.grants++; r.dollars += amt; r.funders.add(fid)
      r.rids.set(c.rid[i], (r.rids.get(c.rid[i]) || 0) + 1)
      if (c.rein[i] >= 0) r.eins.set(c.rein[i], (r.eins.get(c.rein[i]) || 0) + 1)
    }
  }
  const dir = await getDirectory()
  const topF = [...funders.values()].sort((a, b) => b.dollars - a.dollars || b.grants - a.grants || a.fid - b.fid).slice(0, limit)
  const topR = [...recips.values()].sort((a, b) => b.dollars - a.dollars || b.grants - a.grants).slice(0, 15)
  // look up display texts: recipient names and typical purposes
  const needInfo = new Map(), needPurpose = new Map()
  for (const r of topR) { r.rid = modeOf(r.rids); add(needInfo, r.st, r.rid) }
  for (const f of topF) { f.pk = modeOf(f.purposes); if (f.pk) { const [st, pid] = f.pk.split(':'); add(needPurpose, st, Number(pid)) } }
  const infoBy = new Map(), purposeBy = new Map()
  await Promise.all([
    ...[...needInfo].map(async ([st, ids]) => infoBy.set(st, await (await getState(st)).info.readRows(['name', 'city'], [...ids]))),
    ...[...needPurpose].map(async ([st, ids]) => purposeBy.set(st, await (await getState(st)).purposes.readRows(['purpose'], [...ids]))),
  ])
  return {
    query: term, state, years: p.years || undefined,
    totals: { grants: totGrants, dollars: totDollars, funders: funders.size },
    recipients_matched: topR.map(r => {
      const inf = infoBy.get(r.st)?.get(r.rid) || {}
      const e = modeOf(r.eins)
      return { recipient_name: inf.name || null, city: inf.city || null, state: r.st === 'XX' ? null : r.st, ein: e === null ? null : ein9(e), grants: r.grants, dollars: r.dollars, funders: r.funders.size }
    }),
    recipients_matched_total: recips.size,
    funders: topF.map(f => {
      const i = card(dir, f.fid, f.isPf) ?? card(dir, f.fid, !f.isPf)
      let purpose = null
      if (f.pk) { const [st, pid] = f.pk.split(':'); purpose = purposeBy.get(st)?.get(Number(pid))?.purpose ?? null }
      return {
        funder_ein: ein9(f.fid), funder_name: i === null ? null : dir.name[i], funder_type: f.isPf ? 'private_foundation' : 'public_charity',
        funder_city: i === null ? null : dir.city[i] || null, funder_state: i === null ? null : dir.state[i] || null,
        dollars: f.dollars, grants: f.grants, first_year: f.first, last_year: f.last,
        by_year: [...f.years.entries()].sort((a, b) => b[0] - a[0]).map(([y, d]) => `FY${y} ${fmtDollars(d)}`).join('; '),
        typical_purpose: purpose, recipient_names_matched: f.rks.size,
        only_preselected: i !== null && f.isPf ? tri(dir.only_preselected[i]) : null,
      }
    }),
    notes: notes.length ? notes : undefined,
  }
}

function add(map, k, v) { if (!map.has(k)) map.set(k, new Set()); map.get(k).add(v) }

// ----------------------------------------------------------------------------- search
export async function search(p) {
  const idx = await getIndex()
  const state = parseState(p.state)
  if (!state) {
    throw new ApiError(400, 'state is required for /api/search (one state partition per request). To look up one funder use /api/foundation; to find who funds one recipient use /api/funders-of with its EIN.',
      { states: Object.keys(idx.states) })
  }
  const store = await getState(state)
  if (!store) throw new ApiError(404, `No grants index for state ${state}.`, { states: Object.keys(idx.states) })
  const yrs = parseYears(p.years), limit = parseLimit(p.limit, 25)
  const nEx = Math.min(Math.max(p.examples === undefined || p.examples === '' ? 3 : parseInt(p.examples, 10) || 0, 0), 5)
  const pre = (p.preselected || 'any').toLowerCase()
  if (!['yes', 'no', 'any'].includes(pre)) throw new ApiError(400, 'preselected must be yes, no or any')
  const ftype = (p.funder_type || 'any').toLowerCase()
  if (!['pf', 'pc', 'any'].includes(ftype)) throw new ApiError(400, 'funder_type must be pf, pc or any')
  const sort = (p.sort || 'dollars').toLowerCase()
  const minGrant = p.min_grant === undefined || p.min_grant === '' ? null : Number(p.min_grant)
  if (minGrant !== null && !Number.isFinite(minGrant)) throw new ApiError(400, 'min_grant must be a number')
  // county: prefix match on the county name, case insensitive
  let countyOk = null, countyNames = null
  if (p.county) {
    const cn = String(p.county).trim().replace(/\s+(county|parish|borough)$/i, '').toLowerCase()
    countyOk = new Uint8Array(store.meta.counties.length + 1)
    countyNames = []
    store.meta.counties.forEach((c, i) => { if (c.toLowerCase().startsWith(cn)) { countyOk[i] = 1; countyNames.push(c) } })
    if (!countyNames.length) throw new ApiError(404, `No county in ${state} starts with "${p.county}".`, { counties: store.meta.counties })
  }
  // tags
  let mask = 0, allTags = ['1', 'true', 'yes'].includes(String(p.all_tags || '').toLowerCase()), tagList = []
  if (p.tags) {
    const asked = String(p.tags).split(',').map(t => t.trim().toLowerCase().replace(/[^a-z0-9]+/g, '_').replace(/^_+|_+$/g, '')).filter(Boolean)
    const bad = []
    for (const t of asked) {
      // exact tag name first, then a name or label that contains the words given, when only one tag fits
      let hit = idx.tags.filter(x => x.tag === t)
      if (!hit.length) hit = idx.tags.filter(x => ('_' + x.tag + '_').includes('_' + t + '_') || ('_' + x.label.toLowerCase().replace(/[^a-z0-9]+/g, '_') + '_').includes('_' + t + '_'))
      if (hit.length === 1) { tagList.push(hit[0].tag); mask |= 1 << hit[0].bit } else bad.push(t)
    }
    if (bad.length) throw new ApiError(400, `Unknown or ambiguous tag(s): ${bad.join(', ')}.`, { valid_tags: idx.tags.map(t => t.tag) })
  }
  // text: every word must appear in the purpose or in the recipient name
  const words = p.text ? String(p.text).toUpperCase().split(/\s+/).filter(Boolean) : []
  if (words.length > 8) throw new ApiError(400, 'text accepts at most 8 words.')
  const [g, dir, textIdx] = await Promise.all([
    store.grants.readAll(GRANT_COLUMNS.filter(c => c !== 'rein')),
    getDirectory(),
    words.length ? Promise.all([store.purposes.readAll(['purpose']), store.info.readAll(['name'])]) : null,
  ])
  let pHit = null, rHit = null
  if (textIdx) {
    const purposes = textIdx[0].purpose, names = textIdx[1].name
    pHit = words.map(w => { const a = new Uint8Array(purposes.length); for (let i = 0; i < purposes.length; i++) if (purposes[i].toUpperCase().includes(w)) a[i] = 1; return a })
    rHit = words.map(w => { const a = new Uint8Array(names.length); for (let i = 0; i < names.length; i++) if (names[i].toUpperCase().includes(w)) a[i] = 1; return a })
  }
  const n = g.funder_id.length
  const nullRk = store.meta.null_key_rk
  const funders = new Map()
  let totGrants = 0, totDollars = 0
  rows: for (let i = 0; i < n; i++) {
    if (yrs && (g.fiscal_year[i] < yrs[0] || g.fiscal_year[i] > yrs[1])) continue
    if (countyOk && !(g.county_id[i] >= 0 && countyOk[g.county_id[i]])) continue
    if (mask && (allTags ? (g.tag_mask[i] & mask) !== mask : (g.tag_mask[i] & mask) === 0)) continue
    const amt = g.amount[i]
    if (minGrant !== null && amt < minGrant) continue
    if (ftype === 'pf' && !g.is_pf[i]) continue
    if (ftype === 'pc' && g.is_pf[i]) continue
    if (pHit) for (let w = 0; w < words.length; w++) if (!((g.purpose_id[i] >= 0 && pHit[w][g.purpose_id[i]]) || rHit[w][g.rid[i]])) continue rows
    totGrants++; totDollars += amt
    const fid = g.funder_id[i]
    let f = funders.get(fid)
    if (!f) { f = { fid, isPf: !!g.is_pf[i], dollars: 0, grants: 0, first: 9999, last: 0, amounts: [], rks: new Set(), ex: [] }; funders.set(fid, f) }
    f.dollars += amt; f.grants++; f.amounts.push(amt)
    const y = g.fiscal_year[i]
    if (y < f.first) f.first = y
    if (y > f.last) f.last = y
    if (g.rk[i] !== nullRk) f.rks.add(g.rk[i])
    if (nEx && (f.ex.length < nEx || amt > g.amount[f.ex[f.ex.length - 1]])) {
      f.ex.push(i); f.ex.sort((a, b) => g.amount[b] - g.amount[a] || a - b)
      if (f.ex.length > nEx) f.ex.pop()
    }
  }
  let list = []
  for (const f of funders.values()) {
    const i = card(dir, f.fid, f.isPf)
    const only = i !== null && f.isPf ? dir.only_preselected[i] === 1 : false
    if (pre === 'yes' && !only) continue
    if (pre === 'no' && only) continue
    f.card = i; list.push(f)
  }
  const afterFilter = list.length
  list.sort(sort === 'count' ? (a, b) => b.grants - a.grants || b.dollars - a.dollars || a.fid - b.fid : (a, b) => b.dollars - a.dollars || b.grants - a.grants || a.fid - b.fid)
  list = list.slice(0, limit)
  const rids = new Set(), pids = new Set()
  for (const f of list) for (const i of f.ex) { rids.add(g.rid[i]); if (g.purpose_id[i] >= 0) pids.add(g.purpose_id[i]) }
  const [infoBy, purposeBy] = await Promise.all([
    rids.size ? store.info.readRows(['name', 'city'], [...rids]) : new Map(),
    pids.size ? store.purposes.readRows(['purpose'], [...pids]) : new Map(),
  ])
  const filters = {}
  for (const k of ['state', 'county', 'tags', 'all_tags', 'text', 'min_grant', 'years', 'preselected', 'funder_type']) if (p[k] !== undefined && p[k] !== '') filters[k] = p[k]
  filters.state = state
  if (tagList.length) filters.tags = tagList.join(',')
  return {
    filters,
    counties_matched: countyNames || undefined,
    totals_before_preselected_filter: { grants: totGrants, dollars: totDollars, funders: funders.size },
    funders_after_preselected_filter: afterFilter,
    funders: list.map(f => {
      const i = f.card
      return {
        funder_ein: ein9(f.fid), funder_name: i === null ? null : dir.name[i], funder_type: f.isPf ? 'private_foundation' : 'public_charity',
        funder_city: i === null ? null : dir.city[i] || null, funder_state: i === null ? null : dir.state[i] || null,
        dollars: f.dollars, grants: f.grants, recipients: f.rks.size, first_year: f.first, last_year: f.last, median_matching_grant: median(f.amounts),
        assets: i === null ? null : nan(dir.assets[i]), total_giving_latest: i === null ? null : nan(dir.giving_latest[i]),
        only_preselected: i !== null && f.isPf ? tri(dir.only_preselected[i]) : null,
        ever_listed_application_info: i !== null && f.isPf ? tri(dir.ever_listed_application_info[i]) : null,
        app_deadlines: i !== null && f.isPf ? dir.app_deadlines[i] || null : null,
        website: i === null ? null : dir.website[i] || null, top_recipient_states: i === null ? null : dir.top_recipient_states[i] || null,
        examples: f.ex.map(r => {
          const inf = infoBy.get(g.rid[r]) || {}
          return { fy: g.fiscal_year[r], recipient: inf.name || null, city: inf.city || null, amount: g.amount[r], purpose: g.purpose_id[r] >= 0 ? purposeBy.get(g.purpose_id[r])?.purpose ?? null : null }
        }),
      }
    }),
  }
}

// ----------------------------------------------------------------------------- peers-funders
export async function peersFunders(p) {
  const raw = String(p.eins || '').trim().split(/[,\s]+/).filter(Boolean)
  const eins = raw.map(asEin)
  if (!eins.length || eins.includes(null)) throw new ApiError(400, 'eins needs comma separated 9-digit EINs of recipient organizations.')
  if (eins.length > MAX_PEERS) throw new ApiError(400, `At most ${MAX_PEERS} EINs per request.`)
  const yrs = parseYears(p.years), limit = parseLimit(p.limit, 40)
  const minPeers = Math.max(parseInt(p.min_peers || '2', 10) || 2, 1)
  const want = new Set(eins.map(Number))
  const et = await getEinTable()
  const hits = await et.readByKey('ein', ['state', 'rid', 'row_start', 'n_rows', 'n_grants'], [...want])
  const byState = new Map()
  for (const h of hits) { if (!byState.has(h.state)) byState.set(h.state, []); byState.get(h.state).push([h.row_start, h.row_start + h.n_rows]) }
  const peers = new Map()      // ein -> agg
  const funders = new Map()    // funder_id -> { isPf, peers: Map(ein -> {d, n, ly}) }
  await Promise.all([...byState].map(async ([st, ranges]) => {
    const store = await getState(st)
    const merged = mergeRows(ranges)
    const c = await store.grants.readRanges(['funder_id', 'is_pf', 'fiscal_year', 'amount', 'rid', 'rein'], merged)
    for (let i = 0; i < c.funder_id.length; i++) {
      const e = c.rein[i]
      if (!want.has(e) || !yearOk(yrs, c.fiscal_year[i])) continue
      const amt = c.amount[i], fid = c.funder_id[i], y = c.fiscal_year[i]
      let pe = peers.get(e)
      if (!pe) { pe = { ein: e, grants: 0, dollars: 0, funders: new Set(), names: new Map(), states: new Map() }; peers.set(e, pe) }
      pe.grants++; pe.dollars += amt; pe.funders.add(fid)
      const nk = st + ':' + c.rid[i]
      pe.names.set(nk, (pe.names.get(nk) || 0) + 1); pe.states.set(st, (pe.states.get(st) || 0) + 1)
      let f = funders.get(fid)
      if (!f) { f = { fid, isPf: !!c.is_pf[i], peers: new Map() }; funders.set(fid, f) }
      let fp = f.peers.get(e)
      if (!fp) { fp = { d: 0, n: 0, ly: 0 }; f.peers.set(e, fp) }
      fp.d += amt; fp.n++; if (y > fp.ly) fp.ly = y
    }
  }))
  // peer display names
  const need = new Map()
  for (const pe of peers.values()) { pe.nk = modeOf(pe.names); const [st, rid] = pe.nk.split(':'); add(need, st, Number(rid)) }
  const infoBy = new Map()
  await Promise.all([...need].map(async ([st, ids]) => infoBy.set(st, await (await getState(st)).info.readRows(['name'], [...ids]))))
  for (const pe of peers.values()) { const [st, rid] = pe.nk.split(':'); pe.name = infoBy.get(st)?.get(Number(rid))?.name || null; pe.state = modeOf(pe.states) }
  const dir = await getDirectory()
  const rows = []
  for (const f of funders.values()) {
    if (f.peers.size < minPeers) continue
    let dollars = 0, grants = 0, last = 0
    for (const v of f.peers.values()) { dollars += v.d; grants += v.n; if (v.ly > last) last = v.ly }
    rows.push({ f, dollars, grants, last })
  }
  rows.sort((a, b) => b.f.peers.size - a.f.peers.size || b.dollars - a.dollars || a.f.fid - b.f.fid)
  return {
    peers: [...peers.values()].sort((a, b) => b.dollars - a.dollars).map(pe => ({ ein: ein9(pe.ein), name: pe.name, state: pe.state === 'XX' ? null : pe.state, grants: pe.grants, dollars: pe.dollars, funders: pe.funders.size })),
    peers_with_no_grants_found: eins.filter(e => !peers.has(Number(e))),
    funders_total: rows.length,
    funders: rows.slice(0, limit).map(({ f, dollars, grants, last }) => {
      // same rule as fdb.py: a private foundation with an e-filed return wins, otherwise the public charity record
      let i = card(dir, f.fid, true), isPf = true
      if (i === null || dir.efile_missing[i]) { i = card(dir, f.fid, false); isPf = false }
      return {
        funder_ein: ein9(f.fid), funder_name: i === null ? null : dir.name[i], funder_type: isPf ? 'private_foundation' : 'public_charity',
        funder_city: i === null ? null : dir.city[i] || null, funder_state: i === null ? null : dir.state[i] || null,
        peers_funded: f.peers.size, dollars, grants, last_year: last, assets: i === null ? null : nan(dir.assets[i]),
        only_preselected: i !== null && isPf ? tri(dir.only_preselected[i]) : null,
        ever_listed_application_info: i !== null && isPf ? tri(dir.ever_listed_application_info[i]) : null,
        website: i === null ? null : dir.website[i] || null,
        peers: [...f.peers.entries()].sort((a, b) => b[1].d - a[1].d).map(([e, v]) => `${peers.get(e).name} ${fmtDollars(v.d)} (${v.n}, last FY${v.ly})`).join('; '),
      }
    }),
  }
}

function mergeRows(ranges) {
  const s = ranges.map(r => [...r]).sort((a, b) => a[0] - b[0]); const out = []
  for (const r of s) { const l = out[out.length - 1]; if (l && r[0] <= l[1]) l[1] = Math.max(l[1], r[1]); else out.push(r) }
  return out
}
