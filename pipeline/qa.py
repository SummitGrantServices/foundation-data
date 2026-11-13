#!/usr/bin/env python3
"""
Stage 3: quality checks. Reads db/fdb.duckdb and writes QA.md (system record) with real numbers.
Usage: python3 qa.py [--no-propublica]
"""
import os, sys, json, subprocess, datetime, random
import duckdb

BASE = os.path.dirname(os.path.abspath(__file__))
DB = os.environ.get('FDB_DB', os.path.join(BASE, 'db', 'fdb.duckdb'))
OUT = os.environ.get('FDB_QA', os.path.join(BASE, 'QA.md'))
c = duckdb.connect(DB, read_only=True)
c.execute("PRAGMA memory_limit='4GB'"); c.execute(f"PRAGMA temp_directory='{os.path.join(BASE, 'duck_tmp')}'"); c.execute("PRAGMA threads=2")
L = []

def q(sql): return c.execute(sql).fetchall()
def one(sql): return c.execute(sql).fetchone()[0]
def n(v): return '' if v is None else (f'{v:,.0f}' if isinstance(v, (int, float)) and not isinstance(v, bool) else str(v))
def usd(v): return '' if v is None else f'${v:,.0f}'
def pc(a, b): return 'n/a' if not b else f'{100.0 * a / b:.1f}%'
def h(s): L.append(''); L.append(s); L.append('')
def p(s=''): L.append(s)
def table(head, rows, fmt=None):
    L.append('| ' + ' | '.join(head) + ' |'); L.append('|' + '|'.join(['---'] * len(head)) + '|')
    for r in rows:
        cells = [(fmt[i](v) if fmt and fmt[i] else n(v)) for i, v in enumerate(r)]
        L.append('| ' + ' | '.join(str(x).replace('|', '/').replace('\n', ' ') for x in cells) + ' |')
    L.append('')

meta = dict(q("SELECT key, value FROM meta"))
stats = dict(q("SELECT key, value FROM build_stats"))
p('# Foundation database: quality report'); p()
p(f"Generated {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')} from `{os.path.relpath(DB, BASE)}` (database build date {meta.get('build_date')}). "
  f"Every number below was computed by `qa.py` from the database; nothing is typed by hand.")

# ---------------------------------------------------------------- 1 row counts
h('## 1. Row counts')
rc = json.loads(meta['row_counts'])
table(['table', 'rows'], sorted(rc.items()))
p('Rows by fiscal year (fiscal year = calendar year in which the tax period ends):'); p()
rows = q("""WITH y AS (SELECT fiscal_year FROM pf_returns UNION SELECT fiscal_year FROM pc_filers)
    SELECT y.fiscal_year,
      (SELECT count(*) FROM pf_returns r WHERE r.fiscal_year = y.fiscal_year),
      (SELECT count(*) FROM pf_grants g WHERE g.fiscal_year = y.fiscal_year AND kind = 'paid'),
      (SELECT sum(giving) FROM pf_returns r WHERE r.fiscal_year = y.fiscal_year),
      (SELECT count(*) FROM pc_filers r WHERE r.fiscal_year = y.fiscal_year),
      (SELECT count(*) FROM pc_grants g WHERE g.fiscal_year = y.fiscal_year),
      (SELECT sum(sum_grants_listed) FROM pc_filers r WHERE r.fiscal_year = y.fiscal_year)
    FROM y ORDER BY 1""")
table(['fiscal year', '990-PF returns kept', '990-PF grants paid (rows)', '990-PF giving reported', 'Schedule I filers', 'Schedule I grants (rows)', 'Schedule I dollars listed'],
      rows, [str, n, n, usd, n, n, usd])
p(f"Raw parse before dedupe: {n(int(stats['pf_returns_raw_rows']))} 990-PF rows, {n(int(stats['pf_returns_distinct_objects']))} distinct object IDs; "
  f"{n(int(stats['pf_returns_kept']))} kept after one return per EIN and period end (latest timestamp wins). "
  f"Schedule I filers: {n(int(stats['pc_filers_raw_rows']))} raw, {n(int(stats['pc_filers_distinct_objects']))} distinct objects, {n(int(stats['pc_filers_kept']))} kept.")

# ---------------------------------------------------------------- 2 completeness
h('## 2. Completeness against the IRS index files')
rows = q("SELECT index_year, zips_in_list, zips_done, xml_members, index_rows, parse_errors, index_990pf, parsed_990pf, missing_990pf, pct_parsed, index_990, parsed_990_with_schedule_i FROM qa_completeness ORDER BY 1 DESC")
table(['index year', 'zips listed', 'zips done', 'XML files in zips', 'index rows', 'parse errors', 'index 990PF', 'parsed 990PF', 'missing', 'pct parsed', 'index 990', '990 with Schedule I table'],
      rows, [str, n, n, n, n, n, n, n, n, lambda v: f'{v:.2f}%' if v is not None else '', n, n])
bad = [r for r in rows if r[9] is not None and r[9] < 99.0]
if bad:
    p('Index years with a gap above 1 percent: ' + ', '.join(f'{r[0]} ({r[8]:,} missing, {r[9]:.2f}% parsed)' for r in bad) + '. See the notes below.')
else:
    p('No index year has a gap above 1 percent between 990PF rows in the IRS index and 990-PF returns parsed.')
p()
gap_note = os.path.join(BASE, 'logs', 'completeness_notes.md')
if os.path.exists(gap_note): L.extend(open(gap_note).read().splitlines())

# ---------------------------------------------------------------- 3 grants listed
h('## 3. Returns with grants listed, and listed grants against the reported total')
r = q("""SELECT count(*), count(*) FILTER (n_grants_paid > 0), count(*) FILTER (giving > 0), count(*) FILTER (giving > 0 AND n_grants_paid > 0),
                sum(giving), sum(sum_grants_paid_listed) FROM pf_returns""")[0]
p(f"- 990-PF returns kept: {n(r[0])}. With at least one structured grant row: {n(r[1])} ({pc(r[1], r[0])}).")
p(f"- Returns reporting giving above zero: {n(r[2])}; of those, {n(r[3])} ({pc(r[3], r[2])}) have structured grant rows. The rest have none, so nothing of theirs is in `pf_grants`.")
p(f"- Total giving reported across all kept returns: {usd(r[4])}. Sum of structured grant rows: {usd(r[5])} ({pc(r[5], r[4])} of reported).")
r = q("""SELECT count(*), sum(amount), count(DISTINCT object_id) FROM grants_all WHERE funder_type = 'private_foundation' AND amount > 0
           AND regexp_matches(upper(coalesce(recipient_name, '')), '^(SEE |VARIOUS|ATTACH|ATCH|SCHEDULE|STATEMENT|MISC|MULTIPLE|PER ATTACH|AS PER|LIST ATTACH|N/A$|NONE$|OTHER$|SUNDRY)')""")[0]
t = one("SELECT sum(amount) FROM grants_all WHERE funder_type = 'private_foundation' AND amount > 0 AND NOT amount_flag")
p(f"- Placeholder recipients (name starts with SEE, VARIOUS, ATTACHED, SCHEDULE, STATEMENT, MISCELLANEOUS and similar): {n(r[0])} grant rows on {n(r[2])} returns, {usd(r[1])} ({pc(r[1] or 0, t)} of listed 990-PF dollars). These rows do not name a recipient: the list was attached as a document, or the money went to unnamed individuals (as in the drug-company patient assistance foundations).")
p(); p('Distribution of (sum of listed paid grants) / (total grants paid reported in Part XIV, line 3a), for returns where the reported total is above zero:'); p()
rows = q("""WITH x AS (SELECT sum_grants_paid_listed / total_grants_paid AS ratio, total_grants_paid FROM pf_returns WHERE total_grants_paid > 0),
    b AS (SELECT CASE WHEN ratio = 0 THEN '1. zero listed' WHEN ratio < 0.5 THEN '2. under 0.5' WHEN ratio < 0.95 THEN '3. 0.5 to 0.95'
                      WHEN ratio <= 1.05 THEN '4. 0.95 to 1.05 (agrees)' WHEN ratio <= 2 THEN '5. 1.05 to 2' ELSE '6. over 2' END AS bucket, total_grants_paid FROM x)
    SELECT bucket, count(*), 100.0 * count(*) / sum(count(*)) OVER (), sum(total_grants_paid), 100.0 * sum(total_grants_paid) / sum(sum(total_grants_paid)) OVER () FROM b GROUP BY 1 ORDER BY 1""")
table(['ratio bucket', 'returns', 'pct of returns', 'reported dollars', 'pct of dollars'], rows, [str, n, lambda v: f'{v:.1f}%', usd, lambda v: f'{v:.1f}%'])
r = q("SELECT quantile_cont(sum_grants_paid_listed / total_grants_paid, [0.05, 0.25, 0.5, 0.75, 0.95]) FROM pf_returns WHERE total_grants_paid > 0")[0][0]
p('Ratio percentiles (5, 25, 50, 75, 95): ' + ', '.join(f'{x:.3f}' for x in r))
r = q("SELECT count(*), count(*) FILTER (n_grants_listed > 0), sum(grants_paid_total), sum(sum_grants_listed) FROM pc_filers")[0]
p(); p(f"Schedule I filers kept: {n(r[0])}; Schedule I dollars listed {usd(r[3])} against {usd(r[2])} total grants reported on Form 990 Part I line 13 "
       f"({pc(r[3], r[2])}; the Part I figure also includes grants to individuals and foreign grants, which Schedule I recipient tables do not list).")

# ---------------------------------------------------------------- 4 top 20
h('## 4. Sanity check: largest foundations')
p('Top 20 by fair market value of assets (latest return on file):'); p()
rows = q("SELECT name, state, ein, fiscal_year_latest, fmv_assets_eoy, giving_latest, n_returns FROM foundations WHERE NOT efile_missing ORDER BY fmv_assets_eoy DESC NULLS LAST LIMIT 20")
table(['foundation', 'state', 'EIN', 'latest FY', 'FMV assets', 'giving (latest)', 'returns on file'], rows, [str, str, str, str, usd, usd, n])
p('Top 20 by giving on the latest return:'); p()
rows = q("SELECT name, state, ein, fiscal_year_latest, giving_latest, fmv_assets_eoy, flag_amounts_suspect FROM foundations WHERE NOT efile_missing ORDER BY giving_latest DESC NULLS LAST LIMIT 20")
table(['foundation', 'state', 'EIN', 'latest FY', 'giving (latest)', 'FMV assets', 'flagged'], rows, [str, str, str, str, usd, usd, str])
p('Top 10 public charity grantmakers by Schedule I dollars on the latest return:'); p()
rows = q("SELECT name, state, ein, fiscal_year_latest, grants_listed_latest, n_grants_latest, total_assets_eoy FROM pc_funders ORDER BY grants_listed_latest DESC NULLS LAST LIMIT 10")
table(['organization', 'state', 'EIN', 'latest FY', 'Schedule I dollars', 'grants', 'total assets'], rows, [str, str, str, str, usd, n, usd])

# ---------------------------------------------------------------- 5 missing fields
h('## 5. Missing fields in grants_all')
rows = q("""SELECT funder_type, count(*), 100.0 * count(*) FILTER (recipient_state IS NULL) / count(*), 100.0 * count(*) FILTER (recipient_zip IS NULL) / count(*),
       100.0 * count(*) FILTER (recipient_county IS NULL) / count(*), 100.0 * count(*) FILTER (purpose IS NULL OR trim(purpose) = '') / count(*),
       100.0 * count(*) FILTER (amount IS NULL) / count(*), 100.0 * count(*) FILTER (amount = 0) / count(*), 100.0 * count(*) FILTER (recipient_name IS NULL) / count(*),
       100.0 * count(*) FILTER (recipient_country <> 'US') / count(*)
    FROM grants_all GROUP BY ROLLUP (funder_type) ORDER BY 1 NULLS LAST""")
f1 = lambda v: f'{v:.2f}%'
table(['funder type', 'grants', 'no US state', 'no ZIP', 'no county', 'no purpose', 'no amount', 'amount zero', 'no recipient name', 'foreign address'],
      [(r[0] or 'ALL',) + r[1:] for r in rows], [str, n, f1, f1, f1, f1, f1, f1, f1, f1])
p('"No US state" covers foreign recipients and blank or invalid state fields (exported under `XX`). A state was filled from the ZIP code when the state field was unusable.')
rows = q("""SELECT upper(trim(purpose)) AS p, count(*) FROM grants_all WHERE purpose IS NOT NULL GROUP BY 1 ORDER BY 2 DESC LIMIT 12""")
tot = one("SELECT count(*) FROM grants_all")
p(); p('Most common purpose strings (shows how generic the purpose field often is):'); p()
table(['purpose text', 'grants', 'pct of all grants'], [(r[0][:70], r[1], 100.0 * r[1] / tot) for r in rows], [str, n, f1])

# ---------------------------------------------------------------- 6 duplicates
h('## 6. Duplicate checks')
checks = [
 ('pf_returns: object_id unique', "SELECT count(*) - count(DISTINCT object_id) FROM pf_returns"),
 ('pf_returns: (ein, period_end) unique', "SELECT count(*) FROM (SELECT ein, period_end FROM pf_returns GROUP BY ALL HAVING count(*) > 1)"),
 ('pf_grants: (object_id, kind, seq) unique', "SELECT count(*) FROM (SELECT object_id, kind, seq FROM pf_grants GROUP BY ALL HAVING count(*) > 1)"),
 ('pf_grants: rows whose return is not in pf_returns', "SELECT count(*) FROM pf_grants g WHERE NOT EXISTS (SELECT 1 FROM pf_returns r WHERE r.object_id = g.object_id)"),
 ('pc_filers: (ein, period_end) unique', "SELECT count(*) FROM (SELECT ein, period_end FROM pc_filers GROUP BY ALL HAVING count(*) > 1)"),
 ('pc_grants: (object_id, seq) unique', "SELECT count(*) FROM (SELECT object_id, seq FROM pc_grants GROUP BY ALL HAVING count(*) > 1)"),
 ('grants_all: (funder_type, object_id, seq) unique', "SELECT count(*) FROM (SELECT funder_type, object_id, seq FROM grants_all GROUP BY ALL HAVING count(*) > 1)"),
 ('grants_all rows = pf_grants paid + pc_grants', "SELECT (SELECT count(*) FROM grants_all) - (SELECT count(*) FROM pf_grants WHERE kind = 'paid') - (SELECT count(*) FROM pc_grants)"),
 ('foundations: ein unique', "SELECT count(*) - count(DISTINCT ein) FROM foundations"),
 ('pc_funders: ein unique', "SELECT count(*) - count(DISTINCT ein) FROM pc_funders"),
 ('bmf: ein unique', "SELECT count(*) - count(DISTINCT ein) FROM bmf"),
 ('pf_returns: n_grants_paid (counts rows that have an amount) differs from rows in pf_grants; informational', "SELECT count(*) FROM pf_returns r LEFT JOIN (SELECT object_id, count(*) n FROM pf_grants WHERE kind = 'paid' GROUP BY 1) g USING (object_id) WHERE coalesce(g.n, 0) <> r.n_grants_paid"),
]
table(['check', 'violations (0 is a pass)'], [(name, one(sql)) for name, sql in checks])
p(f"Duplicate grant keys found before dedupe: 990-PF {stats.get('pf_grants_dup_keys_before_dedupe')}, Schedule I {stats.get('pc_grants_dup_keys_before_dedupe')}. "
  f"Superseded returns dropped (same EIN and period end, older timestamp): {n(int(stats['pf_returns_distinct_objects']) - int(stats['pf_returns_null_ein']) - int(stats['pf_returns_kept']))}.")
r = q("""SELECT count(*), sum(c) FROM (SELECT funder_ein, fiscal_year, recipient_key, amount, purpose, count(*) c FROM grants_all WHERE amount > 0 GROUP BY ALL HAVING count(*) > 1)""")[0]
p(f"Repeated identical grant lines inside the data (same funder, fiscal year, recipient, amount and purpose): {n(r[0])} groups covering {n(r[1])} rows. "
  "These are kept: many are genuine repeat payments (monthly or quarterly installments) and the returns list them that way.")

# ---------------------------------------------------------------- 7 flags
h('## 7. Negative or implausible amounts (flagged, not deleted)')
rows = q("SELECT funder_type, coalesce(amount_flag_reason, 'not flagged'), count(*), sum(amount) FROM grants_all GROUP BY 1, 2 ORDER BY 1, 2")
table(['funder type', 'amount_flag_reason', 'grants', 'sum of amounts'], rows, [str, str, n, usd])
p('`amount_flag = true` rows stay in every table and export. Profile aggregates (median, average, top states, home state share) and the query tool rankings exclude them.')
p(); p('Largest flagged grants:'); p()
rows = q("SELECT funder_name, recipient_name, fiscal_year, amount, amount_flag_reason FROM grants_all WHERE amount_flag ORDER BY abs(amount) DESC LIMIT 10")
table(['funder', 'recipient', 'FY', 'amount', 'reason'], rows, [str, str, str, usd, str])
p('Largest unflagged grants (for eyeballing):'); p()
rows = q("SELECT funder_name, recipient_name, fiscal_year, amount, funder_type FROM grants_all WHERE NOT amount_flag ORDER BY amount DESC LIMIT 10")
table(['funder', 'recipient', 'FY', 'amount', 'funder type'], rows, [str, str, str, usd, str])
r = q("SELECT count(*), count(*) FILTER (flag_amounts_suspect) FROM pf_returns")[0]
p(f"Returns with `flag_amounts_suspect = true` (negative or impossible assets or giving figures): {n(r[1])} of {n(r[0])}.")

# ---------------------------------------------------------------- 8 preselected
h('## 8. Foundations that give only to preselected organizations')
r = q("""SELECT count(*), count(*) FILTER (only_preselected), sum(giving_latest), sum(giving_latest) FILTER (only_preselected),
         sum(fmv_assets_eoy), sum(fmv_assets_eoy) FILTER (only_preselected), count(*) FILTER (ever_listed_application_info),
         count(*) FILTER (NOT only_preselected AND giving_latest > 0), count(*) FILTER (NOT only_preselected AND ever_listed_application_info AND giving_latest > 0) FROM foundations WHERE NOT efile_missing""")[0]
p(f"- Foundations with an e-filed return: {n(r[0])}. Box checked on the latest return (only_preselected = true): {n(r[1])} ({pc(r[1], r[0])}).")
p(f"- Their share of latest-year giving: {usd(r[3])} of {usd(r[2])} ({pc(r[3], r[2])}). Share of assets: {pc(r[5], r[4])}.")
p(f"- Foundations that ever listed application information: {n(r[6])}. Not preselected and giving above zero: {n(r[7])}; of those, with application information: {n(r[8])}.")
r = q("SELECT count(*), count(*) FILTER (operating_foundation), count(*) FILTER (final_return_latest) FROM foundations WHERE NOT efile_missing")[0]
p(f"- Operating foundations (latest return answers yes): {n(r[1])} ({pc(r[1], r[0])}). Latest return marked final: {n(r[2])}.")

# ---------------------------------------------------------------- 9 universe and enrichment
h('## 9. Universe, Business Master File and geography')
rows = q("SELECT efile_missing, in_bmf, coalesce(bmf_foundation_code, 'none'), count(*) FROM foundations GROUP BY ALL ORDER BY 1, 2, 3")
table(['efile_missing', 'in_bmf', 'BMF foundation code', 'foundations'], rows, [str, str, str, n])
p('BMF foundation codes: 02 exempt operating foundation, 03 private operating foundation, 04 private non-operating foundation, 00 not coded (mostly 4947(a)(1) trusts), 10 to 17 public charity classes.')
r = q("""SELECT count(*), count(*) FILTER (efile_missing), count(county), count(website) FILTER (NOT efile_missing), count(*) FILTER (NOT efile_missing),
         count(ntee_cd), sum(bmf_asset_amt) FILTER (efile_missing), count(phone) FILTER (NOT efile_missing) FROM foundations""")[0]
p(f"- `foundations` rows: {n(r[0])}; of these {n(r[1])} are in the BMF as private foundations but have no e-filed return here (efile_missing). Their BMF assets total {usd(r[6])}.")
p(f"- County assigned: {pc(r[2], r[0])} of rows. NTEE code present: {pc(r[5], r[0])}. Website present (after removing N/A and third-party sites): {pc(r[3], r[4])} of e-filed foundations. Phone present: {pc(r[7], r[4])}.")
r = q("SELECT count(*), count(*) FILTER (source = 'census_zcta'), count(*) FILTER (source <> 'census_zcta') FROM zip_county")[0]
p(f"- `zip_county`: {n(r[0])} ZIP codes; {n(r[1])} from the Census ZCTA file, {n(r[2])} PO box or unique ZIPs inferred from the dominant county of the same city in the BMF.")
p(f"- BMF: {n(rc.get('bmf'))} organizations, downloaded {stats.get('bmf_download_date')}.")

# ---------------------------------------------------------------- 10 match rate
h('## 10. Recipient matching to the Business Master File')
rows = q("""SELECT funder_type, CASE WHEN recipient_ein IS NOT NULL THEN 'EIN reported on return' ELSE coalesce(recipient_match_method, 'no match') END, count(*), sum(amount) FILTER (amount > 0 AND NOT amount_flag)
            FROM grants_all GROUP BY 1, 2 ORDER BY 1, 2""")
tt = {}
for r in rows: tt.setdefault(r[0], [0, 0]); tt[r[0]][0] += r[2]; tt[r[0]][1] += r[3] or 0
table(['funder type', 'recipient EIN source', 'grants', 'pct of grants', 'dollars', 'pct of dollars'],
      [(r[0], r[1], r[2], 100.0 * r[2] / tt[r[0]][0], r[3], 100.0 * (r[3] or 0) / tt[r[0]][1]) for r in rows], [str, str, n, f1, usd, f1])
r = q("""SELECT count(*), count(recipient_ein_matched), sum(amount) FILTER (amount > 0 AND NOT amount_flag), sum(amount) FILTER (amount > 0 AND NOT amount_flag AND recipient_ein_matched IS NOT NULL)
         FROM grants_all WHERE funder_type = 'private_foundation'""")[0]
p(f"**990-PF match rate: {pc(r[1], r[0])} of grants ({n(r[1])} of {n(r[0])}) and {pc(r[3], r[2])} of dollars carry a matched EIN.** Unmatched does not mean the recipient is not a charity: "
  "common reasons are a name that is not unique in its state or ZIP, a name written differently from the IRS record, churches and government bodies that are not in the BMF, individuals (scholarships), and foreign recipients.")
r = q("SELECT count(*), count(recipient_ein_best), count(recipient_ntee) FROM grants_all")[0]
p(f"Across both funder types, {pc(r[1], r[0])} of grants have a recipient EIN (reported or matched) and {pc(r[2], r[0])} have a recipient NTEE code.")
p(); p('Accuracy check: the same name matching was run on Schedule I grants, which carry a reported EIN, and the matched EIN was compared with the reported one:'); p()
rows = q("SELECT method, grants_tested, matched_ein_equals_reported, matched_ein_differs, reported_ein_in_bmf FROM qa_match_validation ORDER BY 1")
table(['method', 'grants tested', 'matched EIN equals reported', 'matched EIN differs', 'agreement'],
      [(r[0], r[1], r[2], r[3], (100.0 * r[2] / (r[2] + r[3])) if (r[0] != 'no_match' and (r[2] or 0) + (r[3] or 0)) else None) for r in rows], [str, n, n, n, lambda v: '' if v is None else f'{v:.1f}%'])
tm = sum((r[2] or 0) + (r[3] or 0) for r in rows if r[0] != 'no_match'); ta = sum(r[2] or 0 for r in rows if r[0] != 'no_match'); tall = sum(r[1] for r in rows)
p(f"On this test set the matcher assigned an EIN to {pc(tm, tall)} of grants, and {pc(ta, tm)} of the assigned EINs equal the EIN the funder reported. "
  "Disagreements include related entities (a chapter against its national office, a foundation arm against its parent), so the true error rate is somewhat lower than the raw disagreement.")

# ---------------------------------------------------------------- 11 tags
h('## 11. Subject tags')
r = q("SELECT count(*), count(*) FILTER (len(tags) > 0), avg(len(tags)) FROM grants_all")[0]
p(f"{pc(r[1], r[0])} of grants carry at least one tag (average {r[2]:.2f} tags per grant). Keyword lists live in `taxonomy.json`."); p()
table(['tag', 'label', 'grants', 'dollars'], q("SELECT tag, label, n_grants, dollars FROM subject_tags ORDER BY n_grants DESC"), [str, str, n, usd])

# ---------------------------------------------------------------- 12 ProPublica spot check
h('## 12. Hand check of five random foundations against ProPublica Nonprofit Explorer')
if '--no-propublica' in sys.argv:
    p('Skipped (run without --no-propublica).')
else:
    random.seed(20261112)
    eins = [r[0] for r in q("SELECT ein FROM foundations WHERE NOT efile_missing AND fmv_assets_eoy > 0 AND n_returns >= 2 ORDER BY ein")]
    sample = random.sample(eins, 5); out = []
    for ein in sample:
        url = f'https://projects.propublica.org/nonprofits/api/v2/organizations/{ein}.json'
        ours = q(f"SELECT name, state, fiscal_year_latest FROM foundations WHERE ein = '{ein}'")[0]
        rets = {r[0]: r for r in q(f"SELECT strftime(period_end, '%Y%m'), total_assets_eoy, fmv_assets_eoy, giving FROM pf_returns WHERE ein = '{ein}'")}
        try:
            r = subprocess.run(['curl', '-sS', '-m', '40', url], capture_output=True, text=True, timeout=60)
            d = json.loads(r.stdout); o = d['organization']
            hit = None
            for f in d.get('filings_with_data', []):
                if str(f.get('tax_prd')) in rets: hit = f; break
            if hit:
                mine = rets[str(hit['tax_prd'])]
                same = mine[1] is not None and hit.get('totassetsend') is not None and abs(mine[1] - hit['totassetsend']) <= 1
                out.append((ein, ours[0], o.get('name'), ours[1], o.get('state'), str(hit['tax_prd']), usd(mine[1]), usd(hit.get('totassetsend')),
                            'match' if (same and (ours[1] or '') == (o.get('state') or '')) else ('assets match, state differs' if same else 'DIFFERS')))
            else:
                out.append((ein, ours[0], o.get('name'), ours[1], o.get('state'), 'no common period', '', usd(o.get('asset_amount')) + ' (BMF figure)', 'name and state only'))
        except Exception as e:
            out.append((ein, ours[0], '', ours[1], '', '', '', '', 'lookup failed: ' + repr(e)[:60]))
    table(['EIN', 'name here', 'name at ProPublica', 'state here', 'state there', 'tax period compared', 'book assets here', 'book assets there', 'result'], out, [str] * 9)
    p('Sample drawn with a fixed random seed from foundations with at least two returns. Assets compared are total assets at end of year (book value) for the same tax period. '
      'Source: `https://projects.propublica.org/nonprofits/api/v2/organizations/<EIN>.json`.')

# ---------------------------------------------------------------- 13 limitations
h('## 13. Known limitations')
for x in json.loads(meta['caveats']): p('- ' + x)
ex = os.path.join(BASE, 'logs', 'extra_limitations.md')
if os.path.exists(ex): L.extend(open(ex).read().splitlines())

txt = '\n'.join(L).replace('—', '-').replace('–', '-') + '\n'
open(OUT, 'w').write(txt)
print('wrote', OUT, len(txt), 'chars')
