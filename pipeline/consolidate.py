#!/usr/bin/env python3
"""
Stage 2: consolidate parquet parts (parts_v2/) into db/fdb.duckdb.
Usage:  python3 consolidate.py            (all steps, in order)
        python3 consolidate.py STEP ...   (only the named steps)
Steps:  macros pf pc bmf zip completeness g0 match grants_all recipients foundations pc_funders indexes meta
        tags  (re-apply taxonomy.json to grants_all after editing it, then rebuild indexes)
Environment: FDB_PARTS (default parts_v3), FDB_DB (default db/fdb.duckdb).
"""
import os, sys, json, time, glob, datetime, csv
import duckdb

BASE = os.path.dirname(os.path.abspath(__file__))
PARTS = os.environ.get('FDB_PARTS', os.path.join(BASE, 'parts_v3'))
DB = os.environ.get('FDB_DB', os.path.join(BASE, 'db', 'fdb.duckdb'))
TMP = os.path.join(BASE, 'duck_tmp')
BMF_DIR = os.path.join(BASE, 'src', 'bmf')
ZCTA = os.path.join(BASE, 'src', 'census', 'zcta_county.txt')
MEM = os.environ.get('FDB_MEM', '5GB')
SCRATCH = os.path.join(TMP, os.path.basename(DB) + '.scratch')
os.makedirs(os.path.dirname(DB), exist_ok=True); os.makedirs(TMP, exist_ok=True)

US_STATES = ['AL','AK','AZ','AR','CA','CO','CT','DE','DC','FL','GA','HI','ID','IL','IN','IA','KS','KY','LA','ME','MD','MA','MI','MN','MS',
             'MO','MT','NE','NV','NH','NJ','NM','NY','NC','ND','OH','OK','OR','PA','RI','SC','SD','TN','TX','UT','VT','VA','WA','WV','WI','WY',
             'PR','VI','GU','AS','MP']
FIPS = {'01':'AL','02':'AK','04':'AZ','05':'AR','06':'CA','08':'CO','09':'CT','10':'DE','11':'DC','12':'FL','13':'GA','15':'HI','16':'ID',
        '17':'IL','18':'IN','19':'IA','20':'KS','21':'KY','22':'LA','23':'ME','24':'MD','25':'MA','26':'MI','27':'MN','28':'MS','29':'MO',
        '30':'MT','31':'NE','32':'NV','33':'NH','34':'NJ','35':'NM','36':'NY','37':'NC','38':'ND','39':'OH','40':'OK','41':'OR','42':'PA',
        '44':'RI','45':'SC','46':'SD','47':'TN','48':'TX','49':'UT','50':'VT','51':'VA','53':'WA','54':'WV','55':'WI','56':'WY',
        '60':'AS','66':'GU','69':'MP','72':'PR','78':'VI'}
STATES_SQL = '(' + ','.join(f"'{s}'" for s in US_STATES) + ')'
GRANT_SORT = "recipient_state NULLS LAST, recipient_key NULLS LAST, fiscal_year DESC NULLS LAST, amount DESC NULLS LAST"

def connect(read_only=False):
    c = duckdb.connect(DB, read_only=read_only)
    c.execute(f"PRAGMA memory_limit='{MEM}'"); c.execute(f"PRAGMA temp_directory='{TMP}'"); c.execute("PRAGMA threads=2")
    if not read_only:   # intermediate tables live in a scratch database so the main file does not carry dead space
        c.execute(f"ATTACH '{SCRATCH}' AS scratch")
    return c

T0 = time.time()
def log(*a):
    print(f'[{time.time() - T0:7.0f}s]', *a, flush=True)

def stat(c, key, value):
    c.execute("CREATE TABLE IF NOT EXISTS build_stats (key VARCHAR, value VARCHAR)")
    c.execute("DELETE FROM build_stats WHERE key = ?", [key])
    c.execute("INSERT INTO build_stats VALUES (?, ?)", [key, str(value)])

def one(c, sql):
    return c.execute(sql).fetchone()[0]

# ------------------------------------------------------------------ macros
def step_macros(c):
    c.execute(r"""CREATE OR REPLACE MACRO name_key(s) AS
        nullif(regexp_replace(trim(regexp_replace(regexp_replace(regexp_replace(replace(upper(strip_accents(s)), '&', ' AND '),
               '[''’`\.]', '', 'g'), '[^A-Z0-9 ]', ' ', 'g'), ' +', ' ', 'g')), '^THE ', ''), '')""")
    c.execute(r"""CREATE OR REPLACE MACRO name_key2(k) AS
        nullif(trim(regexp_replace(regexp_replace(regexp_replace(regexp_replace(regexp_replace(regexp_replace(regexp_replace(regexp_replace(k,
               '\b(FDN|FNDN|FDTN|FOUND)\b', 'FOUNDATION', 'g'), '\b(ASSN|ASSOC)\b', 'ASSOCIATION', 'g'), '\bUNIV\b', 'UNIVERSITY', 'g'),
               '\b(CTR|CNTR)\b', 'CENTER', 'g'), '\bNATL\b', 'NATIONAL', 'g'), '\bSOC\b', 'SOCIETY', 'g'),
               '\b(INC|INCORPORATED|CORP|CORPORATION|LLC|LTD|NFP|CO|COMPANY|THE|OF|AND|FOR)\b', '', 'g'), ' +', ' ', 'g')), '')""")
    c.execute(r"""CREATE OR REPLACE MACRO zip5(z) AS CASE WHEN regexp_matches(trim(z), '^[0-9]{5}') THEN substr(trim(z), 1, 5) END""")
    c.execute(r"""CREATE OR REPLACE MACRO clean_website(w) AS CASE
        WHEN w IS NULL THEN NULL
        WHEN regexp_matches(upper(trim(w)), '^("?N[/\\. ]*A\.?"?( .*)?|NONE.*|NO|NULL|NIL|0+|NOT? .*|SEE .*|(HTTPS?://)?(WWW\.)?GUIDESTAR\.(ORG|COM)/?|(HTTPS?://)?(WWW\.)?IRS\.GOV/?|[-_.X ]+|TBD|UNKNOWN|PENDING|IN PROCESS|UNDER CONSTRUCTION)$') THEN NULL
        WHEN position('.' IN w) = 0 THEN NULL
        WHEN regexp_matches(upper(w), 'GUIDESTAR|CANDID\.ORG|FOUNDATIONCENTER|FDNCENTER|PROPUBLICA|CHARITIESNYS|SUNBIZ\.ORG|TAX990|IRS\.GOV|AG\.CA\.GOV|CAAG\.STATE|OAG\.CA\.GOV|CITIZENAUDIT|CHARITYNAVIGATOR|ERI-NONPROFIT|UPON REQUEST|AVAILABLE AT|NOT APPLICABLE|NO WEBSITE') THEN NULL
        ELSE lower(trim(w)) END""")
    c.execute(r"""CREATE OR REPLACE MACRO clean_ein(e) AS CASE WHEN regexp_matches(regexp_replace(e, '[^0-9]', '', 'g'), '^[0-9]{9}$')
        AND regexp_replace(e, '[^0-9]', '', 'g') NOT IN ('000000000', '999999999', '123456789', '111111111') THEN regexp_replace(e, '[^0-9]', '', 'g') END""")

# ------------------------------------------------------------------ private foundations (Form 990-PF)
def zip_of(col, kind):
    return f"regexp_extract({col}, '{kind}__(.*)__c[0-9]+\\.parquet$', 1)"

def step_pf(c):
    P = PARTS
    c.execute("SET preserve_insertion_order=false")
    raw = c.execute(f"SELECT count(*), count(DISTINCT object_id), count(*) FILTER (ein IS NULL) FROM read_parquet('{P}/returns__*.parquet')").fetchone()
    stat(c, 'pf_returns_raw_rows', raw[0]); stat(c, 'pf_returns_distinct_objects', raw[1]); stat(c, 'pf_returns_null_ein', raw[2])
    log('pf raw returns', raw)
    c.execute(f"""CREATE OR REPLACE TABLE pf_returns AS
    WITH r1 AS (SELECT * FROM read_parquet('{P}/returns__*.parquet')
                QUALIFY row_number() OVER (PARTITION BY object_id ORDER BY source_zip DESC) = 1),
    r2 AS (SELECT *, try_cast(period_end AS DATE) AS pe, try_cast(return_ts AS TIMESTAMPTZ) AS ts FROM r1 WHERE ein IS NOT NULL AND try_cast(period_end AS DATE) IS NOT NULL),
    r3 AS (SELECT * FROM r2 QUALIFY row_number() OVER (PARTITION BY ein, pe ORDER BY ts DESC NULLS LAST, object_id DESC) = 1)
    SELECT object_id, ein, trim(coalesce(name, '') || coalesce(' ' || name2, '')) AS name, year(pe)::INTEGER AS fiscal_year,
        try_cast(period_begin AS DATE) AS period_begin, pe AS period_end, try_cast(tax_year AS INTEGER) AS tax_year, ts AS return_ts,
        return_type, amended, final_return, addr1, city, upper(trim(state)) AS state, zip, zip5(zip) AS zip5, country, phone,
        clean_website(website) AS website, website AS website_raw, signer_name, signer_title,
        fmv_assets_eoy, total_assets_eoy, net_assets_eoy, total_revenue, contributions_received, total_expenses,
        grants_paid_books, grants_paid_charitable, total_grants_paid, total_grants_future,
        coalesce(nullif(total_grants_paid, 0), nullif(grants_paid_charitable, 0), nullif(grants_paid_books, 0), 0) AS giving,
        only_preselected, operating_foundation, n_grants_paid, sum_grants_paid_listed, n_officers, n_app_contacts,
        direct_charitable_activities,
        (coalesce(fmv_assets_eoy, 0) < 0 OR coalesce(fmv_assets_eoy, 0) > 2e11 OR coalesce(total_grants_paid, 0) < 0
          OR coalesce(total_grants_paid, 0) > 5e10
          OR (coalesce(total_grants_paid, 0) > 1e6 AND coalesce(fmv_assets_eoy, 0) + coalesce(total_revenue, 0) > 0
              AND total_grants_paid > 20 * (coalesce(fmv_assets_eoy, 0) + abs(coalesce(total_revenue, 0)) + coalesce(total_expenses, 0)))) AS flag_amounts_suspect,
        source_zip, try_cast(substr(source_zip, 1, 4) AS INTEGER) AS index_year
    FROM r3 ORDER BY ein, pe""")
    n = one(c, "SELECT count(*) FROM pf_returns"); stat(c, 'pf_returns_kept', n); log('pf_returns', n)

    c.execute(f"""CREATE OR REPLACE TABLE pf_grants AS
    SELECT g.object_id, g.kind, g.seq, r.ein AS funder_ein, r.name AS funder_name, r.city AS funder_city, r.state AS funder_state,
        r.fiscal_year, r.period_end, g.recipient_name, name_key(g.recipient_name) AS recipient_key, g.recipient_addr1, g.recipient_city,
        g.recipient_state, g.recipient_zip, g.recipient_country, g.relationship, g.foundation_status, g.purpose, g.amount
    FROM read_parquet('{P}/grants__*.parquet', filename = true) g
    JOIN pf_returns r ON g.object_id = r.object_id AND {zip_of('g.filename', 'grants')} = r.source_zip""")
    dups = one(c, "SELECT count(*) FROM (SELECT object_id, kind, seq FROM pf_grants GROUP BY ALL HAVING count(*) > 1)")
    stat(c, 'pf_grants_dup_keys_before_dedupe', dups)
    if dups:
        log('pf_grants duplicate keys', dups, 'deduping')
        c.execute("""CREATE OR REPLACE TABLE pf_grants AS SELECT * FROM pf_grants
                     QUALIFY row_number() OVER (PARTITION BY object_id, kind, seq ORDER BY amount DESC NULLS LAST) = 1""")
    n = one(c, "SELECT count(*) FROM pf_grants"); stat(c, 'pf_grants', n); log('pf_grants', n)

    c.execute(f"""CREATE OR REPLACE TABLE pf_officers AS
    SELECT o.object_id, row_number() OVER (PARTITION BY o.object_id ORDER BY o.filename, o.file_row_number)::INTEGER AS seq,
        r.ein AS funder_ein, r.name AS funder_name, r.state AS funder_state, r.fiscal_year, r.period_end,
        o.name, o.title, o.hours, o.compensation, o.city, o.state
    FROM read_parquet('{P}/officers__*.parquet', filename = true, file_row_number = true) o
    JOIN pf_returns r ON o.object_id = r.object_id AND {zip_of('o.filename', 'officers')} = r.source_zip
    ORDER BY funder_ein, period_end, seq""")
    n = one(c, "SELECT count(*) FROM pf_officers"); stat(c, 'pf_officers', n); log('pf_officers', n)

    c.execute(f"""CREATE OR REPLACE TABLE pf_app_info AS
    SELECT a.object_id, row_number() OVER (PARTITION BY a.object_id ORDER BY a.filename, a.file_row_number)::INTEGER AS seq,
        r.ein AS funder_ein, r.name AS funder_name, r.state AS funder_state, r.fiscal_year, r.period_end,
        a.contact_name, a.addr1, a.city, a.state, a.zip, a.phone, a.email, a.form_and_materials, a.deadlines, a.restrictions
    FROM read_parquet('{P}/apps__*.parquet', filename = true, file_row_number = true) a
    JOIN pf_returns r ON a.object_id = r.object_id AND {zip_of('a.filename', 'apps')} = r.source_zip
    ORDER BY funder_ein, period_end, seq""")
    n = one(c, "SELECT count(*) FROM pf_app_info"); stat(c, 'pf_app_info', n); log('pf_app_info', n)

# ------------------------------------------------------------------ public charities (Form 990 Schedule I)
def step_pc(c):
    P = PARTS
    c.execute("SET preserve_insertion_order=false")
    raw = c.execute(f"SELECT count(*), count(DISTINCT object_id) FROM read_parquet('{P}/filers990__*.parquet')").fetchone()
    stat(c, 'pc_filers_raw_rows', raw[0]); stat(c, 'pc_filers_distinct_objects', raw[1]); log('pc raw filers', raw)
    c.execute(f"""CREATE OR REPLACE TABLE pc_filers AS
    WITH r1 AS (SELECT * FROM read_parquet('{P}/filers990__*.parquet')
                QUALIFY row_number() OVER (PARTITION BY object_id ORDER BY source_zip DESC) = 1),
    r2 AS (SELECT *, try_cast(period_end AS DATE) AS pe FROM r1 WHERE ein IS NOT NULL AND try_cast(period_end AS DATE) IS NOT NULL),
    r3 AS (SELECT * FROM r2 QUALIFY row_number() OVER (PARTITION BY ein, pe ORDER BY object_id DESC) = 1)
    SELECT object_id, ein, name, year(pe)::INTEGER AS fiscal_year, pe AS period_end, try_cast(tax_year AS INTEGER) AS tax_year,
        addr1, city, upper(trim(state)) AS state, zip, zip5(zip) AS zip5, phone, clean_website(website) AS website, website AS website_raw,
        mission, principal_officer, total_revenue, total_assets_eoy, grants_paid_total, n_grants_listed, sum_grants_listed,
        source_zip, try_cast(substr(source_zip, 1, 4) AS INTEGER) AS index_year
    FROM r3 ORDER BY ein, pe""")
    n = one(c, "SELECT count(*) FROM pc_filers"); stat(c, 'pc_filers_kept', n); log('pc_filers', n)
    c.execute(f"""CREATE OR REPLACE TABLE pc_grants AS
    SELECT g.object_id, g.seq, r.ein AS funder_ein, r.name AS funder_name, r.city AS funder_city, r.state AS funder_state,
        r.fiscal_year, r.period_end, g.recipient_name, name_key(g.recipient_name) AS recipient_key, clean_ein(g.recipient_ein) AS recipient_ein,
        g.recipient_addr1, g.recipient_city, g.recipient_state, g.recipient_zip, g.recipient_country, g.irc_section, g.purpose,
        g.cash_amount, g.noncash_amount, coalesce(g.cash_amount, 0) + coalesce(g.noncash_amount, 0) AS amount
    FROM read_parquet('{P}/grants990__*.parquet', filename = true) g
    JOIN pc_filers r ON g.object_id = r.object_id AND {zip_of('g.filename', 'grants990')} = r.source_zip""")
    dups = one(c, "SELECT count(*) FROM (SELECT object_id, seq FROM pc_grants GROUP BY ALL HAVING count(*) > 1)")
    stat(c, 'pc_grants_dup_keys_before_dedupe', dups)
    if dups:
        log('pc_grants duplicate keys', dups, 'deduping')
        c.execute("""CREATE OR REPLACE TABLE pc_grants AS SELECT * FROM pc_grants
                     QUALIFY row_number() OVER (PARTITION BY object_id, seq ORDER BY amount DESC NULLS LAST) = 1""")
    n = one(c, "SELECT count(*) FROM pc_grants"); stat(c, 'pc_grants', n); log('pc_grants', n)

# ------------------------------------------------------------------ IRS Business Master File
def step_bmf(c):
    files = sorted(glob.glob(os.path.join(BMF_DIR, 'eo*.csv')))
    lst = '[' + ','.join(f"'{f}'" for f in files) + ']'
    c.execute(f"""CREATE OR REPLACE TABLE bmf AS
    WITH b AS (SELECT *, row_number() OVER (PARTITION BY EIN ORDER BY TAX_PERIOD DESC NULLS LAST, filename) AS rn
               FROM read_csv({lst}, header = true, all_varchar = true, union_by_name = true, filename = true))
    SELECT EIN AS ein, NAME AS name, STREET AS street, CITY AS city, STATE AS state, ZIP AS zip, zip5(ZIP) AS zip5,
        SUBSECTION AS subsection, FOUNDATION AS foundation, nullif(trim(NTEE_CD), '') AS ntee_cd,
        try_cast(ASSET_AMT AS BIGINT) AS asset_amt, try_cast(INCOME_AMT AS BIGINT) AS income_amt, try_cast(REVENUE_AMT AS BIGINT) AS revenue_amt,
        RULING AS ruling, TAX_PERIOD AS tax_period, name_key(NAME) AS name_key, name_key2(name_key(NAME)) AS name_key2,
        regexp_extract(filename, '(eo[^/]*)\\.csv$', 1) AS source_file
    FROM b WHERE rn = 1 AND EIN IS NOT NULL ORDER BY ein""")
    n = one(c, "SELECT count(*) FROM bmf"); stat(c, 'bmf', n); log('bmf', n)
    d = datetime.date.fromtimestamp(min(os.path.getmtime(f) for f in files)).isoformat()
    stat(c, 'bmf_download_date', d); stat(c, 'bmf_files', ', '.join(os.path.basename(f) for f in files))

# ------------------------------------------------------------------ ZIP to county
def step_zip(c):
    if not os.path.exists(ZCTA):
        log('zip_county skipped: no Census file'); stat(c, 'zip_county', 'skipped'); return
    vals = ','.join(f"('{k}','{v}')" for k, v in FIPS.items())
    c.execute(f"""CREATE OR REPLACE TABLE zip_county AS
    WITH f AS (SELECT * FROM (VALUES {vals}) t(fips, st)),
    z AS (SELECT GEOID_ZCTA5_20 AS zip, GEOID_COUNTY_20 AS county_fips, NAMELSAD_COUNTY_20 AS county_name,
            try_cast(AREALAND_PART AS BIGINT) AS land, try_cast(AREAWATER_PART AS BIGINT) AS water
          FROM read_csv('{ZCTA}', delim = '|', header = true, all_varchar = true)
          WHERE GEOID_ZCTA5_20 IS NOT NULL AND GEOID_ZCTA5_20 <> '' AND GEOID_COUNTY_20 IS NOT NULL),
    r AS (SELECT *, row_number() OVER (PARTITION BY zip ORDER BY land DESC NULLS LAST, water DESC NULLS LAST, county_fips) AS rk,
            count(*) OVER (PARTITION BY zip) AS n_counties, land / nullif(sum(land) OVER (PARTITION BY zip), 0) AS land_share FROM z)
    SELECT r.zip, r.county_fips, r.county_name, f.st AS state, r.n_counties::INTEGER AS n_counties, round(r.land_share, 4) AS land_share,
           'census_zcta' AS source
    FROM r LEFT JOIN f ON substr(r.county_fips, 1, 2) = f.fips WHERE rk = 1 ORDER BY zip""")
    n = one(c, "SELECT count(*) FROM zip_county"); stat(c, 'zip_county_census', n); log('zip_county census', n)
    # ZIP codes with no ZCTA (mostly PO box ZIPs): infer the county from the city. A city (name + state) gets the county that holds
    # at least 80 percent of its BMF organizations located in Census-mapped ZIPs; a non-ZCTA ZIP takes its dominant city's county.
    if one(c, "SELECT count(*) FROM information_schema.tables WHERE table_name = 'bmf'"):
        c.execute("""INSERT INTO zip_county
        WITH cc AS (SELECT upper(trim(b.city)) AS city, b.state, z.county_fips, z.county_name, count(*) AS n
                    FROM bmf b JOIN zip_county z ON z.zip = b.zip5 AND z.state = b.state WHERE b.city IS NOT NULL GROUP BY ALL),
        cc2 AS (SELECT *, n / sum(n) OVER (PARTITION BY city, state) AS share, row_number() OVER (PARTITION BY city, state ORDER BY n DESC) AS rk FROM cc),
        city_county AS (SELECT city, state, county_fips, county_name, share FROM cc2 WHERE rk = 1 AND share >= 0.8),
        zc AS (SELECT b.zip5 AS zip, upper(trim(b.city)) AS city, b.state, count(*) AS n FROM bmf b
               WHERE b.zip5 IS NOT NULL AND b.city IS NOT NULL AND b.state IS NOT NULL AND b.zip5 NOT IN (SELECT zip FROM zip_county) GROUP BY ALL),
        zc2 AS (SELECT *, row_number() OVER (PARTITION BY zip ORDER BY n DESC) AS rk FROM zc)
        SELECT zc2.zip, k.county_fips, k.county_name, k.state, 1 AS n_counties, NULL AS land_share, 'city_inferred' AS source
        FROM zc2 JOIN city_county k ON k.city = zc2.city AND k.state = zc2.state WHERE zc2.rk = 1""")
    n = one(c, "SELECT count(*) FROM zip_county"); stat(c, 'zip_county', n); log('zip_county with city-inferred rows', n)

# ------------------------------------------------------------------ completeness against the IRS index files
def step_completeness(c):
    P = PARTS
    c.execute("CREATE OR REPLACE TEMP TABLE parsed AS SELECT DISTINCT object_id FROM read_parquet('%s/returns__*.parquet')" % P)
    c.execute("CREATE OR REPLACE TEMP TABLE parsed990 AS SELECT DISTINCT object_id FROM read_parquet('%s/filers990__*.parquet')" % P)
    c.execute("""CREATE OR REPLACE TABLE qa_completeness (index_year INTEGER, index_rows BIGINT, index_990pf BIGINT, parsed_990pf BIGINT,
                 missing_990pf BIGINT, pct_parsed DOUBLE, index_990 BIGINT, parsed_990_with_schedule_i BIGINT, zips_in_list INTEGER, zips_done INTEGER, xml_members BIGINT, parse_errors BIGINT)""")
    zips = json.load(open(os.path.join(BASE, 'zips.json')))
    for y in range(2018, 2027):
        p = os.path.join(BASE, f'idx_{y}.csv')
        if not os.path.exists(p): continue
        r = c.execute(f"""WITH i AS (SELECT DISTINCT trim(OBJECT_ID) AS object_id, upper(trim(RETURN_TYPE)) AS rt
                              FROM read_csv('{p}', header = true, all_varchar = true, ignore_errors = true, null_padding = true))
            SELECT (SELECT count(*) FROM i), count(*) FILTER (rt = '990PF'), count(*) FILTER (rt = '990PF' AND object_id IN (SELECT object_id FROM parsed)),
                   count(*) FILTER (rt = '990'), count(*) FILTER (rt = '990' AND object_id IN (SELECT object_id FROM parsed990)) FROM i""").fetchone()
        zl = zips.get(str(y), []); done = members = errors = 0
        for z in zl:
            dp = os.path.join(P, z['name'][:-4] + '.done')
            if os.path.exists(dp):
                i = json.load(open(dp)); done += 1; members += i.get('members', 0); errors += i.get('errors', 0)
        c.execute("INSERT INTO qa_completeness VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                  [y, r[0], r[1], r[2], r[1] - r[2], round(100.0 * r[2] / r[1], 3) if r[1] else None, r[3], r[4], len(zl), done, members, errors])
        log('completeness', y, r)
    c.execute("CREATE OR REPLACE TABLE source_zips (index_year INTEGER, zip VARCHAR, url VARCHAR, bytes BIGINT, done BOOLEAN, xml_members BIGINT, parse_errors BIGINT, pf_returns BIGINT, pf_grants BIGINT, pf_officers BIGINT, pf_app_info BIGINT, pc_filers BIGINT, pc_grants BIGINT)")
    for y in sorted(zips):
        for z in zips[y]:
            dp = os.path.join(P, z['name'][:-4] + '.done'); i = json.load(open(dp)) if os.path.exists(dp) else {}
            c.execute("INSERT INTO source_zips VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", [int(y), z['name'], z['url'], z['bytes'], bool(i), i.get('members'), i.get('errors'),
                      i.get('returns'), i.get('grants'), i.get('officers'), i.get('apps'), i.get('filers990'), i.get('grants990')])

# ------------------------------------------------------------------ unified grants, step 1: common shape and normalized geography
def step_g0(c):
    c.execute("SET preserve_insertion_order=false")
    has_zip = one(c, "SELECT count(*) FROM information_schema.tables WHERE table_name = 'zip_county'") > 0
    zjoin = "LEFT JOIN zip_county zc ON zc.zip = u.z5" if has_zip else "LEFT JOIN (SELECT NULL::VARCHAR AS zip, NULL::VARCHAR AS state, NULL::VARCHAR AS county_name WHERE false) zc ON zc.zip = u.z5"
    c.execute(f"""CREATE OR REPLACE TABLE scratch.g0 AS
    WITH u AS (
      SELECT 'private_foundation' AS funder_type, g.funder_ein, g.funder_name, g.funder_city, g.funder_state, g.fiscal_year, g.period_end,
             g.object_id, g.seq, g.recipient_name, g.recipient_key, NULL::VARCHAR AS recipient_ein, g.recipient_city,
             upper(trim(g.recipient_state)) AS st_raw, g.recipient_country,
             (g.recipient_country IS NULL OR upper(g.recipient_country) IN ('US', 'USA')) AS is_us,
             CASE WHEN g.recipient_country IS NULL OR upper(g.recipient_country) IN ('US', 'USA') THEN zip5(g.recipient_zip) END AS z5,
             g.purpose, g.amount, g.foundation_status AS recipient_status,
             greatest(coalesce(r.total_grants_paid, 0), coalesce(r.grants_paid_charitable, 0), coalesce(r.grants_paid_books, 0), coalesce(r.total_expenses, 0)) AS funder_reported_total
      FROM pf_grants g JOIN pf_returns r ON r.object_id = g.object_id WHERE g.kind = 'paid'
      UNION ALL
      SELECT 'public_charity', g.funder_ein, g.funder_name, g.funder_city, g.funder_state, g.fiscal_year, g.period_end,
             g.object_id, g.seq, g.recipient_name, g.recipient_key, g.recipient_ein, g.recipient_city,
             upper(trim(g.recipient_state)), g.recipient_country,
             (g.recipient_country IS NULL OR upper(g.recipient_country) IN ('US', 'USA')),
             CASE WHEN g.recipient_country IS NULL OR upper(g.recipient_country) IN ('US', 'USA') THEN zip5(g.recipient_zip) END,
             g.purpose, g.amount, g.irc_section, greatest(coalesce(r.grants_paid_total, 0), coalesce(r.total_revenue, 0), coalesce(r.total_assets_eoy, 0))
      FROM pc_grants g JOIN pc_filers r ON r.object_id = g.object_id)
    SELECT u.funder_type, u.funder_ein, u.funder_name, u.funder_city, u.funder_state, u.fiscal_year, u.period_end, u.object_id, u.seq,
        u.recipient_name, u.recipient_key, u.recipient_ein, u.recipient_city,
        CASE WHEN u.is_us AND u.st_raw IN {STATES_SQL} THEN u.st_raw WHEN u.is_us AND zc.state IS NOT NULL THEN zc.state END AS recipient_state,
        u.z5 AS recipient_zip,
        CASE WHEN zc.state IS NOT NULL AND (NOT (u.st_raw IN {STATES_SQL}) OR u.st_raw IS NULL OR u.st_raw = zc.state) THEN zc.county_name END AS recipient_county,
        CASE WHEN u.is_us THEN 'US' ELSE upper(u.recipient_country) END AS recipient_country,
        u.recipient_status, u.purpose, u.amount,
        (u.amount < 0 OR u.amount > 2e10 OR (u.amount >= 1e6 AND u.funder_reported_total > 0 AND u.amount > 2 * u.funder_reported_total)) IS TRUE AS amount_flag,
        CASE WHEN u.amount < 0 THEN 'negative' WHEN u.amount > 2e10 THEN 'absurd'
             WHEN u.amount >= 1e6 AND u.funder_reported_total > 0 AND u.amount > 2 * u.funder_reported_total THEN 'exceeds_twice_funder_reported_totals' END AS amount_flag_reason
    FROM u {zjoin}""")
    n = one(c, "SELECT count(*) FROM scratch.g0"); stat(c, 'g0', n); log('g0', n)

# ------------------------------------------------------------------ recipient matching to the BMF
def step_match(c):
    c.execute("SET preserve_insertion_order=false")
    c.execute("CREATE OR REPLACE TEMP TABLE bmf_kz AS SELECT name_key, zip5, any_value(ein) AS ein FROM bmf WHERE name_key IS NOT NULL AND zip5 IS NOT NULL GROUP BY 1, 2 HAVING count(*) = 1")
    c.execute("CREATE OR REPLACE TEMP TABLE bmf_ks AS SELECT name_key, state, any_value(ein) AS ein FROM bmf WHERE name_key IS NOT NULL AND state IS NOT NULL GROUP BY 1, 2 HAVING count(*) = 1")
    c.execute("CREATE OR REPLACE TEMP TABLE bmf_k2 AS SELECT name_key2, state, any_value(ein) AS ein FROM bmf WHERE name_key2 IS NOT NULL AND state IS NOT NULL GROUP BY 1, 2 HAVING count(*) = 1")
    c.execute("""CREATE OR REPLACE TABLE recipient_match AS
    WITH d AS (SELECT recipient_key, recipient_state, recipient_zip, count(*) AS n_grants, sum(amount) AS dollars
               FROM scratch.g0 WHERE recipient_ein IS NULL AND recipient_key IS NOT NULL GROUP BY ALL)
    SELECT d.recipient_key, d.recipient_state, d.recipient_zip, d.n_grants, d.dollars,
        coalesce(kz.ein, ks.ein, k2.ein) AS ein,
        CASE WHEN kz.ein IS NOT NULL THEN 'name_zip' WHEN ks.ein IS NOT NULL THEN 'name_state' WHEN k2.ein IS NOT NULL THEN 'simplified_name_state' END AS method
    FROM d LEFT JOIN bmf_kz kz ON d.recipient_key = kz.name_key AND d.recipient_zip = kz.zip5
           LEFT JOIN bmf_ks ks ON d.recipient_key = ks.name_key AND d.recipient_state = ks.state
           LEFT JOIN bmf_k2 k2 ON name_key2(d.recipient_key) = k2.name_key2 AND d.recipient_state = k2.state""")
    # validation: run the same name matching on Schedule I grants that carry a reported EIN and compare
    c.execute("""CREATE OR REPLACE TABLE qa_match_validation AS
    WITH d AS (SELECT recipient_key, recipient_state, recipient_zip, recipient_ein, count(*) AS n FROM scratch.g0
               WHERE recipient_ein IS NOT NULL AND recipient_key IS NOT NULL GROUP BY ALL),
    m AS (SELECT d.*, coalesce(kz.ein, ks.ein, k2.ein) AS ein,
            CASE WHEN kz.ein IS NOT NULL THEN 'name_zip' WHEN ks.ein IS NOT NULL THEN 'name_state' WHEN k2.ein IS NOT NULL THEN 'simplified_name_state' ELSE 'no_match' END AS method
          FROM d LEFT JOIN bmf_kz kz ON d.recipient_key = kz.name_key AND d.recipient_zip = kz.zip5
                 LEFT JOIN bmf_ks ks ON d.recipient_key = ks.name_key AND d.recipient_state = ks.state
                 LEFT JOIN bmf_k2 k2 ON name_key2(d.recipient_key) = k2.name_key2 AND d.recipient_state = k2.state)
    SELECT method, sum(n)::BIGINT AS grants_tested, sum(n) FILTER (ein = recipient_ein)::BIGINT AS matched_ein_equals_reported,
           sum(n) FILTER (ein IS NOT NULL AND ein <> recipient_ein)::BIGINT AS matched_ein_differs,
           sum(n) FILTER (recipient_ein IN (SELECT ein FROM bmf))::BIGINT AS reported_ein_in_bmf
    FROM m GROUP BY 1 ORDER BY 1""")
    log('match validation', c.execute("SELECT * FROM qa_match_validation").fetchall())
    r = c.execute("SELECT count(*), count(ein), sum(n_grants), sum(n_grants) FILTER (ein IS NOT NULL) FROM recipient_match").fetchone()
    stat(c, 'recipient_match_keys', r[0]); stat(c, 'recipient_match_keys_matched', r[1]); log('recipient_match keys, matched, grants, grants matched', r)

# ------------------------------------------------------------------ tags
def tag_sql(txt='txt', ntee='recipient_ntee'):
    tax = json.load(open(os.path.join(BASE, 'taxonomy.json')))['tags']
    def q(s): return s.replace("'", "''")
    parts = []
    for tag, d in tax.items():
        kws = [k for k in d.get('keywords', []) if k]
        ex = [k for k in d.get('exclude', []) if k]
        base = f"regexp_replace({txt}, '\\b(?:{q('|'.join(ex))})\\b', ' ', 'g')" if ex else txt
        conds = []
        if kws: conds.append(f"regexp_matches({base}, '\\b(?:{q('|'.join(kws))})\\b')")
        pre = d.get('ntee_prefixes', [])
        if pre: conds.append(f"regexp_matches({ntee}, '^(?:{q('|'.join(pre))})')")
        parts.append(f"CASE WHEN {' OR '.join('coalesce(' + x + ', false)' for x in conds)} THEN '{q(tag)}' END")
    return "list_filter([" + ",\n".join(parts) + "], lambda x: x IS NOT NULL)"

GA_COLS = """funder_type, funder_ein, funder_name, funder_city, funder_state, fiscal_year, recipient_name, recipient_key, recipient_ein,
        recipient_ein_matched, recipient_match_method, recipient_ein_best, recipient_city, recipient_state, recipient_zip, recipient_county,
        recipient_country, recipient_ntee, recipient_status, purpose, amount, amount_flag, amount_flag_reason, period_end, object_id, seq"""

def step_grants_all(c, retag=False):
    c.execute("SET preserve_insertion_order=true")
    if retag:
        src = f"(SELECT {GA_COLS} FROM grants_all)"
    else:
        src = """(SELECT g.funder_type, g.funder_ein, g.funder_name, g.funder_city, g.funder_state, g.fiscal_year, g.recipient_name, g.recipient_key,
            g.recipient_ein, m.ein AS recipient_ein_matched, m.method AS recipient_match_method, coalesce(g.recipient_ein, m.ein) AS recipient_ein_best,
            g.recipient_city, g.recipient_state, g.recipient_zip, g.recipient_county, g.recipient_country,
            b.ntee_cd AS recipient_ntee, g.recipient_status, g.purpose, g.amount, g.amount_flag, g.amount_flag_reason, g.period_end, g.object_id, g.seq
          FROM scratch.g0 g
          LEFT JOIN recipient_match m ON g.recipient_ein IS NULL AND m.ein IS NOT NULL AND g.recipient_key = m.recipient_key
               AND g.recipient_state IS NOT DISTINCT FROM m.recipient_state AND g.recipient_zip IS NOT DISTINCT FROM m.recipient_zip
          LEFT JOIN bmf b ON b.ein = coalesce(g.recipient_ein, m.ein))"""
    c.execute("DROP TABLE IF EXISTS grants_all_new")
    c.execute(f"""CREATE TABLE grants_all_new AS
    WITH s AS (SELECT *, upper(coalesce(purpose, '') || ' | ' || coalesce(recipient_name, '')) AS txt FROM {src})
    SELECT {GA_COLS}, {tag_sql()} AS tags
    FROM s ORDER BY {GRANT_SORT}""")
    c.execute("DROP INDEX IF EXISTS idx_ga_funder"); c.execute("DROP INDEX IF EXISTS idx_ga_recipient_ein"); c.execute("DROP INDEX IF EXISTS idx_ga_recipient_key")
    c.execute("DROP TABLE IF EXISTS grants_all"); c.execute("DROP VIEW IF EXISTS grants_all")
    c.execute("ALTER TABLE grants_all_new RENAME TO grants_all")
    n = one(c, "SELECT count(*) FROM grants_all"); stat(c, 'grants_all', n); log('grants_all', n)
    # tag summary table
    tax = json.load(open(os.path.join(BASE, 'taxonomy.json')))['tags']
    c.execute("CREATE OR REPLACE TEMP TABLE tagdef (tag VARCHAR, label VARCHAR, keywords VARCHAR, exclude VARCHAR, ntee_prefixes VARCHAR)")
    for t, d in tax.items():
        c.execute("INSERT INTO tagdef VALUES (?,?,?,?,?)", [t, d.get('label'), ' | '.join(d.get('keywords', [])), ' | '.join(d.get('exclude', [])), ', '.join(d.get('ntee_prefixes', []))])
    c.execute("""CREATE OR REPLACE TABLE subject_tags AS
    WITH u AS (SELECT unnest(tags) AS tag, amount, amount_flag FROM grants_all),
    a AS (SELECT tag, count(*) AS n_grants, sum(amount) FILTER (amount > 0 AND NOT amount_flag) AS dollars FROM u GROUP BY 1)
    SELECT d.tag, d.label, coalesce(a.n_grants, 0) AS n_grants, a.dollars, d.keywords, d.exclude, d.ntee_prefixes FROM tagdef d LEFT JOIN a USING (tag) ORDER BY n_grants DESC""")
    r = c.execute("SELECT count(*) FILTER (len(tags) > 0), count(*) FROM grants_all").fetchone()
    stat(c, 'grants_tagged', r[0]); log('tagged', r)

def step_recipients(c):
    c.execute("SET preserve_insertion_order=true")
    c.execute("""CREATE OR REPLACE TABLE recipients AS
    SELECT recipient_key, recipient_state, mode(recipient_name) AS recipient_name, mode(recipient_city) AS recipient_city,
        mode(recipient_ein_best) AS recipient_ein_best, mode(recipient_ntee) AS recipient_ntee,
        count(*) AS n_grants, count(DISTINCT funder_ein) AS n_funders, sum(amount) FILTER (amount > 0 AND NOT amount_flag) AS dollars,
        min(fiscal_year) AS first_fiscal_year, max(fiscal_year) AS last_fiscal_year
    FROM grants_all WHERE recipient_key IS NOT NULL GROUP BY 1, 2 ORDER BY recipient_key, recipient_state""")
    n = one(c, "SELECT count(*) FROM recipients"); stat(c, 'recipients', n); log('recipients', n)

# ------------------------------------------------------------------ profiles
def last_six(c, table):
    # latest fiscal year holding at least a quarter as many returns as the fullest year (skips the thin, still-arriving year)
    mx = one(c, f"SELECT max(fiscal_year) FROM (SELECT fiscal_year, count(*) n, max(count(*)) OVER () mx FROM {table} GROUP BY 1) WHERE n >= 0.25 * mx")
    return list(range(mx - 5, mx + 1))

def step_foundations(c):
    c.execute("SET preserve_insertion_order=true")
    yrs = last_six(c, 'pf_returns'); stat(c, 'last_six_fiscal_years', ','.join(map(str, yrs)))
    fycols = ",\n".join(f"sum(giving) FILTER (fiscal_year = {y}) AS giving_fy{y}, (sum(n_grants) FILTER (fiscal_year = {y}))::BIGINT AS n_grants_fy{y}" for y in yrs)
    fysel = ", ".join(f"a.giving_fy{y}, a.n_grants_fy{y}" for y in yrs)
    fynull = ", ".join(f"NULL::DOUBLE AS giving_fy{y}, NULL::BIGINT AS n_grants_fy{y}" for y in yrs)
    c.execute(f"""CREATE OR REPLACE TABLE foundations AS
    WITH latest AS (SELECT * FROM pf_returns QUALIFY row_number() OVER (PARTITION BY ein ORDER BY period_end DESC, return_ts DESC NULLS LAST, object_id DESC) = 1),
    fy AS (SELECT ein, fiscal_year, sum(giving) AS giving, sum(n_grants_paid)::BIGINT AS n_grants FROM pf_returns GROUP BY 1, 2),
    a AS (SELECT ein, min(fiscal_year) AS first_fiscal_year, max(fiscal_year) AS last_fiscal_year, sum(giving) AS total_giving_all_years,
            sum(n_grants)::BIGINT AS n_grants_all_years,
            {fycols},
            json_group_object(fiscal_year::VARCHAR, json_object('giving', giving, 'grants', n_grants))::VARCHAR AS giving_by_year
          FROM fy GROUP BY 1),
    nr AS (SELECT ein, count(*) AS n_returns, max(only_preselected) AS ever_only_preselected, min(only_preselected) AS always_only_preselected FROM pf_returns GROUP BY 1),
    gs AS (SELECT g.funder_ein AS ein, median(g.amount) AS median_grant, avg(g.amount) AS avg_grant, max(g.amount) AS max_grant, count(*) AS n_grants_positive,
            sum(g.amount) AS listed_dollars, sum(g.amount) FILTER (g.recipient_state = l.state) / nullif(sum(g.amount), 0) AS home_state_share
          FROM grants_all g JOIN latest l ON l.ein = g.funder_ein
          WHERE g.funder_type = 'private_foundation' AND g.amount > 0 AND NOT g.amount_flag GROUP BY 1),
    st AS (SELECT funder_ein AS ein, recipient_state AS s, sum(amount) AS d FROM grants_all
           WHERE funder_type = 'private_foundation' AND amount > 0 AND NOT amount_flag GROUP BY 1, 2),
    st2 AS (SELECT ein, s, d, d / sum(d) OVER (PARTITION BY ein) AS share, row_number() OVER (PARTITION BY ein ORDER BY (s IS NULL), d DESC) AS rk FROM st),
    tops AS (SELECT ein, string_agg(s || ' ' || round(100 * share)::INTEGER || '%', '; ' ORDER BY rk) AS top_recipient_states,
               max(CASE WHEN rk = 1 THEN s END) AS top_recipient_state FROM st2 WHERE rk <= 5 AND s IS NOT NULL GROUP BY 1),
    ap AS (SELECT * FROM pf_app_info
           WHERE coalesce(contact_name, addr1, phone, email, form_and_materials, deadlines, restrictions) IS NOT NULL
           QUALIFY row_number() OVER (PARTITION BY funder_ein ORDER BY period_end DESC, seq) = 1),
    ofc AS (SELECT o.funder_ein AS ein, string_agg(o.name || coalesce(', ' || o.title, ''), '; ' ORDER BY o.seq) AS officers, count(*) AS n_officers
            FROM pf_officers o JOIN latest l ON l.object_id = o.object_id WHERE o.name IS NOT NULL AND o.seq <= 60 GROUP BY 1)
    SELECT l.ein, l.name, l.addr1 AS address, l.city, l.state, l.zip, zc.county_name AS county, l.phone, l.website,
        nullif(trim(coalesce(l.signer_name, '') || coalesce(', ' || l.signer_title, '')), '') AS signer,
        l.fiscal_year AS fiscal_year_latest, l.period_end AS period_end_latest, l.fmv_assets_eoy, l.total_assets_eoy, l.total_revenue,
        l.contributions_received, l.total_grants_paid, l.giving AS giving_latest, (l.only_preselected = 1) AS only_preselected,
        (l.operating_foundation = 1) AS operating_foundation, (l.final_return = 1) AS final_return_latest,
        a.first_fiscal_year, a.last_fiscal_year, nr.n_returns, a.total_giving_all_years, a.n_grants_all_years,
        {fysel}, a.giving_by_year,
        gs.median_grant, gs.avg_grant, gs.max_grant, tops.top_recipient_states, tops.top_recipient_state, gs.home_state_share,
        (ap.funder_ein IS NOT NULL) AS ever_listed_application_info, ap.fiscal_year AS app_fiscal_year, ap.contact_name AS app_contact_name,
        nullif(concat_ws(', ', ap.addr1, ap.city, ap.state, ap.zip), '') AS app_address, ap.phone AS app_phone, ap.email AS app_email,
        ap.form_and_materials AS app_form_and_materials, ap.deadlines AS app_deadlines, ap.restrictions AS app_restrictions,
        ofc.officers, l.direct_charitable_activities,
        b.ntee_cd, (b.ein IS NOT NULL) AS in_bmf, b.foundation AS bmf_foundation_code, b.subsection AS bmf_subsection, b.ruling AS bmf_ruling,
        b.asset_amt AS bmf_asset_amt, b.income_amt AS bmf_income_amt, b.tax_period AS bmf_tax_period,
        false AS efile_missing, l.flag_amounts_suspect, l.object_id AS latest_object_id
    FROM latest l LEFT JOIN a USING (ein) LEFT JOIN nr USING (ein) LEFT JOIN gs USING (ein) LEFT JOIN tops USING (ein)
        LEFT JOIN ap ON ap.funder_ein = l.ein LEFT JOIN ofc USING (ein) LEFT JOIN bmf b ON b.ein = l.ein
        LEFT JOIN zip_county zc ON zc.zip = l.zip5 AND zc.state = l.state
    UNION ALL BY NAME
    SELECT b.ein, b.name, b.street AS address, b.city, b.state, b.zip, zc.county_name AS county, NULL AS phone, NULL AS website, NULL AS signer,
        NULL::INTEGER AS fiscal_year_latest, NULL::DATE AS period_end_latest, NULL::DOUBLE AS fmv_assets_eoy, NULL::DOUBLE AS total_assets_eoy,
        NULL::DOUBLE AS total_revenue, NULL::DOUBLE AS contributions_received, NULL::DOUBLE AS total_grants_paid, NULL::DOUBLE AS giving_latest,
        NULL::BOOLEAN AS only_preselected, (b.foundation IN ('02', '03')) AS operating_foundation, NULL::BOOLEAN AS final_return_latest,
        NULL::INTEGER AS first_fiscal_year, NULL::INTEGER AS last_fiscal_year, 0 AS n_returns, NULL::DOUBLE AS total_giving_all_years, NULL::BIGINT AS n_grants_all_years,
        {fynull}, NULL AS giving_by_year,
        NULL::DOUBLE AS median_grant, NULL::DOUBLE AS avg_grant, NULL::DOUBLE AS max_grant, NULL AS top_recipient_states, NULL AS top_recipient_state, NULL::DOUBLE AS home_state_share,
        false AS ever_listed_application_info, NULL::INTEGER AS app_fiscal_year, NULL AS app_contact_name, NULL AS app_address, NULL AS app_phone, NULL AS app_email,
        NULL AS app_form_and_materials, NULL AS app_deadlines, NULL AS app_restrictions, NULL AS officers, NULL AS direct_charitable_activities,
        b.ntee_cd, true AS in_bmf, b.foundation AS bmf_foundation_code, b.subsection AS bmf_subsection, b.ruling AS bmf_ruling,
        b.asset_amt AS bmf_asset_amt, b.income_amt AS bmf_income_amt, b.tax_period AS bmf_tax_period,
        true AS efile_missing, false AS flag_amounts_suspect, NULL AS latest_object_id
    FROM bmf b LEFT JOIN zip_county zc ON zc.zip = b.zip5 AND zc.state = b.state
    WHERE b.foundation IN ('02', '03', '04') AND b.ein NOT IN (SELECT ein FROM pf_returns)
    ORDER BY state NULLS LAST, name""")
    r = c.execute("SELECT count(*), count(*) FILTER (efile_missing), count(DISTINCT ein) FROM foundations").fetchone()
    stat(c, 'foundations', r[0]); stat(c, 'foundations_efile_missing', r[1]); log('foundations total, efile_missing, distinct ein', r)

def step_pc_funders(c):
    c.execute("SET preserve_insertion_order=true")
    yrs = last_six(c, 'pc_filers')
    fycols = ",\n".join(f"sum(listed) FILTER (fiscal_year = {y}) AS grants_listed_fy{y}, (sum(n_grants) FILTER (fiscal_year = {y}))::BIGINT AS n_grants_fy{y}" for y in yrs)
    fysel = ", ".join(f"a.grants_listed_fy{y}, a.n_grants_fy{y}" for y in yrs)
    c.execute(f"""CREATE OR REPLACE TABLE pc_funders AS
    WITH latest AS (SELECT * FROM pc_filers QUALIFY row_number() OVER (PARTITION BY ein ORDER BY period_end DESC, object_id DESC) = 1),
    fy AS (SELECT f.ein, f.fiscal_year, coalesce(g.listed, 0) AS listed, coalesce(g.n_grants, 0)::BIGINT AS n_grants, f.paid_total
           FROM (SELECT ein, fiscal_year, sum(grants_paid_total) AS paid_total FROM pc_filers GROUP BY 1, 2) f
           LEFT JOIN (SELECT funder_ein, fiscal_year, sum(amount) AS listed, count(*) AS n_grants FROM grants_all
                      WHERE funder_type = 'public_charity' AND NOT amount_flag GROUP BY 1, 2) g ON g.funder_ein = f.ein AND g.fiscal_year = f.fiscal_year),
    a AS (SELECT ein, min(fiscal_year) AS first_fiscal_year, max(fiscal_year) AS last_fiscal_year, count(*) AS n_fiscal_years,
            sum(listed) AS grants_listed_all_years, sum(n_grants)::BIGINT AS n_grants_all_years,
            {fycols},
            json_group_object(fiscal_year::VARCHAR, json_object('grants_listed', listed, 'grants', n_grants, 'grants_paid_total', paid_total))::VARCHAR AS giving_by_year
          FROM fy GROUP BY 1),
    nr AS (SELECT ein, count(*) AS n_returns FROM pc_filers GROUP BY 1),
    gs AS (SELECT g.funder_ein AS ein, median(g.amount) AS median_grant, avg(g.amount) AS avg_grant, max(g.amount) AS max_grant,
            sum(g.amount) FILTER (g.recipient_state = l.state) / nullif(sum(g.amount), 0) AS home_state_share
          FROM grants_all g JOIN latest l ON l.ein = g.funder_ein
          WHERE g.funder_type = 'public_charity' AND g.amount > 0 AND NOT g.amount_flag GROUP BY 1),
    st AS (SELECT funder_ein AS ein, recipient_state AS s, sum(amount) AS d FROM grants_all
           WHERE funder_type = 'public_charity' AND amount > 0 AND NOT amount_flag GROUP BY 1, 2),
    st2 AS (SELECT ein, s, d, d / sum(d) OVER (PARTITION BY ein) AS share, row_number() OVER (PARTITION BY ein ORDER BY (s IS NULL), d DESC) AS rk FROM st),
    tops AS (SELECT ein, string_agg(s || ' ' || round(100 * share)::INTEGER || '%', '; ' ORDER BY rk) AS top_recipient_states,
               max(CASE WHEN rk = 1 THEN s END) AS top_recipient_state FROM st2 WHERE rk <= 5 AND s IS NOT NULL GROUP BY 1)
    SELECT l.ein, l.name, l.addr1 AS address, l.city, l.state, l.zip, zc.county_name AS county, l.phone, l.website, l.mission, l.principal_officer,
        l.fiscal_year AS fiscal_year_latest, l.period_end AS period_end_latest, l.total_assets_eoy, l.total_revenue,
        l.grants_paid_total AS total_grants_paid, l.sum_grants_listed AS grants_listed_latest, l.n_grants_listed AS n_grants_latest,
        a.first_fiscal_year, a.last_fiscal_year, nr.n_returns, a.grants_listed_all_years, a.n_grants_all_years,
        {fysel}, a.giving_by_year, gs.median_grant, gs.avg_grant, gs.max_grant, tops.top_recipient_states, tops.top_recipient_state, gs.home_state_share,
        b.ntee_cd, (b.ein IS NOT NULL) AS in_bmf, b.foundation AS bmf_foundation_code, b.subsection AS bmf_subsection, b.ruling AS bmf_ruling,
        l.object_id AS latest_object_id
    FROM latest l LEFT JOIN a USING (ein) LEFT JOIN nr USING (ein) LEFT JOIN gs USING (ein) LEFT JOIN tops USING (ein)
        LEFT JOIN bmf b ON b.ein = l.ein LEFT JOIN zip_county zc ON zc.zip = l.zip5 AND zc.state = l.state
    ORDER BY state NULLS LAST, name""")
    n = one(c, "SELECT count(*) FROM pc_funders"); stat(c, 'pc_funders', n); log('pc_funders', n)

def step_indexes(c):
    for name, col in (('idx_ga_funder', 'funder_ein'), ('idx_ga_recipient_ein', 'recipient_ein_best'), ('idx_ga_recipient_key', 'recipient_key')):
        t = time.time()
        try:
            c.execute(f"DROP INDEX IF EXISTS {name}"); c.execute(f"CREATE INDEX {name} ON grants_all ({col})")
            log('index', name, round(time.time() - t), 's'); stat(c, name, 'created')
        except Exception as e:
            log('index failed', name, repr(e)[:200]); stat(c, name, 'failed: ' + repr(e)[:150])
    for name, tbl, col in (('idx_f_ein', 'foundations', 'ein'), ('idx_pcf_ein', 'pc_funders', 'ein'), ('idx_pfr_ein', 'pf_returns', 'ein'),
                           ('idx_pfo_ein', 'pf_officers', 'funder_ein'), ('idx_bmf_ein', 'bmf', 'ein'), ('idx_pfg_ein', 'pf_grants', 'funder_ein'),
                           ('idx_pcg_ein', 'pc_grants', 'funder_ein'), ('idx_rcp_key', 'recipients', 'recipient_key')):
        try:
            c.execute(f"DROP INDEX IF EXISTS {name}"); c.execute(f"CREATE INDEX {name} ON {tbl} ({col})")
        except Exception as e:
            log('index failed', name, repr(e)[:200])
    log('indexes done')

CAVEATS = [
 "Source is IRS e-filed returns only (Form 990 series XML zips for 2018 to 2026). Paper-filed returns are not included. Private foundations were required to e-file for tax years beginning after July 1, 2019, so fiscal years before 2020 are incomplete.",
 "Data lags: a return is usually filed 5 to 11 months after the fiscal year ends and released by the IRS some months later. The most recent fiscal year is partial.",
 "Foundations found only in the IRS Business Master File (efile_missing = true) have no return data here: no grants, officers or application details.",
 "Grant purpose text is whatever the filer typed. Much of it is generic (for example GENERAL SUPPORT). Subject tags are keyword matches and will contain false positives and misses.",
 "Form 990-PF grant lists do not carry recipient EINs. recipient_ein_matched is a name and location match to the Business Master File that accepts only unique matches; it can still be wrong, and most unmatched recipients are simply not matched, not absent from the IRS list.",
 "Public charity grants (Schedule I) include donor-advised fund sponsors and community foundations. Those grants show the sponsor as funder; the advising donor is not disclosed.",
 "Schedule I lists only grants to domestic organizations and governments above 5,000 dollars per recipient. Grants to individuals and foreign grants are not in grants_all.",
 "Some foundations attach their grant list as a PDF or statement instead of structured data; those returns show a reported total with few or no listed grants.",
 "Amounts are as reported. Rows with negative or implausible amounts are kept and marked amount_flag = true; profile aggregates exclude them.",
 "ZIP to county uses Census 2020 ZCTA relationships (largest land overlap). ZIP codes with no ZCTA (mostly PO boxes) take the dominant county of their city as seen in the Business Master File when at least 80 percent of that city's organizations sit in one county; otherwise the county is blank.",
 "Transfers between related foundations (for example Gates Foundation Trust to Gates Foundation) and in-kind patient assistance programs are reported as grants and are included as reported, so national dollar totals double count some money.",
 "Some returns list a placeholder instead of real recipients (SEE ATTACHED, VARIOUS, SEE STATEMENT). Those rows are kept as reported and cannot be attributed to a recipient.",
 "Recipient state comes from the address on the return. When the state field was blank or invalid it was filled from the ZIP code; foreign and unknown locations are null (XX in the exports).",
 "The website field on Form 990-PF is often N/A or a third-party site (GuideStar, Candid, a state charity registry). Those values are nulled; the raw text stays in pf_returns.website_raw.",
 "One return is kept per EIN and period end (latest filing timestamp wins), so amended returns replace originals.",
]

def step_meta(c):
    c.execute("CREATE OR REPLACE TABLE meta (key VARCHAR, value VARCHAR)")
    def put(k, v): c.execute("INSERT INTO meta VALUES (?, ?)", [k, v if isinstance(v, str) else json.dumps(v)])
    put('build_date', datetime.date.today().isoformat())
    put('build_timestamp_utc', datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'))
    put('source_efile_page', 'https://www.irs.gov/charities-non-profits/form-990-series-downloads')
    put('source_efile_root', 'https://apps.irs.gov/pub/epostcard/990/xml/<year>/<zip>')
    put('source_bmf', 'https://www.irs.gov/pub/irs-soi/eo1.csv, eo2.csv, eo3.csv, eo4.csv, eo_pr.csv, eo_xx.csv')
    put('source_zip_county', 'https://www2.census.gov/geo/docs/maps-data/data/rel2020/zcta520/tab20_zcta520_county20_natl.txt')
    for k, v in c.execute("SELECT key, value FROM build_stats").fetchall(): put('stat_' + k, v)
    put('zips', [dict(zip(('index_year', 'zip', 'bytes', 'done', 'xml_members', 'pf_returns', 'pf_grants', 'pc_filers', 'pc_grants'), r)) for r in
                 c.execute("SELECT index_year, zip, bytes, done, xml_members, pf_returns, pf_grants, pc_filers, pc_grants FROM source_zips ORDER BY 1, 2").fetchall()])
    rc = {}
    for (t,) in c.execute("SELECT table_name FROM information_schema.tables WHERE table_schema = 'main' AND table_type = 'BASE TABLE' ORDER BY 1").fetchall():
        rc[t] = one(c, f'SELECT count(*) FROM "{t}"')
    put('row_counts', rc); put('caveats', CAVEATS)
    put('name_key_rule', "upper case, accents stripped, & becomes AND, apostrophes and periods deleted, other punctuation becomes a space, spaces collapsed, leading THE removed")
    log('meta', rc)

def step_cleanup(c):
    c.execute("DROP TABLE IF EXISTS grants_all_new"); c.execute("DROP TABLE IF EXISTS scratch.g0"); c.execute("CHECKPOINT")
    c.execute("DETACH scratch")
    for f in (SCRATCH, SCRATCH + '.wal'):
        if os.path.isfile(f): os.remove(f)

STEPS = [('macros', step_macros), ('pf', step_pf), ('pc', step_pc), ('bmf', step_bmf), ('zip', step_zip), ('completeness', step_completeness),
         ('g0', step_g0), ('match', step_match), ('grants_all', step_grants_all), ('recipients', step_recipients),
         ('foundations', step_foundations), ('pc_funders', step_pc_funders), ('indexes', step_indexes), ('cleanup', step_cleanup), ('meta', step_meta)]

def main():
    want = sys.argv[1:]
    c = connect()
    if want == ['tags']:
        step_macros(c); step_grants_all(c, retag=True); step_recipients(c); step_indexes(c); step_cleanup(c); step_meta(c)
    else:
        for name, fn in STEPS:
            if want and name not in want: continue
            log('== step', name); fn(c); c.execute("CHECKPOINT")
    c.close(); log('done')

if __name__ == '__main__':
    main()
