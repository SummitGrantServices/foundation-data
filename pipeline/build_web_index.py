#!/usr/bin/env python3
"""
build_web_index.py : build the small integer-coded index files in data/index/ that the HTTP search service reads.

The parquet exports in data/ are laid out for analysis (row groups of about 100,000 rows, sorted by fiscal year and amount).
A serverless function cannot scan them inside a request, so this script writes a second, compact layout:

  index/index.json                      manifest: tags, states, counties, files, row counts (the service loads it first)
  index/funder_directory.parquet        one row per funder (private foundation or grantmaking public charity): the "card"
  index/funder_profiles/p_NN.parquet    one row per funder, sorted by EIN: full profile and precomputed summaries as JSON text
  index/grants/<ST>[_NN].parquet        one row per grant with amount > 0 and no amount flag, integers only, sorted by recipient
  index/recipient_keys/<ST>.parquet     one row per recipient (normalized name + city) in the state: key and pointer into grants
  index/recipient_info/<ST>.parquet     same rows: display name, city, EIN, totals
  index/purposes/<ST>.parquet           distinct purpose texts in the state; row number = purpose id
  index/recipient_eins.parquet          recipient EIN -> state and row range in index/grants, sorted by EIN

Usage:
  python3 pipeline/build_web_index.py --db db/fdb.duckdb --out data/index [steps ...]
  steps: states directory profiles eins manifest   (default: all, in that order)
  --only FL,IL   limit the "states" step to some states (for testing)
  --work DIR     working files (default ./index_work); nothing in it is published
"""
import os, sys, json, time, argparse, glob, hashlib
import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

MAX_FILE_BYTES = 18 * 1024 * 1024        # split index files above this size (keeps every file CDN friendly)
GRANT_GROUP = 8192                        # row group size for index/grants (point reads by row range)
INFO_GROUP = 4096                         # row group size for recipient_info and purposes (point reads by row number)
PROFILE_GROUP = 200                       # row group size for funder_profiles (point reads by EIN)
EIN_GROUP = 16384                         # row group size for recipient_eins (binary search on EIN statistics)
TOP_RECIPIENTS = 15
LATEST_GRANTS = 25
TOP_TAGS = 8
SM = 'index_work/state_meta'       # working files (not published); set from --work in main()

def log(*a):
    print(time.strftime('%H:%M:%S'), *a, flush=True)

def connect(db):
    con = duckdb.connect(db, read_only=True)
    con.execute("PRAGMA threads=2"); con.execute("PRAGMA memory_limit='4GB'")
    tmp = os.environ.get('FDB_TMP') or os.path.join(os.path.dirname(os.path.abspath(db)), '..', 'duck_tmp')
    os.makedirs(tmp, exist_ok=True)
    con.execute(f"SET temp_directory='{os.path.abspath(tmp)}'")
    con.execute("SET preserve_insertion_order=true")
    return con

def arrow(con, sql, params=None):
    rel = con.execute(sql, params or [])
    for name in ('to_arrow_table', 'fetch_arrow_table', 'arrow'):
        fn = getattr(rel, name, None)
        if fn:
            t = fn()
            return t.read_all() if hasattr(t, 'read_all') else t
    raise RuntimeError('no arrow fetch method')

def required(table):
    """Mark every column REQUIRED (no nulls) so readers get plain typed arrays."""
    for c in table.column_names:
        if table[c].null_count:
            raise RuntimeError(f'column {c} has nulls')
    return table.cast(pa.schema([pa.field(f.name, f.type, nullable=False) for f in table.schema]))

def write(table, path, group, dictionary=None, stats=False, level=15):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + '.tmp'
    pq.write_table(table, tmp, row_group_size=group, compression='zstd', compression_level=level,
                   use_dictionary=dictionary if dictionary is not None else False, write_statistics=stats,
                   data_page_size=64 * 1024 * 1024, version='1.0', data_page_version='1.0', store_schema=False)
    os.replace(tmp, path)
    return os.path.getsize(path)

def write_split(table, base, group, **kw):
    """Write one file, or numbered parts when the file would pass MAX_FILE_BYTES. Returns [{file, rows, row_start, bytes}]."""
    path = base + '.parquet'
    size = write(table, path, group, **kw)
    if size <= MAX_FILE_BYTES:
        return [{'file': os.path.basename(path), 'rows': table.num_rows, 'row_start': 0, 'bytes': size}]
    os.replace(path, path + '.whole')
    parts = -(-size // int(MAX_FILE_BYTES * 0.9))
    while True:
        per = -(-table.num_rows // parts); per = -(-per // group) * group
        out = []; ok = True
        for i, start in enumerate(range(0, table.num_rows, per), 1):
            p = f'{base}_{i:02d}.parquet'
            sz = write(table.slice(start, per), p, group, **kw)
            out.append({'file': os.path.basename(p), 'rows': min(per, table.num_rows - start), 'row_start': start, 'bytes': sz})
            ok = ok and sz <= MAX_FILE_BYTES
        if ok: break
        parts += 1
    os.replace(path + '.whole', os.path.join(os.path.dirname(path), '.' + os.path.basename(path) + '.superseded'))
    return out

# ----------------------------------------------------------------------------- states
def tag_list(con):
    return [r[0] for r in con.execute("SELECT tag FROM subject_tags ORDER BY tag").fetchall()]

def step_states(con, out, only=None):
    tags = tag_list(con)
    case = ' '.join(f"WHEN '{t}' THEN {1 << i}" for i, t in enumerate(tags))
    states = [r[0] for r in con.execute("SELECT DISTINCT coalesce(recipient_state, 'XX') FROM grants_all ORDER BY 1").fetchall()]
    if only: states = [s for s in states if s in only]
    os.makedirs(SM, exist_ok=True)
    for st in states:
        t0 = time.time()
        cond = "recipient_state IS NULL" if st == 'XX' else f"recipient_state = '{st}'"
        con.execute(f"""CREATE OR REPLACE TEMP TABLE g AS
            SELECT funder_ein::INT AS funder_id, funder_type = 'private_foundation' AS is_pf, fiscal_year::INT AS fiscal_year, amount::DOUBLE AS amount,
                   coalesce(list_sum(list_transform(list_distinct(tags), lambda t: CASE t {case} ELSE 0 END)), 0)::INT AS tag_mask,
                   recipient_county, coalesce(recipient_key, '') AS recipient_key, upper(trim(coalesce(recipient_city, ''))) AS city_u,
                   recipient_name, recipient_city, recipient_ein_best, purpose, funder_ein
            FROM grants_all WHERE {cond} AND amount > 0 AND NOT amount_flag""")
        con.execute("""CREATE OR REPLACE TEMP TABLE r AS
            SELECT (row_number() OVER (ORDER BY recipient_key, city_u) - 1)::INT AS rid, (dense_rank() OVER (ORDER BY recipient_key) - 1)::INT AS rk, *
            FROM (SELECT recipient_key, city_u, coalesce(mode(recipient_name), '') AS name, coalesce(mode(recipient_city), '') AS city,
                         coalesce(mode(recipient_ein_best)::INT, -1) AS ein, count(*)::INT AS n_grants, count(DISTINCT funder_id)::INT AS n_funders, sum(amount) AS dollars
                  FROM g GROUP BY 1, 2)""")
        con.execute("""CREATE OR REPLACE TEMP TABLE p AS
            SELECT (row_number() OVER (ORDER BY n DESC, purpose) - 1)::INT AS pid, purpose, n::INT AS n
            FROM (SELECT purpose, count(*) AS n FROM g WHERE purpose IS NOT NULL GROUP BY 1)""")
        counties = [x[0] for x in con.execute("SELECT DISTINCT recipient_county FROM g WHERE recipient_county IS NOT NULL ORDER BY 1").fetchall()]
        con.execute("CREATE OR REPLACE TEMP TABLE c AS SELECT unnest(?::VARCHAR[]) AS county, unnest(range(?))::INT AS cid", [counties, len(counties)])
        con.execute("""CREATE OR REPLACE TEMP TABLE x AS
            SELECT (row_number() OVER (ORDER BY r.rid, g.fiscal_year DESC, g.amount DESC, g.funder_id) - 1)::INT AS row, g.funder_id, g.is_pf, g.fiscal_year, g.amount, g.tag_mask,
                   coalesce(c.cid, -1)::INT AS county_id, r.rid, r.rk, coalesce(p.pid, -1)::INT AS purpose_id, coalesce(g.recipient_ein_best::INT, -1) AS rein
            FROM g JOIN r ON r.recipient_key = g.recipient_key AND r.city_u = g.city_u
                   LEFT JOIN p ON p.purpose = g.purpose LEFT JOIN c ON c.county = g.recipient_county""")
        gt = required(arrow(con, """SELECT funder_id, is_pf, fiscal_year::SMALLINT AS fiscal_year, amount, tag_mask, county_id::SMALLINT AS county_id,
                                           rid, rk, purpose_id, rein FROM x ORDER BY row"""))
        gfiles = write_split(gt, os.path.join(out, 'grants', st), GRANT_GROUP)
        kt = required(arrow(con, """SELECT r.recipient_key, r.rk, s.row_start, s.n_rows FROM r
                                     JOIN (SELECT rid, min(row)::INT AS row_start, count(*)::INT AS n_rows FROM x GROUP BY 1) s USING (rid) ORDER BY r.rid"""))
        kfiles = write_split(kt, os.path.join(out, 'recipient_keys', st), 65536)
        it = required(arrow(con, "SELECT name, city, ein, n_grants, n_funders, dollars FROM r ORDER BY rid"))
        ifiles = write_split(it, os.path.join(out, 'recipient_info', st), INFO_GROUP)
        pt = required(arrow(con, "SELECT purpose FROM p ORDER BY pid"))
        pfiles = write_split(pt, os.path.join(out, 'purposes', st), INFO_GROUP)
        # EIN -> row range, one row per (ein, recipient row)
        et = arrow(con, f"""SELECT x.rein AS ein, '{st}' AS state, x.rid, min(x.row)::INT AS row_start, (max(x.row) - min(x.row) + 1)::INT AS n_rows, count(*)::INT AS n_grants
                            FROM x WHERE x.rein >= 0 GROUP BY 1, 3""")
        os.makedirs(SM, exist_ok=True)
        pq.write_table(et, os.path.join(SM, f'eins_{st}.parquet'), compression='zstd')
        null_rk = con.execute("SELECT min(rk) FROM r WHERE recipient_key = ''").fetchone()[0]
        tot = con.execute("SELECT count(*), sum(amount), count(DISTINCT funder_id) FROM g").fetchone()
        meta = {'state': st, 'grants': tot[0], 'dollars': tot[1], 'funders': tot[2], 'recipients': kt.num_rows, 'purposes': pt.num_rows,
                'null_key_rk': -1 if null_rk is None else int(null_rk), 'counties': counties,
                'files': {'grants': gfiles, 'recipient_keys': kfiles, 'recipient_info': ifiles, 'purposes': pfiles}}
        with open(os.path.join(SM, f'{st}.json'), 'w') as f: json.dump(meta, f)
        sz = sum(x['bytes'] for k in meta['files'] for x in meta['files'][k])
        log(f"{st}: {tot[0]:,} grants, {kt.num_rows:,} recipients, {pt.num_rows:,} purposes, {sz / 1e6:.1f} MB "
            f"(grants {sum(x['bytes'] for x in gfiles) / 1e6:.1f}, keys {sum(x['bytes'] for x in kfiles) / 1e6:.1f}, info {sum(x['bytes'] for x in ifiles) / 1e6:.1f}, "
            f"purposes {sum(x['bytes'] for x in pfiles) / 1e6:.1f}) in {time.time() - t0:.0f}s")

# ----------------------------------------------------------------------------- directory
def step_directory(con, out):
    t = arrow(con, """
        SELECT ein::INT AS ein, true AS is_pf, coalesce(name, '') AS name, coalesce(city, '') AS city, coalesce(state, '') AS state,
               coalesce(fmv_assets_eoy, 'NaN'::DOUBLE) AS assets, coalesce(fmv_assets_eoy, total_assets_eoy, bmf_asset_amt::DOUBLE, 'NaN'::DOUBLE) AS assets_any,
               coalesce(giving_latest, 'NaN'::DOUBLE) AS giving_latest, coalesce(fiscal_year_latest, 0)::SMALLINT AS fiscal_year_latest,
               coalesce(efile_missing, false) AS efile_missing, CASE WHEN only_preselected IS NULL THEN -1 WHEN only_preselected THEN 1 ELSE 0 END::TINYINT AS only_preselected,
               CASE WHEN ever_listed_application_info IS NULL THEN -1 WHEN ever_listed_application_info THEN 1 ELSE 0 END::TINYINT AS ever_listed_application_info,
               coalesce(app_deadlines, '') AS app_deadlines, coalesce(website, '') AS website, coalesce(top_recipient_states, '') AS top_recipient_states
        FROM foundations
        UNION ALL
        SELECT ein::INT, false, coalesce(name, ''), coalesce(city, ''), coalesce(state, ''), coalesce(total_assets_eoy, 'NaN'::DOUBLE), coalesce(total_assets_eoy, 'NaN'::DOUBLE),
               coalesce(grants_listed_latest, 'NaN'::DOUBLE), coalesce(fiscal_year_latest, 0)::SMALLINT, false, -1::TINYINT, -1::TINYINT, '', coalesce(website, ''), coalesce(top_recipient_states, '')
        FROM pc_funders
        ORDER BY 1, 2 DESC""")
    size = write(required(t), os.path.join(out, 'funder_directory.parquet'), 1 << 20, dictionary=['state'])
    log(f'funder_directory: {t.num_rows:,} rows, {size / 1e6:.1f} MB')
    with open(os.path.join(SM, '_directory.json'), 'w') as f: json.dump({'file': 'funder_directory.parquet', 'rows': t.num_rows, 'bytes': size}, f)

# ----------------------------------------------------------------------------- profiles
def step_profiles(con, out, work):
    os.makedirs(work, exist_ok=True)
    f1, f2, f3 = (os.path.join(work, n) for n in ('top_recipients.parquet', 'latest_grants.parquet', 'top_tags.parquet'))
    if not os.path.exists(f1):
        log('profiles: top recipients')
        con.execute(f"""COPY (
            WITH a AS (SELECT funder_ein, recipient_key, any_value(recipient_name) AS recipient_name, mode(recipient_city) AS city, mode(recipient_state) AS state,
                              sum(amount) AS dollars, count(*) AS grants, min(fiscal_year) AS first_year, max(fiscal_year) AS last_year, mode(recipient_ein_best) AS ein
                       FROM grants_all WHERE amount > 0 AND NOT amount_flag GROUP BY 1, 2),
                 b AS (SELECT *, row_number() OVER (PARTITION BY funder_ein ORDER BY dollars DESC, grants DESC, recipient_key) AS rn FROM a)
            SELECT funder_ein, to_json(list({{'recipient_name': recipient_name, 'city': city, 'state': state, 'dollars': dollars, 'grants': grants,
                                             'first_year': first_year, 'last_year': last_year, 'ein': ein}} ORDER BY rn))::VARCHAR AS top_recipients
            FROM b WHERE rn <= {TOP_RECIPIENTS} GROUP BY 1) TO '{f1}.tmp' (FORMAT parquet, COMPRESSION zstd)""")
        os.replace(f1 + '.tmp', f1)
    if not os.path.exists(f2):
        log('profiles: latest grants')
        con.execute(f"""COPY (
            WITH b AS (SELECT funder_ein, fiscal_year, recipient_name, recipient_city, recipient_state, amount, purpose, tags, amount_flag,
                              row_number() OVER (PARTITION BY funder_ein ORDER BY fiscal_year DESC, amount DESC NULLS LAST, recipient_key, seq) AS rn FROM grants_all)
            SELECT funder_ein, to_json(list({{'fiscal_year': fiscal_year, 'recipient_name': recipient_name, 'recipient_city': recipient_city, 'recipient_state': recipient_state,
                                             'amount': amount, 'purpose': purpose, 'tags': tags, 'amount_flag': amount_flag}} ORDER BY rn))::VARCHAR AS latest_grants
            FROM b WHERE rn <= {LATEST_GRANTS} GROUP BY 1) TO '{f2}.tmp' (FORMAT parquet, COMPRESSION zstd)""")
        os.replace(f2 + '.tmp', f2)
    if not os.path.exists(f3):
        log('profiles: top tags')
        con.execute(f"""COPY (
            WITH g AS (SELECT funder_ein, amount, tags FROM grants_all WHERE amount > 0 AND NOT amount_flag),
                 tot AS (SELECT funder_ein, sum(amount) AS total FROM g GROUP BY 1),
                 t AS (SELECT funder_ein, unnest(tags) AS tag, amount FROM g),
                 a AS (SELECT funder_ein, tag, sum(amount) AS dollars, count(*) AS grants FROM t GROUP BY 1, 2),
                 b AS (SELECT a.*, a.dollars / tot.total AS share, row_number() OVER (PARTITION BY a.funder_ein ORDER BY a.dollars DESC, a.tag) AS rn FROM a JOIN tot USING (funder_ein))
            SELECT funder_ein, to_json(list({{'tag': tag, 'dollars': dollars, 'grants': grants, 'share': share}} ORDER BY rn))::VARCHAR AS top_tags
            FROM b WHERE rn <= {TOP_TAGS} GROUP BY 1) TO '{f3}.tmp' (FORMAT parquet, COMPRESSION zstd)""")
        os.replace(f3 + '.tmp', f3)
    log('profiles: assemble')
    con.execute(f"""CREATE OR REPLACE TEMP TABLE prof AS
        SELECT u.ein::INT AS ein, u.is_pf, u.profile, coalesce(a.top_recipients, '[]') AS top_recipients, coalesce(c.top_tags, '[]') AS top_tags, coalesce(b.latest_grants, '[]') AS latest_grants
        FROM (SELECT ein, true AS is_pf, to_json(f)::VARCHAR AS profile FROM foundations f
              UNION ALL SELECT ein, false, to_json(p)::VARCHAR FROM pc_funders p) u
        LEFT JOIN read_parquet('{f1}') a ON a.funder_ein = u.ein LEFT JOIN read_parquet('{f2}') b ON b.funder_ein = u.ein LEFT JOIN read_parquet('{f3}') c ON c.funder_ein = u.ein""")
    n = con.execute("SELECT count(*) FROM prof").fetchone()[0]
    # split into files by EIN range, about 16 MB each
    target = 18000
    files = []
    bounds = [r[0] for r in con.execute(f"SELECT ein FROM (SELECT ein, row_number() OVER (ORDER BY ein, is_pf DESC) AS rn FROM prof) WHERE rn % {target} = 1 ORDER BY 1").fetchall()]
    bounds = sorted(set(bounds))
    for i, lo in enumerate(bounds):
        hi = bounds[i + 1] if i + 1 < len(bounds) else 2 ** 31 - 1
        t = required(arrow(con, "SELECT ein, is_pf, profile, top_recipients, top_tags, latest_grants FROM prof WHERE ein >= ? AND ein < ? ORDER BY ein, is_pf DESC", [lo, hi]))
        path = os.path.join(out, 'funder_profiles', f'p_{i + 1:02d}.parquet')
        size = write(t, path, PROFILE_GROUP, stats=['ein'], level=12)
        files.append({'file': os.path.basename(path), 'rows': t.num_rows, 'bytes': size, 'ein_min': int(t['ein'][0].as_py()), 'ein_max': int(t['ein'][-1].as_py())})
        log(f"  {os.path.basename(path)}: {t.num_rows:,} rows, {size / 1e6:.1f} MB")
    with open(os.path.join(SM, '_profiles.json'), 'w') as f: json.dump({'rows': n, 'files': files}, f)

# ----------------------------------------------------------------------------- recipient EIN map
def step_eins(con, out):
    src = os.path.join(SM, 'eins_*.parquet')
    t = required(arrow(con, f"SELECT ein, state, rid, row_start, n_rows, n_grants FROM read_parquet('{src}') ORDER BY ein, n_grants DESC, state, rid"))
    size = write(t, os.path.join(out, 'recipient_eins.parquet'), EIN_GROUP, dictionary=['state'], stats=['ein'])
    log(f'recipient_eins: {t.num_rows:,} rows, {size / 1e6:.1f} MB')
    with open(os.path.join(SM, '_eins.json'), 'w') as f: json.dump({'file': 'recipient_eins.parquet', 'rows': t.num_rows, 'bytes': size}, f)

# ----------------------------------------------------------------------------- manifest
def step_manifest(con, out):
    states = {}
    for p in sorted(glob.glob(os.path.join(SM, '[A-Z][A-Z].json'))):
        m = json.load(open(p)); states[m.pop('state')] = m
    meta = dict(con.execute("SELECT key, value FROM meta").fetchall())
    tags = [{'tag': r[0], 'label': r[1], 'bit': i, 'grants': r[2]} for i, r in enumerate(con.execute("SELECT tag, label, n_grants FROM subject_tags ORDER BY tag").fetchall())]
    idx = {'format': 1, 'build_date': meta.get('build_date'), 'generated': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
           'row_counts': json.loads(meta.get('row_counts', '{}')), 'caveats': json.loads(meta.get('caveats', '[]')),
           'limits': {'top_recipients': TOP_RECIPIENTS, 'latest_grants': LATEST_GRANTS, 'top_tags': TOP_TAGS},
           'tags': tags, 'directory': json.load(open(os.path.join(SM, '_directory.json'))), 'profiles': json.load(open(os.path.join(SM, '_profiles.json'))),
           'recipient_eins': json.load(open(os.path.join(SM, '_eins.json'))), 'states': states}
    with open(os.path.join(out, 'index.json'), 'w') as f: json.dump(idx, f, separators=(',', ':'))
    log(f"index.json: {os.path.getsize(os.path.join(out, 'index.json')) / 1e3:.0f} KB, {len(states)} states")

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--db', default='db/fdb.duckdb'); ap.add_argument('--out', default='data/index'); ap.add_argument('--only'); ap.add_argument('--work', help='folder for working files, default ./index_work')
    ap.add_argument('steps', nargs='*')
    a = ap.parse_args()
    steps = a.steps or ['states', 'directory', 'profiles', 'eins', 'manifest']
    global SM
    work = a.work or 'index_work'
    SM = os.path.join(work, 'state_meta')
    os.makedirs(SM, exist_ok=True)
    con = connect(a.db)
    for s in steps:
        if s == 'states': step_states(con, a.out, a.only.split(',') if a.only else None)
        elif s == 'directory': step_directory(con, a.out)
        elif s == 'profiles': step_profiles(con, a.out, os.path.join(work, 'profiles'))
        elif s == 'eins': step_eins(con, a.out)
        elif s == 'manifest': step_manifest(con, a.out)
        else: raise SystemExit(f'unknown step {s}')

if __name__ == '__main__':
    main()
