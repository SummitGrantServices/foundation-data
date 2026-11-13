#!/usr/bin/env python3
"""
fdb.py : command-line queries over the foundation database (IRS e-filed 990-PF and 990 Schedule I data).

  python3 fdb.py foundation <EIN or name fragment> [--state ST] [--pick N]
  python3 fdb.py funders-of <recipient name fragment or EIN> [--state ST] [--years 2022-2025] [--limit N]
  python3 fdb.py search --state ST [--county NAME] [--tags a,b] [--all-tags] [--text "words"] [--min-grant N]
                        [--years 2022-2025] [--preselected yes|no|any] [--funder-state ST] [--funder-type pf|pc|any]
                        [--sort dollars|count] [--limit N] [--examples N]
  python3 fdb.py peers-funders --eins e1,e2,... [--min-peers N] [--years 2022-2025] [--limit N]
  python3 fdb.py sql "<select ...>"            (read-only)
  python3 fdb.py tags | info

Add --json to any command for machine-readable output.
Data source: db/fdb.duckdb next to this script (or --db PATH, or env FDB_DB). If no database file is found, the tool
reads the parquet exports instead (--data DIR, default ./export, then ./data): foundations*.parquet, pc_funders*.parquet,
pf_officers*.parquet, pf_app_info*.parquet, grants_all/*.parquet.
"""
import os, sys, re, json, argparse, unicodedata, glob, datetime
import duckdb

BASE = os.path.dirname(os.path.abspath(__file__))

# ----------------------------------------------------------------------------- helpers
def name_key(s):
    """Same normalization as the database macro name_key()."""
    if s is None: return None
    s = ''.join(ch for ch in unicodedata.normalize('NFKD', s) if not unicodedata.combining(ch)).upper().replace('&', ' AND ')
    s = re.sub(r"['’`.]", '', s); s = re.sub(r'[^A-Z0-9 ]', ' ', s); s = re.sub(r' +', ' ', s).strip()
    s = re.sub(r'^THE ', '', s)
    return s or None

def as_ein(s):
    d = re.sub(r'[^0-9]', '', s or '')
    return d if len(d) == 9 and re.fullmatch(r'[0-9\- ]+', s.strip()) else None

def money(v):
    if v is None: return '-'
    try: return '${:,.0f}'.format(v)
    except Exception: return str(v)

def pct(v):
    return '-' if v is None else '{:.0f}%'.format(100 * v)

def years_arg(s):
    if not s: return None
    m = re.fullmatch(r'\s*(\d{4})\s*(?:-\s*(\d{4}))?\s*', s)
    if not m: raise SystemExit('--years must look like 2022-2025 or 2024')
    a = int(m.group(1)); b = int(m.group(2) or a)
    return (min(a, b), max(a, b))

def jdefault(o):
    if isinstance(o, (datetime.date, datetime.datetime)): return o.isoformat()
    try: return float(o)
    except Exception: return str(o)

class DB:
    def __init__(self, args):
        path = args.db or os.environ.get('FDB_DB') or os.path.join(BASE, 'db', 'fdb.duckdb')
        self.mode = 'duckdb'
        if os.path.isfile(path):
            self.con = duckdb.connect(path, read_only=True)
        else:
            d = args.data or next((x for x in (os.path.join(BASE, 'export'), os.path.join(BASE, 'data')) if os.path.isdir(x)), os.path.join(BASE, 'data'))
            if not glob.glob(os.path.join(d, 'foundations*.parquet')):
                raise SystemExit(f'No database at {path} and no parquet exports in {d}. Use --db or --data.')
            self.mode = 'parquet'; self.con = duckdb.connect()
            for t, pat in (('foundations', 'foundations*.parquet'), ('pc_funders', 'pc_funders*.parquet'), ('pf_officers', 'pf_officers*.parquet'),
                           ('pf_app_info', 'pf_app_info*.parquet'), ('grants_all', 'grants_all/*.parquet'), ('subject_tags', 'subject_tags*.parquet'),
                           ('meta', 'meta*.parquet')):
                if glob.glob(os.path.join(d, pat)):
                    self.con.execute(f"CREATE VIEW {t} AS SELECT * FROM read_parquet('{os.path.join(d, pat)}', union_by_name = true)")
        self.con.execute("PRAGMA threads=2"); self.con.execute("PRAGMA memory_limit='3GB'")
    def q(self, sql, params=None):
        cur = self.con.execute(sql, params or [])
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]

def out(args, obj, text_fn):
    if args.json: print(json.dumps(obj, default=jdefault, ensure_ascii=False, indent=None if args.compact else 1))
    else: text_fn(obj)

def year_clause(yrs, col='fiscal_year'):
    return f" AND {col} BETWEEN {yrs[0]} AND {yrs[1]}" if yrs else ''

# ----------------------------------------------------------------------------- foundation
def cmd_foundation(db, a):
    term = ' '.join(a.term); ein = as_ein(term); cands = []
    if ein:
        f = db.q("SELECT * FROM foundations WHERE ein = ?", [ein]); p = db.q("SELECT * FROM pc_funders WHERE ein = ?", [ein])
    else:
        like = '%' + term.strip().replace(' ', '%') + '%'; st = " AND state = ?" if a.state else ''; ps = [like] + ([a.state.upper()] if a.state else [])
        cands = db.q(f"""SELECT * FROM (
            SELECT 'private_foundation' AS funder_type, ein, name, city, state, coalesce(fmv_assets_eoy, total_assets_eoy, bmf_asset_amt) AS assets,
                   giving_latest AS giving, fiscal_year_latest, efile_missing FROM foundations WHERE name ILIKE ?{st}
            UNION ALL
            SELECT 'public_charity', ein, name, city, state, total_assets_eoy, grants_listed_latest, fiscal_year_latest, false FROM pc_funders WHERE name ILIKE ?{st})
            ORDER BY assets DESC NULLS LAST LIMIT 25""", ps + ps)
        if not cands:
            return out(a, {'query': term, 'matches': []}, lambda o: print(f'No foundation or grantmaking public charity matches "{term}".'))
        pick = cands[min(max(a.pick, 1), len(cands)) - 1]; ein = pick['ein']
        f = db.q("SELECT * FROM foundations WHERE ein = ?", [ein]) if pick['funder_type'] == 'private_foundation' else []
        p = db.q("SELECT * FROM pc_funders WHERE ein = ?", [ein]) if pick['funder_type'] == 'public_charity' else []
    if not f and not p:
        return out(a, {'query': term, 'matches': []}, lambda o: print(f'EIN {ein} is not in foundations or pc_funders.'))
    prof = (f or p)[0]; ftype = 'private_foundation' if f else 'public_charity'
    by_year = json.loads(prof['giving_by_year']) if prof.get('giving_by_year') else {}
    top = db.q("""SELECT any_value(recipient_name) AS recipient_name, mode(recipient_city) AS city, mode(recipient_state) AS state, sum(amount) AS dollars, count(*) AS grants,
                    min(fiscal_year) AS first_year, max(fiscal_year) AS last_year, mode(recipient_ein_best) AS ein
                  FROM grants_all WHERE funder_ein = ? AND amount > 0 AND NOT amount_flag GROUP BY recipient_key ORDER BY dollars DESC LIMIT ?""", [ein, a.top])
    tags = db.q("""WITH g AS (SELECT amount, tags FROM grants_all WHERE funder_ein = ? AND amount > 0 AND NOT amount_flag),
                   t AS (SELECT unnest(tags) AS tag, amount FROM g)
                   SELECT tag, sum(amount) AS dollars, count(*) AS grants, sum(amount) / (SELECT sum(amount) FROM g) AS share FROM t GROUP BY 1 ORDER BY dollars DESC LIMIT 8""", [ein])
    latest = db.q("""SELECT fiscal_year, recipient_name, recipient_city, recipient_state, amount, purpose, tags, amount_flag FROM grants_all
                     WHERE funder_ein = ? ORDER BY fiscal_year DESC, amount DESC NULLS LAST LIMIT ?""", [ein, a.grants])
    obj = {'funder_type': ftype, 'profile': prof, 'giving_by_year': by_year, 'top_recipients': top, 'top_tags': tags, 'latest_grants': latest,
           'other_matches': [c for c in cands if c['ein'] != ein]}
    def text(o):
        r = prof
        print(f"{r['name']}  (EIN {r['ein']}, {ftype.replace('_', ' ')})")
        print(f"  {r.get('address') or ''}, {r.get('city') or ''}, {r.get('state') or ''} {r.get('zip') or ''}   county: {r.get('county') or '-'}")
        print(f"  phone: {r.get('phone') or '-'}   website: {r.get('website') or '-'}   NTEE: {r.get('ntee_cd') or '-'}   in IRS master file: {r.get('in_bmf')}")
        if ftype == 'private_foundation':
            if r.get('efile_missing'):
                print(f"  NO E-FILED RETURN IN THIS DATABASE. Master file only: assets {money(r.get('bmf_asset_amt'))}, income {money(r.get('bmf_income_amt'))}, tax period {r.get('bmf_tax_period')}")
                return
            print(f"  latest return: FY{r['fiscal_year_latest']} (period end {r['period_end_latest']})   signer: {r.get('signer') or '-'}")
            print(f"  assets (FMV): {money(r['fmv_assets_eoy'])}   book assets: {money(r['total_assets_eoy'])}   revenue: {money(r['total_revenue'])}   contributions received: {money(r['contributions_received'])}")
            print(f"  grants paid (latest): {money(r['total_grants_paid'])}   returns on file: {r['n_returns']} (FY{r['first_fiscal_year']} to FY{r['last_fiscal_year']})")
            print(f"  only gives to preselected organizations (latest return): {'YES' if r['only_preselected'] else 'no'}   operating foundation: {'yes' if r['operating_foundation'] else 'no'}"
                  + ('   FINAL RETURN FILED' if r.get('final_return_latest') else '') + ('   AMOUNTS FLAGGED AS SUSPECT' if r.get('flag_amounts_suspect') else ''))
            print(f"  grant size: median {money(r['median_grant'])}, average {money(r['avg_grant'])}, largest {money(r['max_grant'])}")
            print(f"  where the money goes: {r.get('top_recipient_states') or '-'}   share staying in {r.get('state')}: {pct(r.get('home_state_share'))}")
        else:
            print(f"  latest return: FY{r['fiscal_year_latest']}   principal officer: {r.get('principal_officer') or '-'}")
            print(f"  total assets: {money(r['total_assets_eoy'])}   revenue: {money(r['total_revenue'])}   grants paid (Part IX): {money(r['total_grants_paid'])}   Schedule I grants listed: {money(r['grants_listed_latest'])} ({r['n_grants_latest']} grants)")
            print(f"  grant size: median {money(r['median_grant'])}, average {money(r['avg_grant'])}, largest {money(r['max_grant'])}")
            print(f"  where the money goes: {r.get('top_recipient_states') or '-'}   share staying in {r.get('state')}: {pct(r.get('home_state_share'))}")
            if r.get('mission'): print(f"  mission: {r['mission'][:400]}")
        print('\nGIVING BY FISCAL YEAR')
        for y in sorted(by_year, reverse=True):
            d = by_year[y]
            if ftype == 'private_foundation': print(f"  FY{y}: {money(d.get('giving')):>16}   grants listed: {d.get('grants')}")
            else: print(f"  FY{y}: {money(d.get('grants_listed')):>16}   grants listed: {d.get('grants')}   (Part IX total {money(d.get('grants_paid_total'))})")
        if ftype == 'private_foundation':
            print('\nAPPLICATION INFORMATION' + (f" (from FY{r['app_fiscal_year']} return)" if r.get('app_fiscal_year') else ''))
            if r.get('ever_listed_application_info'):
                for k, lab in (('app_contact_name', 'contact'), ('app_address', 'address'), ('app_phone', 'phone'), ('app_email', 'email'),
                               ('app_form_and_materials', 'form and materials'), ('app_deadlines', 'deadlines'), ('app_restrictions', 'restrictions')):
                    if r.get(k): print(f"  {lab}: {str(r[k])[:600]}")
            else: print('  none listed on any e-filed return')
            if r['only_preselected']: print('  NOTE: latest return checks the box "only makes contributions to preselected charitable organizations and does not accept unsolicited requests".')
            print('\nOFFICERS AND TRUSTEES (latest return)'); print('  ' + (r.get('officers') or '-')[:1500])
        if tags:
            print('\nSUBJECT MIX (keyword tags, share of listed dollars; a grant can carry several tags)')
            print('  ' + '; '.join(f"{t['tag']} {pct(t['share'])}" for t in tags))
        print(f'\nTOP RECIPIENTS (all years on file)')
        for t in top:
            print(f"  {money(t['dollars']):>15}  {t['grants']:>4} grants  FY{t['first_year']}-{t['last_year']}  {t['recipient_name']} ({t['city'] or '-'}, {t['state'] or '-'})")
        print(f'\nLATEST GRANTS')
        for g in latest:
            print(f"  FY{g['fiscal_year']} {money(g['amount']):>13}  {g['recipient_name']} ({g['recipient_city'] or '-'}, {g['recipient_state'] or '-'})  | {(g['purpose'] or '')[:90]}" + ('  [FLAGGED]' if g['amount_flag'] else ''))
        if o['other_matches']:
            print(f'\nOTHER NAME MATCHES (use the EIN, or --pick N)')
            for i, c in enumerate(cands, 1):
                if c['ein'] != ein:
                    print(f"  {i:>2}. {c['ein']}  {c['name']} ({c['city']}, {c['state']})  assets {money(c['assets'])}  {c['funder_type']}" + ('  [no e-filed return]' if c['efile_missing'] else ''))
    out(a, obj, text)

# ----------------------------------------------------------------------------- funders-of
def cmd_funders_of(db, a):
    term = ' '.join(a.term); ein = as_ein(term); yrs = years_arg(a.years)
    if ein: cond, ps = "recipient_ein_best = ?", [ein]
    else:
        k = name_key(term)
        if not k: raise SystemExit('Give a recipient name fragment or a 9-digit EIN.')
        cond, ps = "contains(recipient_key, ?)", [k]
    if a.state: cond += " AND recipient_state = ?"; ps.append(a.state.upper())
    cond += year_clause(yrs)
    recips = db.q(f"""SELECT any_value(recipient_name) AS recipient_name, mode(recipient_city) AS city, recipient_state AS state, mode(recipient_ein_best) AS ein,
                        count(*) AS grants, sum(amount) AS dollars, count(DISTINCT funder_ein) AS funders
                      FROM grants_all WHERE {cond} AND amount > 0 AND NOT amount_flag GROUP BY recipient_key, recipient_state ORDER BY dollars DESC LIMIT 15""", ps)
    funders = db.q(f"""WITH g AS (SELECT * FROM grants_all WHERE {cond} AND amount > 0 AND NOT amount_flag),
        y AS (SELECT funder_ein, fiscal_year, sum(amount) AS d, count(*) AS n FROM g GROUP BY 1, 2),
        yy AS (SELECT funder_ein, string_agg('FY' || fiscal_year || ' ' || printf('$%,d', d::BIGINT), '; ' ORDER BY fiscal_year DESC) AS by_year FROM y GROUP BY 1)
        SELECT g.funder_ein, any_value(g.funder_name) AS funder_name, any_value(g.funder_type) AS funder_type, mode(g.funder_city) AS funder_city, mode(g.funder_state) AS funder_state,
               sum(g.amount) AS dollars, count(*) AS grants, min(g.fiscal_year) AS first_year, max(g.fiscal_year) AS last_year,
               any_value(yy.by_year) AS by_year, mode(g.purpose) AS typical_purpose, count(DISTINCT g.recipient_key) AS recipient_names_matched
        FROM g JOIN yy USING (funder_ein) GROUP BY g.funder_ein ORDER BY dollars DESC LIMIT ?""", ps + [a.limit])
    tot = db.q(f"SELECT count(*) AS grants, sum(amount) AS dollars, count(DISTINCT funder_ein) AS funders FROM grants_all WHERE {cond} AND amount > 0 AND NOT amount_flag", ps)[0]
    obj = {'query': term, 'state': a.state, 'totals': tot, 'recipients_matched': recips, 'funders': funders}
    def text(o):
        print(f"Funders of \"{term}\"" + (f" in {a.state.upper()}" if a.state else '') + f": {tot['funders']} funders, {tot['grants']} grants, {money(tot['dollars'])}")
        print('\nRECIPIENT RECORDS MATCHED (check these are the organization you mean; add --state or use the EIN to narrow)')
        for r in recips:
            print(f"  {money(r['dollars']):>15}  {r['grants']:>5} grants  {r['funders']:>4} funders  {r['recipient_name']} ({r['city'] or '-'}, {r['state'] or '-'})  EIN {r['ein'] or '-'}")
        print('\nFUNDERS (ranked by dollars)')
        for f in funders:
            print(f"  {money(f['dollars']):>15}  {f['grants']:>4} grants  {f['funder_name']} ({f['funder_city'] or '-'}, {f['funder_state'] or '-'})  EIN {f['funder_ein']}  [{ 'PF' if f['funder_type'] == 'private_foundation' else 'PC'}]")
            print(f"                   {f['by_year']}" + (f"  | {f['typical_purpose'][:70]}" if f['typical_purpose'] else ''))
    out(a, obj, text)

# ----------------------------------------------------------------------------- search
def valid_tags(db):
    try: return [r['tag'] for r in db.q("SELECT tag FROM subject_tags ORDER BY 1")]
    except Exception: return []

def cmd_search(db, a):
    conds = ["amount > 0", "NOT amount_flag"]; ps = []; yrs = years_arg(a.years)
    if a.state: conds.append("recipient_state = ?"); ps.append(a.state.upper())
    if a.county:
        cn = re.sub(r'\s+(county|parish|borough)$', '', a.county.strip(), flags=re.I)
        conds.append("recipient_county ILIKE ?"); ps.append(cn + '%')
    if a.tags:
        tags = [t.strip() for t in a.tags.split(',') if t.strip()]; vt = valid_tags(db)
        bad = [t for t in tags if vt and t not in vt]
        if bad: raise SystemExit(f"Unknown tag(s): {', '.join(bad)}. Valid tags: {', '.join(vt)}")
        lst = '[' + ','.join("'" + t.replace("'", "''") + "'" for t in tags) + ']'
        conds.append(f"list_has_all(tags, {lst})" if a.all_tags else f"list_has_any(tags, {lst})")
    if a.text:
        for w in a.text.upper().split():
            conds.append("contains(upper(coalesce(purpose, '') || ' ' || coalesce(recipient_name, '')), ?)"); ps.append(w)
    if a.min_grant: conds.append("amount >= ?"); ps.append(float(a.min_grant))
    if yrs: conds.append(f"fiscal_year BETWEEN {yrs[0]} AND {yrs[1]}")
    if a.funder_state: conds.append("funder_state = ?"); ps.append(a.funder_state.upper())
    if a.funder_type == 'pf': conds.append("funder_type = 'private_foundation'")
    if a.funder_type == 'pc': conds.append("funder_type = 'public_charity'")
    pre = {'yes': "AND fo.only_preselected", 'no': "AND (fo.only_preselected IS NOT TRUE)", 'any': ''}[a.preselected]
    order = 'x.grants DESC, x.dollars DESC' if a.sort == 'count' else 'x.dollars DESC, x.grants DESC'
    where = ' AND '.join(conds)
    rows = db.q(f"""WITH g AS (SELECT * FROM grants_all WHERE {where}),
        x AS (SELECT funder_ein, any_value(funder_type) AS funder_type, sum(amount) AS dollars, count(*) AS grants, count(DISTINCT recipient_key) AS recipients,
                min(fiscal_year) AS first_year, max(fiscal_year) AS last_year, median(amount) AS median_matching_grant,
                max_by({{'fy': fiscal_year, 'recipient': recipient_name, 'city': recipient_city, 'amount': amount, 'purpose': purpose}}, amount, {int(a.examples)}) AS examples
              FROM g GROUP BY 1)
        SELECT x.funder_ein, coalesce(fo.name, pc.name) AS funder_name, x.funder_type, coalesce(fo.city, pc.city) AS funder_city, coalesce(fo.state, pc.state) AS funder_state,
               x.dollars, x.grants, x.recipients, x.first_year, x.last_year, x.median_matching_grant,
               coalesce(fo.fmv_assets_eoy, pc.total_assets_eoy) AS assets, coalesce(fo.giving_latest, pc.grants_listed_latest) AS total_giving_latest,
               fo.only_preselected, fo.ever_listed_application_info, fo.app_deadlines, coalesce(fo.website, pc.website) AS website,
               coalesce(fo.top_recipient_states, pc.top_recipient_states) AS top_recipient_states, x.examples
        FROM x LEFT JOIN foundations fo ON x.funder_type = 'private_foundation' AND fo.ein = x.funder_ein
               LEFT JOIN pc_funders pc ON x.funder_type = 'public_charity' AND pc.ein = x.funder_ein
        WHERE true {pre} ORDER BY {order} LIMIT ?""", ps + [a.limit])
    tot = db.q(f"SELECT count(*) AS grants, sum(amount) AS dollars, count(DISTINCT funder_ein) AS funders FROM grants_all WHERE {where}", ps)[0]
    obj = {'filters': {k: v for k, v in vars(a).items() if k in ('state', 'county', 'tags', 'all_tags', 'text', 'min_grant', 'years', 'preselected', 'funder_state', 'funder_type') and v},
           'totals_before_preselected_filter': tot, 'funders': rows}
    def text(o):
        print(f"Search {o['filters']}: {tot['funders']} funders, {tot['grants']} matching grants, {money(tot['dollars'])} (before any preselected filter)")
        for i, r in enumerate(rows, 1):
            flag = 'PC' if r['funder_type'] == 'public_charity' else ('PF, preselected only' if r['only_preselected'] else 'PF, accepts requests' + (' (application info listed)' if r['ever_listed_application_info'] else ''))
            print(f"\n{i:>3}. {r['funder_name']} ({r['funder_city'] or '-'}, {r['funder_state'] or '-'})  EIN {r['funder_ein']}  [{flag}]")
            print(f"     matching: {money(r['dollars'])} in {r['grants']} grants to {r['recipients']} recipients, FY{r['first_year']}-{r['last_year']}, median {money(r['median_matching_grant'])}"
                  f"   | assets {money(r['assets'])}, latest total giving {money(r['total_giving_latest'])}")
            extra = '     ' + '   '.join(x for x in (('web: ' + r['website']) if r['website'] else '', ('gives in: ' + r['top_recipient_states']) if r['top_recipient_states'] else '',
                                                    ('deadlines: ' + r['app_deadlines'][:80]) if r['app_deadlines'] else '') if x)
            if extra.strip(): print(extra)
            for e in (r['examples'] or []):
                print(f"       FY{e['fy']} {money(e['amount']):>12}  {e['recipient']} ({e['city'] or '-'})  | {(e['purpose'] or '')[:80]}")
    out(a, obj, text)

# ----------------------------------------------------------------------------- peers-funders
def cmd_peers(db, a):
    eins = [as_ein(e) for e in re.split(r'[,\s]+', a.eins.strip()) if e]
    if not eins or None in eins: raise SystemExit('--eins needs comma-separated 9-digit EINs')
    yrs = years_arg(a.years); lst = ','.join("'" + e + "'" for e in eins)
    peers = db.q(f"""SELECT recipient_ein_best AS ein, mode(recipient_name) AS name, mode(recipient_state) AS state, count(*) AS grants, sum(amount) AS dollars,
                       count(DISTINCT funder_ein) AS funders FROM grants_all WHERE recipient_ein_best IN ({lst}) AND amount > 0 AND NOT amount_flag{year_clause(yrs)} GROUP BY 1 ORDER BY dollars DESC""")
    rows = db.q(f"""WITH g AS (SELECT * FROM grants_all WHERE recipient_ein_best IN ({lst}) AND amount > 0 AND NOT amount_flag{year_clause(yrs)}),
        p AS (SELECT funder_ein, recipient_ein_best AS pe, mode(recipient_name) AS pn, sum(amount) AS d, count(*) AS n, max(fiscal_year) AS ly FROM g GROUP BY 1, 2),
        x AS (SELECT funder_ein, count(*) AS peers_funded, sum(d) AS dollars, sum(n) AS grants, max(ly) AS last_year,
                string_agg(pn || ' ' || printf('$%,d', d::BIGINT) || ' (' || n || ', last FY' || ly || ')', '; ' ORDER BY d DESC) AS peers FROM p GROUP BY 1)
        SELECT x.funder_ein, coalesce(fo.name, pc.name) AS funder_name, CASE WHEN fo.ein IS NOT NULL THEN 'private_foundation' ELSE 'public_charity' END AS funder_type,
               coalesce(fo.city, pc.city) AS funder_city, coalesce(fo.state, pc.state) AS funder_state, x.peers_funded, x.dollars, x.grants, x.last_year,
               coalesce(fo.fmv_assets_eoy, pc.total_assets_eoy) AS assets, fo.only_preselected, fo.ever_listed_application_info, coalesce(fo.website, pc.website) AS website, x.peers
        FROM x LEFT JOIN foundations fo ON fo.ein = x.funder_ein AND NOT fo.efile_missing LEFT JOIN pc_funders pc ON pc.ein = x.funder_ein AND fo.ein IS NULL
        WHERE x.peers_funded >= ? ORDER BY x.peers_funded DESC, x.dollars DESC LIMIT ?""", [a.min_peers, a.limit])
    missing = [e for e in eins if e not in {p['ein'] for p in peers}]
    obj = {'peers': peers, 'peers_with_no_grants_found': missing, 'funders': rows}
    def text(o):
        print('PEER ORGANIZATIONS FOUND AS RECIPIENTS')
        for p in peers: print(f"  {p['ein']}  {p['name']} ({p['state'] or '-'}): {p['grants']} grants from {p['funders']} funders, {money(p['dollars'])}")
        if missing: print('  no grants found for: ' + ', '.join(missing) + '  (990-PF grants only link to an EIN when the name match was unique)')
        print(f'\nFUNDERS SUPPORTING AT LEAST {a.min_peers} OF THESE PEERS')
        for r in rows:
            flag = 'PC' if r['funder_type'] == 'public_charity' else ('PF, preselected only' if r['only_preselected'] else 'PF, accepts requests')
            print(f"  {r['peers_funded']} peers  {money(r['dollars']):>14}  {r['funder_name']} ({r['funder_city'] or '-'}, {r['funder_state'] or '-'})  EIN {r['funder_ein']}  [{flag}]  assets {money(r['assets'])}")
            print(f"           {r['peers'][:400]}")
    out(a, obj, text)

# ----------------------------------------------------------------------------- sql, tags, info
def cmd_sql(db, a):
    sql = a.query.strip().rstrip(';')
    if ';' in sql or not re.match(r'(?is)^\s*(select|with|describe|show|summarize|explain|from)\b', sql):
        raise SystemExit('Only a single read-only statement is allowed (SELECT, WITH, DESCRIBE, SHOW, SUMMARIZE, EXPLAIN).')
    if db.mode == 'duckdb':
        db.con.execute("SET enable_external_access=false")
    cur = db.con.execute(sql); cols = [d[0] for d in cur.description]; rows = cur.fetchmany(a.limit)
    obj = [dict(zip(cols, r)) for r in rows]
    def text(o):
        print('\t'.join(cols))
        for r in rows: print('\t'.join('' if v is None else str(v) for v in r))
        if len(rows) == a.limit: print(f'... output capped at {a.limit} rows (raise with --limit)')
    out(a, obj, text)

def cmd_tags(db, a):
    rows = db.q("SELECT tag, label, n_grants, dollars FROM subject_tags ORDER BY n_grants DESC")
    out(a, rows, lambda o: [print(f"  {r['tag']:<24} {r['label']:<42} {r['n_grants']:>10,} grants  {money(r['dollars'])}") for r in rows])

def cmd_info(db, a):
    m = {r['key']: r['value'] for r in db.q("SELECT key, value FROM meta")}
    obj = {'mode': db.mode, 'build_date': m.get('build_date'), 'row_counts': json.loads(m.get('row_counts', '{}')), 'caveats': json.loads(m.get('caveats', '[]')),
           'bmf_download_date': m.get('stat_bmf_download_date'), 'last_six_fiscal_years': m.get('stat_last_six_fiscal_years')}
    def text(o):
        print(f"mode: {db.mode}   build date: {obj['build_date']}   BMF downloaded: {obj['bmf_download_date']}")
        for k, v in obj['row_counts'].items(): print(f"  {k:<22} {v:>12,}")
        print('Caveats:'); [print('  - ' + c) for c in obj['caveats']]
    out(a, obj, text)

def main():
    try:   # let "fdb.py ... | head" end quietly
        import signal; signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    except Exception: pass
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument('--json', action='store_true', help='JSON output'); common.add_argument('--compact', action='store_true', help='single-line JSON')
    common.add_argument('--db', help='path to fdb.duckdb'); common.add_argument('--data', help='folder of parquet exports (used when no database file exists)')
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest='cmd', required=True)
    p = sub.add_parser('foundation', parents=[common]); p.add_argument('term', nargs='+'); p.add_argument('--state'); p.add_argument('--pick', type=int, default=1)
    p.add_argument('--top', type=int, default=15); p.add_argument('--grants', type=int, default=25); p.set_defaults(fn=cmd_foundation)
    p = sub.add_parser('funders-of', parents=[common]); p.add_argument('term', nargs='+'); p.add_argument('--state'); p.add_argument('--years'); p.add_argument('--limit', type=int, default=40)
    p.set_defaults(fn=cmd_funders_of)
    p = sub.add_parser('search', parents=[common]); p.add_argument('--state'); p.add_argument('--county'); p.add_argument('--tags'); p.add_argument('--all-tags', action='store_true')
    p.add_argument('--text'); p.add_argument('--min-grant', type=float); p.add_argument('--years'); p.add_argument('--preselected', choices=['yes', 'no', 'any'], default='any')
    p.add_argument('--funder-state'); p.add_argument('--funder-type', choices=['pf', 'pc', 'any'], default='any'); p.add_argument('--sort', choices=['dollars', 'count'], default='dollars')
    p.add_argument('--limit', type=int, default=25); p.add_argument('--examples', type=int, default=3); p.set_defaults(fn=cmd_search)
    p = sub.add_parser('peers-funders', parents=[common]); p.add_argument('--eins', required=True); p.add_argument('--min-peers', type=int, default=2); p.add_argument('--years')
    p.add_argument('--limit', type=int, default=40); p.set_defaults(fn=cmd_peers)
    p = sub.add_parser('sql', parents=[common]); p.add_argument('query'); p.add_argument('--limit', type=int, default=200); p.set_defaults(fn=cmd_sql)
    p = sub.add_parser('tags', parents=[common]); p.set_defaults(fn=cmd_tags)
    p = sub.add_parser('info', parents=[common]); p.set_defaults(fn=cmd_info)
    a = ap.parse_args()
    if a.cmd == 'search' and not (a.state or a.county or a.tags or a.text or a.funder_state):
        raise SystemExit('search needs at least --state (or --tags, --text, --funder-state).')
    a.fn(DB(a), a)

if __name__ == '__main__':
    main()
