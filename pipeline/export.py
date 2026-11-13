#!/usr/bin/env python3
"""
Stage 4 exports: zstd parquet files (none over 45 MB) and per-state CSV profiles, written to export/.
Usage: python3 export.py            (everything)
       python3 export.py grants     (only grants_all partitions)   | profiles | csv
Environment: FDB_DB (default db/fdb.duckdb), FDB_EXPORT (default export).
"""
import os, sys, json, math, time, datetime, shutil
import duckdb

BASE = os.path.dirname(os.path.abspath(__file__))
DB = os.environ.get('FDB_DB', os.path.join(BASE, 'db', 'fdb.duckdb'))
OUT = os.environ.get('FDB_EXPORT', os.path.join(BASE, 'export'))
TMP = os.path.join(BASE, 'duck_tmp')
MAXB = 45 * 1000 * 1000          # hard ceiling per file
TARGET = 36 * 1000 * 1000        # aim when a table has to be split
LEVEL = 12
T0 = time.time()

def log(*a): print(f'[{time.time() - T0:6.0f}s]', *a, flush=True)

def connect():
    c = duckdb.connect(DB, read_only=True)
    c.execute("PRAGMA memory_limit='4GB'"); c.execute(f"PRAGMA temp_directory='{TMP}'"); c.execute("PRAGMA threads=2")
    return c

def copy(c, sql, path):
    c.execute(f"COPY ({sql}) TO '{path}' (FORMAT parquet, COMPRESSION zstd, COMPRESSION_LEVEL {LEVEL}, ROW_GROUP_SIZE 100000)")
    return os.path.getsize(path)

def write_split(c, sql, order, folder, stem, files):
    """Write sql (sorted by order) to folder/stem.parquet, or stem_01.parquet ... when one file would exceed the ceiling."""
    os.makedirs(folder, exist_ok=True)
    c.execute(f"CREATE OR REPLACE TEMP TABLE _x AS SELECT row_number() OVER (ORDER BY {order}) AS _rn, * FROM ({sql})")
    n = c.execute("SELECT count(*) FROM _x").fetchone()[0]
    if n == 0: return
    single = os.path.join(folder, stem + '.parquet')
    size = copy(c, "SELECT * EXCLUDE (_rn) FROM _x ORDER BY _rn", single)
    if size <= MAXB:
        files.append({'file': os.path.relpath(single, OUT), 'bytes': size, 'rows': n}); return
    os.remove(single)
    k = math.ceil(size / TARGET)
    while True:
        step = math.ceil(n / k); out = []; ok = True
        for i in range(k):
            a, b = i * step + 1, min(n, (i + 1) * step)
            if a > n: break
            p = os.path.join(folder, f'{stem}_{i + 1:02d}.parquet')
            s = copy(c, f"SELECT * EXCLUDE (_rn) FROM _x WHERE _rn BETWEEN {a} AND {b} ORDER BY _rn", p)
            out.append({'file': os.path.relpath(p, OUT), 'bytes': s, 'rows': b - a + 1})
            if s > MAXB: ok = False
        if ok: files.extend(out); return
        for o in out: os.remove(os.path.join(OUT, o['file']))
        k += max(1, k // 4)

PROFILE_CSV_COLS = None

def export_profiles(c, files):
    write_split(c, "SELECT * FROM foundations", "state NULLS LAST, name, ein", OUT, 'foundations', files); log('foundations')
    write_split(c, "SELECT * FROM pc_funders", "state NULLS LAST, name, ein", OUT, 'pc_funders', files); log('pc_funders')
    write_split(c, "SELECT * FROM pf_officers", "funder_state NULLS LAST, funder_ein, period_end DESC, seq", OUT, 'pf_officers', files); log('pf_officers')
    write_split(c, "SELECT * FROM pf_app_info", "funder_state NULLS LAST, funder_ein, period_end DESC, seq", OUT, 'pf_app_info', files); log('pf_app_info')
    write_split(c, "SELECT * FROM subject_tags", "n_grants DESC", OUT, 'subject_tags', files)
    write_split(c, "SELECT * FROM meta", "key", OUT, 'meta', files)
    write_split(c, "SELECT * FROM qa_completeness", "index_year", OUT, 'qa_completeness', files)

def export_grants(c, files):
    folder = os.path.join(OUT, 'grants_all')
    states = [r[0] for r in c.execute("SELECT DISTINCT coalesce(recipient_state, 'XX') FROM grants_all ORDER BY 1").fetchall()]
    for st in states:
        cond = "recipient_state IS NULL" if st == 'XX' else f"recipient_state = '{st}'"
        write_split(c, f"SELECT * FROM grants_all WHERE {cond}", "fiscal_year DESC NULLS LAST, amount DESC NULLS LAST, funder_ein, object_id, seq", folder, st, files)
        log('grants', st)

def export_csv(c, files):
    folder = os.path.join(OUT, 'foundations_by_state'); os.makedirs(folder, exist_ok=True)
    desc = [(r[0], r[1]) for r in c.execute("DESCRIBE foundations").fetchall()]
    drop = {'giving_by_year', 'direct_charitable_activities', 'latest_object_id', 'bmf_subsection', 'bmf_income_amt', 'top_recipient_state', 'period_end_latest'}
    # whole dollars for spreadsheets (no trailing .0); shares keep their decimals
    sel = ', '.join((f'CAST(round({n}) AS BIGINT) AS {n}' if t == 'DOUBLE' and n != 'home_state_share' else (f'round({n}, 4) AS {n}' if n == 'home_state_share' else n))
                    for n, t in desc if n not in drop)
    US = "('AL','AK','AZ','AR','CA','CO','CT','DE','DC','FL','GA','HI','ID','IL','IN','IA','KS','KY','LA','ME','MD','MA','MI','MN','MS','MO','MT','NE','NV','NH','NJ','NM','NY','NC','ND','OH','OK','OR','PA','RI','SC','SD','TN','TX','UT','VT','VA','WA','WV','WI','WY','PR','VI','GU','AS','MP')"
    states = [r[0] for r in c.execute(f"SELECT DISTINCT CASE WHEN state IN {US} THEN state ELSE 'XX' END FROM foundations ORDER BY 1").fetchall()]
    for st in states:
        cond = f"(state IS NULL OR state NOT IN {US})" if st == 'XX' else f"state = '{st}'"
        p = os.path.join(folder, f'{st}.csv')
        c.execute(f"COPY (SELECT {sel} FROM foundations WHERE {cond} ORDER BY efile_missing, coalesce(fmv_assets_eoy, total_assets_eoy, bmf_asset_amt) DESC NULLS LAST, name) TO '{p}' (FORMAT csv, HEADER true)")
        n = c.execute(f"SELECT count(*) FROM foundations WHERE {cond}").fetchone()[0]
        files.append({'file': os.path.relpath(p, OUT), 'bytes': os.path.getsize(p), 'rows': n})
    log('csv by state', len(states))

def main():
    want = sys.argv[1:] or ['profiles', 'grants', 'csv']
    os.makedirs(OUT, exist_ok=True)
    mpath = os.path.join(OUT, 'MANIFEST.json')
    old = json.load(open(mpath)) if os.path.exists(mpath) else {'files': []}
    groups = {'profiles': lambda f: '/' not in f, 'grants': lambda f: f.startswith('grants_all/'), 'csv': lambda f: f.startswith('foundations_by_state/')}
    keep = []
    for f in old.get('files', []):      # remove only files this script wrote earlier for the groups being rebuilt
        if any(groups[g](f['file']) for g in want if g in groups):
            p = os.path.join(OUT, f['file'])
            if os.path.isfile(p) and f['file'] not in ('MANIFEST.json',): os.remove(p)
        else: keep.append(f)
    c = connect(); files = []
    if 'profiles' in want: export_profiles(c, files)
    if 'grants' in want: export_grants(c, files)
    if 'csv' in want: export_csv(c, files)
    tx = os.path.join(BASE, 'taxonomy.json')
    if 'profiles' in want and os.path.exists(tx):
        shutil.copyfile(tx, os.path.join(OUT, 'taxonomy.json'))
        files.append({'file': 'taxonomy.json', 'bytes': os.path.getsize(os.path.join(OUT, 'taxonomy.json')), 'rows': None})
    allf = sorted(keep + files, key=lambda f: f['file'])
    meta = dict(c.execute("SELECT key, value FROM meta WHERE key IN ('build_date', 'stat_bmf_download_date')").fetchall())
    man = {'generated': datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'), 'database_build_date': meta.get('build_date'),
           'compression': 'zstd', 'max_file_bytes': max((f['bytes'] for f in allf), default=0),
           'total_bytes': sum(f['bytes'] for f in allf), 'n_files': len(allf),
           'tables': {}, 'files': allf}
    for f in allf:
        if not f['file'].endswith('.parquet'): continue
        t = 'grants_all' if f['file'].startswith('grants_all/') else os.path.basename(f['file']).split('.')[0].rstrip('0123456789').rstrip('_')
        d = man['tables'].setdefault(t, {'files': 0, 'bytes': 0, 'rows': 0}); d['files'] += 1; d['bytes'] += f['bytes']; d['rows'] += f['rows'] or 0
    json.dump(man, open(mpath, 'w'), indent=1)
    over = [f for f in allf if f['bytes'] > MAXB]
    log('done', len(allf), 'files', round(man['total_bytes'] / 1e6, 1), 'MB', 'largest', round(man['max_file_bytes'] / 1e6, 1), 'MB', 'over limit:', over)

if __name__ == '__main__':
    main()
