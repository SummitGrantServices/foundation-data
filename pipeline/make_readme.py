#!/usr/bin/env python3
"""Writes README.md from the live database so the schema section always matches the tables. Usage: python3 make_readme.py"""
import os, re, json, duckdb

BASE = os.path.dirname(os.path.abspath(__file__))
DB = os.environ.get('FDB_DB', os.path.join(BASE, 'db', 'fdb.duckdb'))
OUT = os.environ.get('FDB_README', os.path.join(BASE, 'README.md'))
c = duckdb.connect(DB, read_only=True)
meta = dict(c.execute("SELECT key, value FROM meta").fetchall())
rc = json.loads(meta['row_counts'])
def one(q): return c.execute(q).fetchone()[0]
def n(v): return f'{v:,.0f}'

TABLES = {
 'foundations': 'One row per private foundation EIN: the profile table. Built from the latest e-filed 990-PF plus aggregates over every return on file, plus every Business Master File private foundation that has no e-filed return here (efile_missing = true).',
 'pc_funders': 'One row per public charity that lists grants on Form 990 Schedule I (community foundations, donor-advised fund sponsors, United Ways, hospitals and universities that regrant, corporate-style public charities).',
 'grants_all': 'Every grant in one shape: 990-PF grants paid during the year plus Schedule I grants. Sorted by recipient_state, then recipient_key. This is the table to search.',
 'pf_returns': 'One row per private foundation per tax period (Form 990-PF). Amended returns replace originals: latest filing timestamp wins.',
 'pf_grants': 'Form 990-PF grant lines for the kept returns: grants paid during the year (kind = paid) and grants approved for future payment (kind = future).',
 'pf_officers': 'Officers, directors, trustees and key employees listed on each kept 990-PF.',
 'pf_app_info': 'Application information from Form 990-PF Part XIV line 2 (who to write to, form, deadlines, restrictions) for each kept return that filled it in.',
 'pc_filers': 'One row per public charity per tax period where the Form 990 has a Schedule I recipient table. Highest object_id wins per EIN and period end.',
 'pc_grants': 'Schedule I recipient table lines for the kept public charity returns.',
 'bmf': 'IRS Exempt Organizations Business Master File extract (all regions): the list of recognized exempt organizations, with NTEE codes and foundation codes.',
 'zip_county': 'ZIP code to county. Census 2020 ZCTA to county relationship file, largest land-area overlap wins, plus city-inferred rows for ZIP codes that have no ZCTA.',
 'recipients': 'One row per recipient_key and state: a quick index of who receives grants, used for name searches.',
 'recipient_match': 'Audit table for recipient matching: each distinct recipient name key, state and ZIP that had no reported EIN, with the Business Master File EIN it matched (if any) and the method.',
 'subject_tags': 'The subject taxonomy with counts: one row per tag.',
 'meta': 'Build facts as key and value: build date, sources, zip list, row counts, caveats.',
 'build_stats': 'Raw counters recorded during consolidation (rows before and after dedupe, and similar).',
 'qa_completeness': 'Per IRS index year: 990PF rows in the index against 990-PF returns parsed.',
 'qa_match_validation': 'Accuracy test of the name matcher on Schedule I grants that carry a reported EIN.',
 'source_zips': 'Every IRS zip file processed, with counts.',
}

COMMON = {
 'ein': 'Employer Identification Number, nine digits, text (leading zeros kept).',
 'object_id': 'IRS e-file object ID of the return (18 digits). The source XML is <object_id>_public.xml inside the zip named in source_zip.',
 'fiscal_year': 'Calendar year in which the tax period ends (year of period_end).',
 'period_end': 'Last day of the tax period.',
 'period_begin': 'First day of the tax period.',
 'tax_year': 'Tax year printed on the form (the year the period begins in).',
 'source_zip': 'IRS zip file the return was read from.',
 'index_year': 'IRS index year (the year the IRS processed the filing), taken from the zip name.',
 'funder_ein': 'EIN of the grantmaker.', 'funder_name': 'Name of the grantmaker as written on that return.',
 'funder_city': 'City of the grantmaker on that return.', 'funder_state': 'State of the grantmaker on that return.',
 'recipient_name': 'Recipient name as typed on the return.',
 'recipient_key': 'Normalized recipient name for grouping and matching: upper case, accents stripped, & becomes AND, apostrophes and periods deleted, other punctuation turned into spaces, spaces collapsed, leading THE removed. The SQL macro name_key(text) applies the same rule.',
 'purpose': 'Purpose of the grant as typed on the return.',
 'seq': 'Line number of the row within its return (order of appearance).',
 'website_raw': 'Website field exactly as filed, before cleaning.',
 'zip5': 'First five digits of the ZIP code.',
 'ntee_cd': 'NTEE code from the Business Master File (letter = major group, for example B education, P human services). Null when the IRS has not assigned one or the EIN is not in the BMF.',
 'in_bmf': 'True when the EIN appears in the Business Master File extract.',
 'bmf_foundation_code': 'BMF FOUNDATION code: 02 exempt operating foundation, 03 private operating foundation, 04 private non-operating foundation, 00 not coded, 10 to 17 public charity classes.',
 'bmf_subsection': 'BMF SUBSECTION code (03 means 501(c)(3)).', 'bmf_ruling': 'BMF RULING: year and month of the IRS exemption ruling (YYYYMM).',
 'median_grant': 'Median grant amount across all years on file (grants above zero, flagged amounts excluded).',
 'avg_grant': 'Average grant amount across all years on file (grants above zero, flagged amounts excluded).',
 'max_grant': 'Largest single grant on file (flagged amounts excluded).',
 'top_recipient_states': 'Top five recipient states by dollars across all years, with each state share of all listed dollars, for example "TX 62%; OK 10%".',
 'top_recipient_state': 'The single largest recipient state by dollars.',
 'home_state_share': 'Share (0 to 1) of listed grant dollars that went to recipients in the funder home state.',
 'giving_by_year': 'JSON text keyed by fiscal year with the dollars and number of grants for every year on file.',
 'first_fiscal_year': 'Earliest fiscal year on file.', 'last_fiscal_year': 'Latest fiscal year on file.',
 'n_returns': 'Number of returns on file for this EIN (0 for efile_missing rows).',
 'latest_object_id': 'object_id of the latest return, for tracing back to the source.',
 'fiscal_year_latest': 'Fiscal year of the latest return on file.', 'period_end_latest': 'Period end date of the latest return on file.',
 'county': 'County of the mailing address, from zip_county.', 'phone': 'Phone number on the return.',
 'website': 'Website from the return, lower-cased; null when the filer wrote N/A, NONE, 0 or a third-party site (GuideStar, Candid, a state registry). Nothing is added, so it may lack http.',
 'key': 'Name of the fact.', 'value': 'Value as text (some are JSON).',
}

COLS = {
 'foundations': {
  'name': 'Foundation name on the latest return (BMF name for efile_missing rows).', 'address': 'Street or PO box.', 'city': 'City.', 'state': 'State (two letters; foreign filers show a province or country here).', 'zip': 'ZIP code as filed.',
  'signer': 'Officer who signed the latest return: name, title.',
  'fmv_assets_eoy': 'Fair market value of all assets at end of year (990-PF page 1, item I), latest return.',
  'total_assets_eoy': 'Total assets at end of year, book value (Part II line 16), latest return.',
  'total_revenue': 'Total revenue per books (Part I line 12 column a), latest return.',
  'contributions_received': 'Contributions, gifts and grants received (Part I line 1), latest return. Shows whether the foundation is still being funded.',
  'total_grants_paid': 'Total grants and contributions paid during the year as reported in Part XIV line 3a, latest return.',
  'giving_latest': 'Giving on the latest return: Part XIV line 3a total; when that is blank or zero, Part I line 25 (charitable column, then books column).',
  'only_preselected': 'True when the latest return checks the box "the foundation only makes contributions to preselected charitable organizations and does not accept unsolicited requests for funds".',
  'operating_foundation': 'True when the latest return claims private operating foundation status (for efile_missing rows: BMF foundation code 02 or 03).',
  'final_return_latest': 'True when the latest return is marked as a final return (the foundation is closing or has closed).',
  'total_giving_all_years': 'Sum of giving across every return on file.', 'n_grants_all_years': 'Number of grant lines (with an amount) across every return on file.',
  'ever_listed_application_info': 'True when any return on file filled in the application information block.',
  'app_fiscal_year': 'Fiscal year of the return the application details below come from (the most recent one that has them).',
  'app_contact_name': 'Who applications should be addressed to.', 'app_address': 'Mailing address for applications.', 'app_phone': 'Phone for application questions.', 'app_email': 'Email for applications, as typed.',
  'app_form_and_materials': 'The form in which applications should be submitted and what to include.', 'app_deadlines': 'Submission deadlines.',
  'app_restrictions': 'Restrictions and limitations on awards (geography, fields, kinds of institutions).',
  'officers': 'Officers, directors and trustees on the latest return as one text: "name, title; name, title; ..." (first 60).',
  'direct_charitable_activities': 'Summary of direct charitable activities from the latest return (Part VIII-A), when filled in.',
  'bmf_asset_amt': 'Assets per the Business Master File.', 'bmf_income_amt': 'Income per the Business Master File.', 'bmf_tax_period': 'Tax period (YYYYMM) the BMF amounts refer to.',
  'efile_missing': 'True for foundations listed in the BMF (foundation code 02, 03 or 04) that have no e-filed 990-PF in this database. They have no grants, officers or application details here.',
  'flag_amounts_suspect': 'True when the latest return has negative or impossible asset or giving figures. Treat its numbers with care.',
 },
 'pc_funders': {
  'name': 'Organization name on the latest return.', 'address': 'Street or PO box.', 'city': 'City.', 'state': 'State.', 'zip': 'ZIP code as filed.',
  'mission': 'Mission or most significant activities (Form 990 Part I line 1), latest return.', 'principal_officer': 'Principal officer named on the latest return.',
  'total_assets_eoy': 'Total assets at end of year, latest return.', 'total_revenue': 'Total revenue, latest return.',
  'total_grants_paid': 'Grants and similar amounts paid (Form 990 Part I line 13), latest return. Includes grants to individuals and foreign grants.',
  'grants_listed_latest': 'Sum of the Schedule I recipient lines (cash plus noncash) on the latest return.', 'n_grants_latest': 'Number of Schedule I recipient lines on the latest return.',
  'grants_listed_all_years': 'Sum of Schedule I recipient lines across every return on file (flagged amounts excluded, as in the per-year columns).', 'n_grants_all_years': 'Number of Schedule I recipient lines across every return on file.',
 },
 'grants_all': {
  'funder_type': 'private_foundation (from Form 990-PF) or public_charity (from Form 990 Schedule I).',
  'recipient_ein': 'Recipient EIN as reported on the return. Only Schedule I has this; it is null for private foundation grants.',
  'recipient_ein_matched': 'EIN found by matching the recipient name and location to the Business Master File when the return gave no EIN. Unique matches only.',
  'recipient_match_method': 'How recipient_ein_matched was found: name_zip (same name key and ZIP, most reliable), name_state (same name key, unique in the state), simplified_name_state (legal suffixes and small words removed, common abbreviations expanded, unique in the state).',
  'recipient_ein_best': 'recipient_ein when reported, otherwise recipient_ein_matched. Indexed; use this to find all grants to one organization.',
  'recipient_city': 'Recipient city as typed.', 'recipient_state': 'Two-letter US state or territory of the recipient. Null for foreign or unknown (exported as XX). Filled from the ZIP code when the state field was unusable.',
  'recipient_zip': 'Five-digit recipient ZIP code (US addresses only).', 'recipient_county': 'Recipient county from zip_county.', 'recipient_country': 'US, or the country code for a foreign address.',
  'recipient_ntee': 'NTEE code of the recipient from the BMF (via reported or matched EIN).',
  'recipient_status': 'For 990-PF grants: the foundation status of the recipient as typed (PC = public charity, and so on). For Schedule I: the IRC section, for example 501(c)(3).',
  'amount': 'Grant amount in dollars. For Schedule I this is cash plus noncash assistance.',
  'amount_flag': 'True when the amount is negative or implausible. The row is kept; profile aggregates and the query tool leave it out.',
  'amount_flag_reason': 'negative, absurd (over 20 billion dollars), or exceeds_twice_funder_reported_totals (at least 1 million dollars and more than twice anything the funder reported for the year).',
  'tags': 'List of subject tags from taxonomy.json (keyword matches on purpose and recipient name, plus recipient NTEE). Can be empty.',
 },
 'pf_returns': {
  'name': 'Foundation name (line 1 and line 2 joined).', 'return_ts': 'Timestamp the return was signed and submitted; used to pick the latest filing.', 'return_type': 'Always 990PF.',
  'amended': '1 when the amended-return box is checked.', 'final_return': '1 when the final-return box is checked.', 'addr1': 'Street or PO box.', 'city': 'City.', 'state': 'State.', 'zip': 'ZIP as filed.',
  'country': 'US or a foreign country code.', 'signer_name': 'Officer who signed the return.', 'signer_title': 'Title of the signing officer.',
  'fmv_assets_eoy': 'Fair market value of assets at end of year (page 1, item I).', 'total_assets_eoy': 'Total assets at end of year, book value.', 'net_assets_eoy': 'Net assets or fund balances at end of year.',
  'total_revenue': 'Total revenue per books (Part I line 12).', 'contributions_received': 'Contributions received (Part I line 1).', 'total_expenses': 'Total expenses and disbursements per books (Part I line 26).',
  'grants_paid_books': 'Contributions, gifts, grants paid, books column (Part I line 25a).', 'grants_paid_charitable': 'Contributions, gifts, grants paid, charitable-purposes column (Part I line 25d, cash basis).',
  'total_grants_paid': 'Total of grants paid during the year (Part XIV line 3a).', 'total_grants_future': 'Total of grants approved for future payment (Part XIV line 3b).',
  'giving': 'One giving figure per return: total_grants_paid; when blank or zero, grants_paid_charitable, then grants_paid_books.',
  'only_preselected': '1 when the preselected-organizations-only box is checked.', 'operating_foundation': '1 when the return answers yes to private operating foundation status.',
  'n_grants_paid': 'Number of paid-grant lines that carry an amount.', 'sum_grants_paid_listed': 'Sum of the paid-grant lines.', 'n_officers': 'Number of officer lines.', 'n_app_contacts': 'Number of application-information blocks.',
  'direct_charitable_activities': 'Direct charitable activities text, when filled in.', 'flag_amounts_suspect': 'True when assets or giving are negative or impossible.',
 },
 'pf_grants': {
  'kind': 'paid (paid during the year) or future (approved for future payment).', 'recipient_addr1': 'Recipient street address.', 'recipient_city': 'Recipient city.', 'recipient_state': 'Recipient state exactly as typed (not cleaned; grants_all has the cleaned state).',
  'recipient_zip': 'Recipient ZIP as typed.', 'recipient_country': 'US or the foreign country code.', 'relationship': 'Relationship of an individual recipient to a foundation manager or contributor, as typed (usually NONE).',
  'foundation_status': 'Foundation status of the recipient as typed (PC, NC, PF, GOV and so on).', 'amount': 'Grant amount in dollars.',
 },
 'pf_officers': {'name': 'Person or business name.', 'title': 'Title.', 'hours': 'Average hours per week.', 'compensation': 'Compensation paid by the foundation.', 'city': 'City of the address given for the person.', 'state': 'State of that address.'},
 'pf_app_info': {'contact_name': 'Person or office to address applications to.', 'addr1': 'Street or PO box.', 'city': 'City.', 'state': 'State.', 'zip': 'ZIP.', 'phone': 'Phone.', 'email': 'Email as typed.',
  'form_and_materials': 'Form of application and required materials.', 'deadlines': 'Submission deadlines.', 'restrictions': 'Restrictions and limitations on awards.'},
 'pc_filers': {'name': 'Organization name.', 'addr1': 'Street or PO box.', 'city': 'City.', 'state': 'State.', 'zip': 'ZIP as filed.', 'mission': 'Mission or most significant activities.', 'principal_officer': 'Principal officer.',
  'total_revenue': 'Total revenue, current year.', 'total_assets_eoy': 'Total assets at end of year.', 'grants_paid_total': 'Grants and similar amounts paid (Part I line 13).',
  'n_grants_listed': 'Number of Schedule I recipient lines.', 'sum_grants_listed': 'Sum of Schedule I recipient lines (cash plus noncash).'},
 'pc_grants': {'recipient_ein': 'Recipient EIN as reported (nine digits; placeholders such as 000000000 nulled).', 'recipient_addr1': 'Recipient street address.', 'recipient_city': 'Recipient city.',
  'recipient_state': 'Recipient state as typed.', 'recipient_zip': 'Recipient ZIP as typed.', 'recipient_country': 'US or the foreign country code.', 'irc_section': 'IRC section of the recipient as typed, for example 501(c)(3).',
  'cash_amount': 'Cash grant.', 'noncash_amount': 'Noncash assistance (goods, securities).', 'amount': 'Cash plus noncash.'},
 'bmf': {'name': 'Organization name (IRS spelling, upper case).', 'street': 'Street.', 'city': 'City.', 'state': 'State.', 'zip': 'ZIP plus four.', 'subsection': 'IRC subsection code (03 = 501(c)(3)).',
  'foundation': 'Foundation code (02, 03, 04 are private foundations; 10 to 17 public charities).', 'asset_amt': 'Assets on the most recent return the IRS posted.', 'income_amt': 'Income amount.', 'revenue_amt': 'Revenue amount.',
  'ruling': 'Ruling date YYYYMM.', 'tax_period': 'Tax period of the amounts YYYYMM.', 'name_key': 'name_key(name).', 'name_key2': 'Simplified key used by the third matching pass.', 'source_file': 'Which BMF file the row came from (eo1 to eo4, eo_pr, eo_xx).'},
 'zip_county': {'zip': 'Five-digit ZIP code.', 'county_fips': 'Five-digit county FIPS code.', 'county_name': 'County name, for example "Sedgwick County".', 'state': 'State of that county.',
  'n_counties': 'Number of counties the ZIP area touches.', 'land_share': 'Share of the ZIP land area inside the chosen county.', 'source': 'census_zcta, or city_inferred for ZIP codes with no ZCTA.'},
 'recipients': {'recipient_state': 'Recipient state (cleaned).', 'recipient_name': 'Most common spelling of the name.', 'recipient_city': 'Most common city.', 'recipient_ein_best': 'Most common EIN (reported or matched).', 'recipient_ntee': 'NTEE code.',
  'n_grants': 'Grants received.', 'n_funders': 'Distinct funders.', 'dollars': 'Dollars received (flagged amounts excluded).'},
 'recipient_match': {'recipient_state': 'Recipient state (cleaned).', 'recipient_zip': 'Five-digit ZIP.', 'n_grants': 'Grants with this name, state and ZIP.', 'dollars': 'Their dollars.', 'ein': 'Matched BMF EIN, or null.', 'method': 'Matching method, or null.'},
 'subject_tags': {'tag': 'Tag name used in grants_all.tags.', 'label': 'Plain-language label.', 'n_grants': 'Grants carrying the tag.', 'dollars': 'Their dollars (flagged amounts excluded).', 'keywords': 'Keyword patterns.', 'exclude': 'Phrases blanked before matching.', 'ntee_prefixes': 'NTEE prefixes that also assign the tag.'},
 'qa_completeness': {'index_year': 'IRS index year.', 'index_rows': 'Rows in the IRS index file.', 'index_990pf': 'Index rows with RETURN_TYPE 990PF.', 'parsed_990pf': 'Of those, object IDs parsed into this database.', 'missing_990pf': 'Index 990PF rows not parsed.',
  'pct_parsed': 'parsed / index, percent.', 'index_990': 'Index rows with RETURN_TYPE 990.', 'parsed_990_with_schedule_i': 'Form 990 returns that had a Schedule I recipient table.', 'zips_in_list': 'Zip files listed for the year.', 'zips_done': 'Zip files processed.',
  'xml_members': 'XML files inside those zips.', 'parse_errors': 'XML files that failed to parse.'},
 'qa_match_validation': {'method': 'Matching method.', 'grants_tested': 'Schedule I grants with a reported EIN run through the matcher.', 'matched_ein_equals_reported': 'Matcher agreed with the reported EIN.', 'matched_ein_differs': 'Matcher returned a different EIN.', 'reported_ein_in_bmf': 'Reported EIN exists in the BMF.'},
 'source_zips': {'zip': 'Zip file name.', 'url': 'Download URL.', 'bytes': 'Size.', 'done': 'Processed.', 'xml_members': 'XML files inside.', 'parse_errors': 'Files that failed to parse.', 'pf_returns': '990-PF returns parsed.', 'pf_grants': '990-PF grant lines.',
  'pf_officers': 'Officer lines.', 'pf_app_info': 'Application blocks.', 'pc_filers': 'Form 990 returns with Schedule I.', 'pc_grants': 'Schedule I lines.'},
 'meta': {}, 'build_stats': {},
}

def describe(table, col):
    d = COLS.get(table, {}).get(col) or COMMON.get(col)
    if d: return d
    m = re.fullmatch(r'giving_fy(\d{4})', col)
    if m: return f'Giving reported for fiscal year {m.group(1)} (sum over returns ending that year). Null when no return is on file for that year.'
    m = re.fullmatch(r'grants_listed_fy(\d{4})', col)
    if m: return f'Schedule I dollars listed for fiscal year {m.group(1)}.'
    m = re.fullmatch(r'n_grants_fy(\d{4})', col)
    if m: return f'Number of grant lines for fiscal year {m.group(1)}.'
    return None

order = ['foundations', 'pc_funders', 'grants_all', 'pf_returns', 'pf_grants', 'pf_officers', 'pf_app_info', 'pc_filers', 'pc_grants', 'bmf', 'zip_county',
         'recipients', 'recipient_match', 'subject_tags', 'meta', 'build_stats', 'qa_completeness', 'qa_match_validation', 'source_zips']
tables = [r[0] for r in c.execute("SELECT table_name FROM information_schema.tables WHERE table_schema = 'main' AND table_type = 'BASE TABLE'").fetchall()]
order = [t for t in order if t in tables] + sorted(t for t in tables if t not in order)
missing = []
schema = []
for t in order:
    schema.append(f'### `{t}` ({n(rc.get(t, one(f"SELECT count(*) FROM {t}")))} rows)'); schema.append(''); schema.append(TABLES.get(t, '')); schema.append('')
    schema.append('| column | type | meaning |'); schema.append('|---|---|---|')
    for col, typ, *_ in c.execute(f'DESCRIBE "{t}"').fetchall():
        d = describe(t, col)
        if not d: missing.append(f'{t}.{col}'); d = '(undocumented)'
        schema.append(f'| `{col}` | {typ} | {d} |')
    schema.append('')
if missing: print('UNDOCUMENTED COLUMNS:', missing)

fy = c.execute("SELECT fiscal_year, count(*) FROM pf_returns GROUP BY 1 ORDER BY 1").fetchall()
comp = c.execute("SELECT index_year, index_990pf, parsed_990pf, pct_parsed FROM qa_completeness ORDER BY 1").fetchall()
f = c.execute("SELECT count(*), count(*) FILTER (efile_missing), count(*) FILTER (only_preselected), count(*) FILTER (ever_listed_application_info) FROM foundations").fetchone()
g = c.execute("SELECT count(*), count(*) FILTER (funder_type = 'private_foundation'), count(*) FILTER (funder_type = 'public_charity'), min(fiscal_year), max(fiscal_year) FROM grants_all").fetchone()
mr = c.execute("SELECT count(recipient_ein_matched), count(*) FROM grants_all WHERE funder_type = 'private_foundation'").fetchone()
tags = c.execute("SELECT tag, label, n_grants FROM subject_tags ORDER BY n_grants DESC").fetchall()
six = meta.get('stat_last_six_fiscal_years', '')
man = {}
mp = os.path.join(BASE, 'export', 'MANIFEST.json')
if os.path.exists(mp): man = json.load(open(mp))
dbsize = os.path.getsize(DB)

L = []
A = L.append
A('# Foundation database'); A('')
A(f"A national database of grantmakers and their grants, rebuilt from public IRS data. Build date {meta.get('build_date')}. It answers the questions a foundation directory answers: who funds what, where, how much, who runs the foundation, and whether it takes applications."); A('')
A('## What this is'); A('')
A(f"- **{n(f[0])} private foundations** (`foundations`): {n(f[0] - f[1])} with at least one e-filed Form 990-PF, plus {n(f[1])} that appear only in the IRS master file.")
A(f"- **{n(rc.get('pc_funders', 0))} grantmaking public charities** (`pc_funders`): organizations that list grants on Form 990 Schedule I, including community foundations and donor-advised fund sponsors.")
A(f"- **{n(g[0])} grants** (`grants_all`): {n(g[1])} from private foundations and {n(g[2])} from public charities, fiscal years {g[3]} to {g[4]}.")
A(f"- Officers and trustees ({n(rc.get('pf_officers', 0))} rows) and application instructions ({n(rc.get('pf_app_info', 0))} rows) from the 990-PF.")
A(f"- {n(f[2])} foundations say they give only to preselected organizations; {n(f[3])} have listed application information at least once."); A('')
A('Files in this folder:'); A('')
A('| file | purpose |'); A('|---|---|')
A(f'| `db/fdb.duckdb` | The database ({dbsize / 1e9:.2f} GB). Written by DuckDB {duckdb.__version__}; open it with that version or newer (`pip install duckdb`). |')
A('| `fdb.py` | Command-line query tool. Works on the database, or on the parquet exports when the database file is absent. |')
A('| `export/` | Parquet and CSV exports, none over 45 MB. `export/MANIFEST.json` lists every file with bytes and rows. |')
A('| `taxonomy.json` | Subject keyword lists behind `grants_all.tags`. Editable. |')
A('| `QA.md` | Quality report with real numbers and known limitations. |')
A('| `build.py`, `consolidate.py`, `export.py`, `qa.py`, `make_readme.py` | The pipeline, in run order. |'); A('')
A('## Sources'); A('')
A('- IRS Form 990 series e-file XML, zip years 2018 to 2026 (the 2018 files are not linked on the page but sit under the same root, https://apps.irs.gov/pub/epostcard/990/xml/2018/): https://www.irs.gov/charities-non-profits/form-990-series-downloads')
A(f"- IRS Exempt Organizations Business Master File extract (eo1 to eo4, eo_pr, eo_xx), downloaded {meta.get('stat_bmf_download_date')}: https://www.irs.gov/pub/irs-soi/eo1.csv (and eo2.csv, eo3.csv, eo4.csv, eo_pr.csv, eo_xx.csv)")
A('- Census 2020 ZCTA to county relationship file: https://www2.census.gov/geo/docs/maps-data/data/rel2020/zcta520/tab20_zcta520_county20_natl.txt'); A('')
A('## Coverage'); A('')
A('990-PF returns kept, by fiscal year (the calendar year in which the tax period ends):'); A('')
A('| fiscal year | returns |'); A('|---|---|')
for y, k in fy: A(f'| {y} | {n(k)} |')
A('')
A('Completeness against the IRS index (990PF rows in the index that were parsed):'); A('')
A('| index year | index 990PF | parsed | pct |'); A('|---|---|---|---|')
for y, a, b, pcent in comp: A(f'| {y} | {n(a)} | {n(b)} | {pcent:.2f}% |')
A('')
A(f"The fixed per-year columns in `foundations` and `pc_funders` cover fiscal years {six.replace(',', ', ')}. The newest fiscal year is still arriving and is partial. Returns for fiscal years before 2020 are thin because private foundations were not required to e-file until tax years beginning after July 1, 2019."); A('')
A('## Using the query tool'); A('')
A('```')
A('python3 fdb.py foundation 350868122                      # profile by EIN')
A('python3 fdb.py foundation "kansas health" --state KS     # profile by name fragment; lists other matches')
A('python3 fdb.py funders-of "meals on wheels" --state TX   # who has funded this organization, by year')
A('python3 fdb.py funders-of 237202594                      # the same by recipient EIN')
A('python3 fdb.py search --state KS --tags aging_seniors,food_hunger --years 2022-2025 --preselected no --min-grant 5000')
A('python3 fdb.py search --state TX --county Williamson --text "financial literacy" --funder-type pf --limit 40')
A('python3 fdb.py peers-funders --eins 751568798,237202594,741948646 --min-peers 2')
A('python3 fdb.py sql "select state, count(*) from foundations group by 1 order by 2 desc"')
A('python3 fdb.py tags          # the subject tags with counts')
A('python3 fdb.py info          # build date, row counts, caveats')
A('```'); A('')
A('Every command accepts `--json` (add `--compact` for one line). `search` ranks funders by the dollars and number of grants that match the filters and prints example grants for each funder. `--preselected no` keeps private foundations whose latest return does not check the preselected-only box, plus public charities. `--county` matches the start of the county name, so `--county Hays` finds Hays County.'); A('')
A('Subject tags:'); A('')
A('| tag | meaning | grants |'); A('|---|---|---|')
for t, lab, k in tags: A(f'| `{t}` | {lab} | {n(k)} |')
A('')
A('## Recipes (SQL)'); A('')
A('Open the database with `duckdb db/fdb.duckdb` or use `python3 fdb.py sql "..."`.'); A('')
A('```sql')
A('-- 1. Foundations in a county that accept requests, largest first')
A("SELECT name, city, fmv_assets_eoy, giving_latest, median_grant, website, app_deadlines")
A("FROM foundations WHERE state = 'KS' AND county = 'Sedgwick County' AND NOT efile_missing AND NOT only_preselected")
A("ORDER BY fmv_assets_eoy DESC LIMIT 50;")
A('')
A('-- 2. Who funds senior services in Texas, private foundations only, last three fiscal years')
A("SELECT funder_ein, any_value(funder_name) AS funder, count(*) AS grants, sum(amount) AS dollars")
A("FROM grants_all WHERE recipient_state = 'TX' AND list_contains(tags, 'aging_seniors')")
A("  AND funder_type = 'private_foundation' AND fiscal_year >= 2023 AND amount > 0 AND NOT amount_flag")
A("GROUP BY 1 ORDER BY dollars DESC LIMIT 50;")
A('')
A('-- 3. Every grant to one organization (reported or matched EIN), newest first')
A("SELECT fiscal_year, funder_name, funder_state, amount, purpose FROM grants_all")
A("WHERE recipient_ein_best = '237202594' ORDER BY fiscal_year DESC, amount DESC;")
A('')
A('-- 4. The same by name when the EIN did not match (name_key normalizes the text)')
A("SELECT fiscal_year, funder_name, amount, purpose FROM grants_all")
A("WHERE recipient_state = 'KS' AND recipient_key LIKE name_key('Habitat for Humanity') || '%' ORDER BY 1 DESC, 3 DESC;")
A('')
A('-- 5. Out-of-state foundations that give in a state')
A("SELECT g.funder_ein, any_value(g.funder_name) AS funder, any_value(g.funder_state) AS home, sum(g.amount) AS dollars, count(*) AS grants")
A("FROM grants_all g WHERE g.recipient_state = 'OK' AND g.funder_state <> 'OK' AND g.funder_type = 'private_foundation' AND g.amount > 0 AND NOT g.amount_flag")
A("GROUP BY 1 ORDER BY dollars DESC LIMIT 50;")
A('')
A('-- 6. Trustees: find a person across foundation boards')
A("SELECT DISTINCT funder_name, funder_state, name, title, fiscal_year FROM pf_officers WHERE name ILIKE '%buffett%' ORDER BY fiscal_year DESC;")
A('')
A('-- 7. Foundations whose giving is growing (fixed per-year columns)')
A(f"SELECT name, state, giving_fy{six.split(',')[-3] if six else 2023}, giving_fy{six.split(',')[-2] if six else 2024} FROM foundations")
A(f"WHERE giving_fy{six.split(',')[-2] if six else 2024} > 2 * giving_fy{six.split(',')[-3] if six else 2023} AND giving_fy{six.split(',')[-3] if six else 2023} > 100000 ORDER BY 4 DESC LIMIT 50;")
A('')
A('-- 8. Reading the parquet exports without the database')
A("SELECT * FROM read_parquet('export/grants_all/KS*.parquet') WHERE list_contains(tags, 'food_hunger') LIMIT 20;")
A("SELECT * FROM read_parquet('export/foundations*.parquet') WHERE state = 'KS';")
A('```'); A('')
A('## Exports'); A('')
if man:
    A(f"`export/` holds {man.get('n_files')} files, {man.get('total_bytes', 0) / 1e6:,.0f} MB in total; the largest is {man.get('max_file_bytes', 0) / 1e6:.1f} MB. Parquet files use zstd compression."); A('')
    A('| table | files | rows | MB |'); A('|---|---|---|---|')
    for t, d in sorted(man.get('tables', {}).items()): A(f"| {t} | {d['files']} | {n(d['rows'])} | {d['bytes'] / 1e6:,.1f} |")
    A('')
A('- `foundations.parquet`, `pc_funders.parquet`, `pf_app_info.parquet`: whole tables. `pf_officers` is split into numbered parts when one file would pass 45 MB.')
A('- `grants_all/<ST>.parquet` (or `<ST>_01.parquet`, `<ST>_02.parquet`, ... for large states): grants by recipient state, sorted by fiscal year descending, then amount descending. `XX` holds recipients with no US state (foreign or unknown).')
A('- `foundations_by_state/<ST>.csv`: one spreadsheet-ready CSV per foundation home state with the main profile columns. `XX.csv` holds foreign or unknown states.')
A('- `subject_tags.parquet`, `meta.parquet`, `qa_completeness.parquet`, `taxonomy.json`: reference tables.'); A('')
A('## Schema'); A('')
A('Money is in whole dollars as reported. EINs are text. A fiscal year is the calendar year in which the tax period ends.'); A('')
L.extend(schema)
A('## How it was built'); A('')
A('1. `build.py run` downloads each IRS zip, parses every Form 990-PF and every Form 990 with a Schedule I recipient table, and writes parquet parts to `parts_v3/`. It is resumable.')
A('2. `consolidate.py` dedupes and builds every table in `db/fdb.duckdb`. `python3 consolidate.py tags` re-applies `taxonomy.json` after you edit it.')
A('3. `qa.py` writes `QA.md`. 4. `export.py` writes `export/`. 5. `make_readme.py` writes this file.'); A('')
A('Dedupe rules: a return that appears in more than one zip is counted once (by object ID); one 990-PF is kept per EIN and period end, the latest filing timestamp wins, so amended returns replace originals; for Form 990 the highest object ID wins. Grants, officers and application rows are kept only for the surviving returns.'); A('')
A('## Limitations'); A('')
for x in json.loads(meta['caveats']): A('- ' + x)
A('')
txt = '\n'.join(L).replace('—', '-').replace('–', '-') + '\n'
open(OUT, 'w').write(txt)
print('wrote', OUT, len(txt), 'chars; undocumented columns:', len(missing))
