// store.js : read the index parquet files over HTTP with range requests. No database, no secrets.
//
// DATA_BASE_URL is the URL of the repository's data/ folder, for example
//   https://raw.githubusercontent.com/<owner>/<repo>/main/data
// Everything the service reads lives under <DATA_BASE_URL>/index/ and is described by index/index.json.

import { AsyncLocalStorage } from 'node:async_hooks'
import zlib from 'node:zlib'
import { parquetMetadata, parquetRead } from 'hyparquet'
import { compressors as jsCompressors } from 'hyparquet-compressors'

// Native zstd when the runtime has it (Node 22.15 and later), pure JavaScript otherwise.
const compressors = typeof zlib.zstdDecompressSync === 'function'
  ? { ...jsCompressors, ZSTD: input => zlib.zstdDecompressSync(input) }
  : jsCompressors

export const requestStats = new AsyncLocalStorage()   // per API request: { requests, bytes }

const SMALL_FILE = 1_500_000          // files up to this size are fetched whole in one request
const MAX_RANGES = 6                  // more separate ranges than this: fetch the whole file instead
const RANGE_GAP = 128 * 1024          // merge ranges separated by less than this
const TAIL_BYTES = 256 * 1024         // first guess for the parquet footer
const CACHE_BYTES = Number(process.env.CACHE_BYTES || 192e6)   // bytes of fetched file data kept in memory per warm instance
const INDEX_TTL_MS = 15 * 60 * 1000

export function baseUrl() {
  const b = (process.env.DATA_BASE_URL || '').trim().replace(/\/+$/, '')
  if (!b) throw new ApiError(500, 'DATA_BASE_URL is not set on the server.')
  return b
}

export class ApiError extends Error {
  constructor(status, message, extra) { super(message); this.status = status; this.extra = extra }
}

async function httpGet(path, start, end) {
  const url = baseUrl() + '/' + path
  const headers = {}
  if (process.env.DATA_AUTH_HEADER) headers.Authorization = process.env.DATA_AUTH_HEADER   // optional, only for a private data host
  const ranged = start !== undefined
  if (ranged) { headers.Range = `bytes=${start}-${end - 1}`; headers['Accept-Encoding'] = 'identity' }
  let lastErr
  for (let attempt = 0; attempt < 3; attempt++) {
    try {
      const res = await fetch(url, { headers, redirect: 'follow' })
      if (res.status === 429 || res.status >= 500) { lastErr = new Error(`HTTP ${res.status} from data host`); await sleep(250 * (attempt + 1)); continue }
      if (!res.ok) throw new ApiError(502, `Data host answered HTTP ${res.status} for ${path}`)
      let buf = await res.arrayBuffer()
      const s = requestStats.getStore(); if (s) { s.requests++; s.bytes += buf.byteLength }
      if (ranged && res.status === 200) {
        // the host ignored the Range header and sent the whole file
        return { buf, whole: true }
      }
      return { buf, whole: !ranged }
    } catch (e) {
      if (e instanceof ApiError) throw e
      lastErr = e; await sleep(250 * (attempt + 1))
    }
  }
  throw new ApiError(502, `Could not read ${path} from the data host: ${lastErr?.message || lastErr}`)
}
const sleep = ms => new Promise(r => setTimeout(r, ms))

// ----------------------------------------------------------------------------- byte cache
const files = new Map()   // path -> RemoteFile
let cacheBytes = 0
let clock = 0

function evict() {
  if (cacheBytes <= CACHE_BYTES) return
  const list = [...files.values()].filter(f => f.bytesHeld > 0 && !f.pinned).sort((a, b) => a.used - b.used)
  for (const f of list) {
    if (cacheBytes <= CACHE_BYTES * 0.8) break
    f.drop()
  }
}

class RemoteFile {
  constructor(path, size) {
    this.path = path; this.size = size; this.whole = null; this.wholeP = null; this.chunks = []
    this.md = null; this.mdP = null; this.bytesHeld = 0; this.used = ++clock; this.pinned = false; this.groupStarts = null
  }
  drop() { cacheBytes -= this.bytesHeld; this.bytesHeld = 0; this.whole = null; this.wholeP = null; this.chunks = [] }
  hold(n) { this.bytesHeld += n; cacheBytes += n; this.used = ++clock; evict() }

  fetchWhole() {
    this.used = ++clock
    if (this.whole) return this.whole
    if (!this.wholeP) {
      this.wholeP = httpGet(this.path).then(({ buf }) => {
        this.whole = buf; this.size = buf.byteLength
        cacheBytes -= this.bytesHeld; this.bytesHeld = 0; this.chunks = []
        this.hold(buf.byteLength)
        return buf
      }).catch(e => { this.wholeP = null; throw e })
    }
    return this.wholeP
  }

  covered(start, end) {
    if (this.whole) return { buf: this.whole, start: 0 }
    for (const c of this.chunks) if (c.start <= start && end <= c.end) return c
    return null
  }

  async fetchRange(start, end) {
    const r = await httpGet(this.path, start, end)
    if (r.whole) {
      this.whole = r.buf; this.size = r.buf.byteLength
      cacheBytes -= this.bytesHeld; this.bytesHeld = 0; this.chunks = []
      this.hold(r.buf.byteLength)
    } else {
      this.chunks.push({ start, end: start + r.buf.byteLength, buf: r.buf })
      this.hold(r.buf.byteLength)
    }
  }

  // Make sure every byte range is in memory, with as few HTTP requests as possible.
  async ensure(ranges) {
    this.used = ++clock
    if (this.whole) return
    if (this.wholeP) { await this.wholeP; return }
    const missing = merge(ranges.filter(r => !this.covered(r.start, r.end)))
    if (!missing.length) return
    const bytes = missing.reduce((a, r) => a + r.end - r.start, 0)
    if (!this.size || this.size <= SMALL_FILE || missing.length > MAX_RANGES || bytes > this.size * 0.5) { await this.fetchWhole(); return }
    await Promise.all(missing.map(r => this.fetchRange(r.start, r.end)))
  }

  slice(start, end) {
    this.used = ++clock
    const c = this.covered(start, end)
    if (c) return c.buf.slice(start - c.start, end - c.start)
    return this.fetchRange(start, end).then(() => { const d = this.covered(start, end); return d.buf.slice(start - d.start, end - d.start) })
  }

  asyncBuffer() { return { byteLength: this.size, slice: (s, e) => this.slice(s, e ?? this.size) } }

  metadata() {
    if (this.md) return this.md
    if (!this.mdP) {
      this.mdP = (async () => {
        let tail
        if (!this.size || this.size <= SMALL_FILE) tail = await this.fetchWhole()
        else if (this.whole) tail = this.whole
        else {
          let n = Math.min(this.size, TAIL_BYTES)
          await this.ensure([{ start: this.size - n, end: this.size }])
          if (!this.whole) {
            const last = await this.slice(this.size - 8, this.size)
            const footer = new DataView(last).getUint32(0, true) + 8
            if (footer > n) { n = footer; await this.ensure([{ start: this.size - n, end: this.size }]) }
          }
          tail = this.whole || await this.slice(this.size - n, this.size)
        }
        const md = parquetMetadata(tail)
        const starts = [0]
        for (const g of md.row_groups) starts.push(starts[starts.length - 1] + Number(g.num_rows))
        this.groupStarts = starts
        this.md = md
        return md
      })().catch(e => { this.mdP = null; throw e })
    }
    return this.mdP
  }
}

function merge(ranges) {
  const s = ranges.map(r => ({ ...r })).sort((a, b) => a.start - b.start)
  const out = []
  for (const r of s) {
    const last = out[out.length - 1]
    if (last && r.start <= last.end + RANGE_GAP) last.end = Math.max(last.end, r.end)
    else out.push(r)
  }
  return out
}

function remote(path, size) {
  let f = files.get(path)
  if (!f) { f = new RemoteFile(path, size); files.set(path, f) }
  return f
}

function concat(parts) {
  if (parts.length === 1) return parts[0]
  const n = parts.reduce((a, p) => a + p.length, 0)
  const first = parts.find(p => p.length) || parts[0]
  if (ArrayBuffer.isView(first)) {
    const out = new first.constructor(n); let o = 0
    for (const p of parts) { out.set(p, o); o += p.length }
    return out
  }
  const out = new Array(n); let o = 0
  for (const p of parts) for (let i = 0; i < p.length; i++) out[o++] = p[i]
  return out
}

async function decode(file, md, columns, rowStart, rowEnd) {
  const got = {}
  for (const c of columns) got[c] = []
  await parquetRead({
    file, metadata: md, columns, rowStart, rowEnd, compressors,
    onChunk: ch => { if (got[ch.columnName]) got[ch.columnName].push(ch) },
  })
  const out = {}
  for (const c of columns) {
    const chunks = got[c].sort((a, b) => a.rowStart - b.rowStart)
    out[c] = concat(chunks.map(x => x.columnData))
    out[c].rowStart = chunks.length ? chunks[0].rowStart : rowStart
  }
  return out
}

// ----------------------------------------------------------------------------- tables (one logical table = one or more parquet parts)
export class Table {
  // parts: [{ file, rows, row_start, bytes }]
  constructor(dir, parts) {
    this.parts = parts.map(p => ({ ...p, rf: remote(dir + '/' + p.file, p.bytes) }))
    this.rows = parts.reduce((a, p) => a + p.rows, 0)
  }

  // Whole table, selected columns. One HTTP request per part.
  async readAll(columns) {
    const per = await Promise.all(this.parts.map(async p => {
      const buf = await p.rf.fetchWhole()
      const md = await p.rf.metadata()
      return decode(buf, md, columns, undefined, undefined)
    }))
    const out = {}
    for (const c of columns) out[c] = concat(per.map(x => x[c]))
    return out
  }

  // Rows for a list of [start, end) row ranges (table row numbers). Returns columns holding the rows of every range, in range order.
  async readRanges(columns, ranges) {
    ranges = ranges.filter(r => r[1] > r[0])
    const total = ranges.reduce((a, r) => a + r[1] - r[0], 0)
    const pieces = {}; for (const c of columns) pieces[c] = []
    if (!total) { const out = {}; for (const c of columns) out[c] = []; return out }
    // group cache for this call: part index + group index -> decoded columns
    const need = new Map()
    await Promise.all(this.parts.map(async (p, pi) => {
      const lo = p.row_start, hi = p.row_start + p.rows
      const local = ranges.filter(r => r[1] > lo && r[0] < hi)
      if (!local.length) return
      const md = await p.rf.metadata(); const gs = p.rf.groupStarts
      const groups = new Set()
      for (const r of local) {
        const a = Math.max(r[0], lo) - lo, b = Math.min(r[1], hi) - lo
        let g = upperBound(gs, a) - 1
        for (; g < md.row_groups.length && gs[g] < b; g++) groups.add(g)
      }
      const byteRanges = []
      for (const g of groups) for (const ch of md.row_groups[g].columns) {
        const m = ch.meta_data
        if (!columns.includes(m.path_in_schema[0])) continue
        const start = Number(m.dictionary_page_offset || m.data_page_offset)
        byteRanges.push({ start, end: start + Number(m.total_compressed_size) })
      }
      await p.rf.ensure(byteRanges)
      const file = p.rf.whole || p.rf.asyncBuffer()
      await Promise.all([...groups].map(async g => { need.set(pi + ':' + g, await decode(file, md, columns, gs[g], gs[g + 1])) }))
    }))
    for (const r of ranges) {
      for (let pi = 0; pi < this.parts.length; pi++) {
        const p = this.parts[pi]; const lo = p.row_start, hi = p.row_start + p.rows
        if (!(r[1] > lo && r[0] < hi)) continue
        const gs = p.rf.groupStarts
        const a = Math.max(r[0], lo) - lo, b = Math.min(r[1], hi) - lo
        for (let g = upperBound(gs, a) - 1; g < gs.length - 1 && gs[g] < b; g++) {
          const d = need.get(pi + ':' + g)
          const s = Math.max(a, gs[g]) - gs[g], e = Math.min(b, gs[g + 1]) - gs[g]
          for (const c of columns) pieces[c].push(ArrayBuffer.isView(d[c]) ? d[c].subarray(s, e) : d[c].slice(s, e))
        }
      }
    }
    const out = {}
    for (const c of columns) out[c] = concat(pieces[c])
    return out
  }

  // Rows by row number. Returns Map(row -> object).
  async readRows(columns, ids) {
    const uniq = [...new Set(ids)].filter(i => i >= 0 && i < this.rows).sort((a, b) => a - b)
    const cols = await this.readRanges(columns, uniq.map(i => [i, i + 1]))
    const out = new Map()
    uniq.forEach((id, k) => { const o = {}; for (const c of columns) o[c] = cols[c][k]; out.set(id, o) })
    return out
  }

  // Rows whose sorted integer key column equals one of the values, using row group statistics to pick the groups.
  async readByKey(keyColumn, columns, values) {
    const want = new Set(values); const lo = Math.min(...values), hi = Math.max(...values)
    const rows = []
    await Promise.all(this.parts.map(async p => {
      if (p.key_min !== undefined && (hi < p.key_min || lo > p.key_max)) return
      const md = await p.rf.metadata(); const gs = p.rf.groupStarts
      const ci = md.row_groups[0].columns.findIndex(c => c.meta_data.path_in_schema[0] === keyColumn)
      const ranges = []
      md.row_groups.forEach((g, gi) => {
        const st = g.columns[ci].meta_data.statistics
        if (!st) { ranges.push([gs[gi], gs[gi + 1]]); return }
        const mn = Number(st.min_value), mx = Number(st.max_value)
        for (const v of want) if (v >= mn && v <= mx) { ranges.push([gs[gi], gs[gi + 1]]); break }
      })
      if (!ranges.length) return
      const sub = new Table('', [])
      sub.parts = [{ ...p, row_start: 0 }]; sub.rows = p.rows
      const cols = await sub.readRanges([...new Set([keyColumn, ...columns])], ranges)
      const k = cols[keyColumn]
      for (let i = 0; i < k.length; i++) if (want.has(k[i])) { const o = {}; for (const c of columns) o[c] = cols[c][i]; o[keyColumn] = k[i]; rows.push(o) }
    }))
    return rows
  }
}

function upperBound(arr, v) {   // first index with arr[i] > v
  let lo = 0, hi = arr.length
  while (lo < hi) { const m = (lo + hi) >> 1; if (arr[m] <= v) lo = m + 1; else hi = m }
  return lo
}

// ----------------------------------------------------------------------------- index.json, directory, states
let indexCache = null, indexAt = 0, indexP = null
export async function getIndex() {
  if (indexCache && Date.now() - indexAt < INDEX_TTL_MS) return indexCache
  if (!indexP) {
    indexP = (async () => {
      const { buf } = await httpGet('index/index.json')
      const idx = JSON.parse(new TextDecoder().decode(buf))
      if (indexCache && indexCache.generated !== idx.generated) {   // new data build: forget everything
        for (const f of files.values()) f.drop()
        files.clear(); directoryP = null; stateStores.clear()
      }
      indexCache = idx; indexAt = Date.now()
      return idx
    })().finally(() => { indexP = null })
  }
  return indexP
}

let directoryP = null
// The funder directory (about 259,000 rows, about 10 MB) is read once per warm instance and kept in memory.
export function getDirectory() {
  if (!directoryP) {
    directoryP = (async () => {
      const idx = await getIndex()
      const t = new Table('index', [{ ...idx.directory, row_start: 0 }])
      const d = await t.readAll(['ein', 'is_pf', 'name', 'city', 'state', 'assets', 'assets_any', 'giving_latest', 'fiscal_year_latest', 'efile_missing',
        'only_preselected', 'ever_listed_application_info', 'app_deadlines', 'website', 'top_recipient_states'])
      t.parts[0].rf.drop()   // keep the decoded columns, not the bytes
      const pf = new Map(), pc = new Map()
      for (let i = 0; i < d.ein.length; i++) (d.is_pf[i] ? pf : pc).set(d.ein[i], i)
      d.pf = pf; d.pc = pc; d.n = d.ein.length
      return d
    })().catch(e => { directoryP = null; throw e })
  }
  return directoryP
}

const stateStores = new Map()
export async function getState(st) {
  const idx = await getIndex()
  const meta = idx.states[st]
  if (!meta) return null
  let s = stateStores.get(st)
  if (!s) {
    s = {
      code: st, meta,
      grants: new Table('index/grants', meta.files.grants),
      keys: new Table('index/recipient_keys', meta.files.recipient_keys),
      info: new Table('index/recipient_info', meta.files.recipient_info),
      purposes: new Table('index/purposes', meta.files.purposes),
    }
    stateStores.set(st, s)
  }
  return s
}

export async function getProfilesTable() {
  const idx = await getIndex()
  return new Table('index/funder_profiles', idx.profiles.files.map(f => ({ ...f, row_start: 0, key_min: f.ein_min, key_max: f.ein_max })))
}

export async function getEinTable() {
  const idx = await getIndex()
  return new Table('index', [{ ...idx.recipient_eins, row_start: 0 }])
}

// ----------------------------------------------------------------------------- website profiles
// data/web_profiles/NN.json holds profiles read from funders' own websites, keyed by 9-digit EIN (NN = first two digits).
// Small files, so each shard is fetched whole and kept for INDEX_TTL_MS. A missing shard means no profile.
const webShards = new Map()   // NN -> { at, obj }
export async function getWebProfile(ein) {
  const nn = String(ein).padStart(9, '0').slice(0, 2)
  const hit = webShards.get(nn)
  if (hit && Date.now() - hit.at < INDEX_TTL_MS) return hit.obj[String(ein).padStart(9, '0')] || null
  let obj = {}
  try {
    const { buf } = await httpGet(`web_profiles/${nn}.json`)
    obj = JSON.parse(new TextDecoder().decode(buf))
  } catch (e) {
    if (!(e instanceof ApiError)) throw e   // a missing shard or a data host error must not break the IRS answer
    obj = {}
  }
  webShards.set(nn, { at: Date.now(), obj })
  return obj[String(ein).padStart(9, '0')] || null
}
