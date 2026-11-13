# Foundation data

A national dataset of United States grantmakers and the grants they report, compiled from public Internal Revenue Service (IRS) records. Build date 2026-11-12.

It answers the questions a foundation directory answers: who funds what, where, how much, who runs the foundation, and whether it takes applications.

- **165,592 private foundations** (`foundations`): 154,173 with at least one e-filed Form 990-PF, plus 11,419 that appear only in the IRS master file.
- **93,322 grantmaking public charities** (`pc_funders`): organizations that list grants on Form 990 Schedule I, including community foundations and donor-advised fund sponsors.
- **20,181,044 grants** (`grants_all`): 13,323,732 from private foundations and 6,857,312 from public charities, fiscal years 2015 to 2026.
- Officers and trustees (2,922,561 rows) and application instructions (229,675 rows) from Form 990-PF.
- 107,513 foundations say they give only to preselected organizations; 38,191 have listed application information at least once.

**This dataset is compiled from public IRS records. It is provided as is, with no warranty of any kind, express or implied, including accuracy, completeness or fitness for a purpose. Figures are as reported by the filers. Verify anything important against the original return or with the funder.**

## What is in this repository

| path | purpose |
|---|---|
| `data/*.parquet` | Whole tables: `foundations`, `pc_funders`, `pf_officers`, `pf_app_info`, `subject_tags`, `meta`, `qa_completeness`, and `funder_web_profiles` (read from funders' own websites, not IRS data). |
| `data/web_profiles/*.json` | The same website profiles as small JSON files keyed by EIN, for the search service. |
| `data/grants_all/<ST>.parquet` | Grants by recipient state (`<ST>_01`, `<ST>_02`, ... for large states; `XX` holds recipients with no US state). Sorted by fiscal year descending, then amount descending. |
| `data/MANIFEST.json` | Every data file with bytes, rows and SHA-256. |
| `data/index/` | Compact index files for range-request access by a small HTTP service. Described below. |
| `fdb.py` | Command-line query tool (Python, DuckDB). Reads the parquet files in `data/`. |
| `taxonomy.json` | Keyword lists behind the subject tags in `grants_all.tags`. |
| `pipeline/` | The scripts that built the dataset from the IRS files, for transparency and reproduction. |

All parquet files use zstd compression. No file is larger than 45 MB.

## Sources

- IRS Form 990 series e-file XML, zip files for 2018 to 2026: https://www.irs.gov/charities-non-profits/form-990-series-downloads (the 2018 files are not linked on that page but sit under the same root, https://apps.irs.gov/pub/epostcard/990/xml/2018/).
- IRS Exempt Organizations Business Master File extract (eo1 to eo4, eo_pr, eo_xx), downloaded 2026-11-12: https://www.irs.gov/pub/irs-soi/eo1.csv (and eo2.csv, eo3.csv, eo4.csv, eo_pr.csv, eo_xx.csv).
- Census 2020 ZCTA to county relationship file: https://www2.census.gov/geo/docs/maps-data/data/rel2020/zcta520/tab20_zcta520_county20_natl.txt

Every Form 990-PF in those zip files was parsed (returns, grants paid, officers, application information), and every Form 990 with a Schedule I recipient table (grants made by public charities). All 81 zip files parsed with zero parse errors.

## Coverage

990-PF returns kept, by fiscal year (the calendar year in which the tax period ends):

| fiscal year | returns |
|---|---|
| 2015 | 112 |
| 2016 | 1,990 |
| 2017 | 60,533 |
| 2018 | 73,356 |
| 2019 | 83,006 |
| 2020 | 110,218 |
| 2021 | 118,115 |
| 2022 | 121,880 |
| 2023 | 124,689 |
| 2024 | 125,412 |
| 2025 | 89,445 |
| 2026 | 4,707 |

Completeness against the IRS index (990PF rows in the index that were parsed):

| index year | index 990PF | parsed | pct |
|---|---|---|---|
| 2018 | 67,520 | 56,502 | 83.68% |
| 2019 | 64,614 | 64,614 | 100.00% |
| 2020 | 27,883 | 27,883 | 100.00% |
| 2021 | 108,644 | 108,644 | 100.00% |
| 2022 | 117,073 | 117,073 | 100.00% |
| 2023 | 124,666 | 124,665 | 100.00% |
| 2024 | 126,982 | 126,981 | 100.00% |
| 2025 | 130,347 | 130,346 | 100.00% |
| 2026 | 85,068 | 85,067 | 100.00% |

The fixed per-year columns in `foundations` and `pc_funders` cover fiscal years 2020, 2021, 2022, 2023, 2024, 2025. The newest fiscal year is still arriving and is partial. Returns for fiscal years before 2020 are thin because private foundations were not required to e-file until tax years beginning after July 1, 2019.

## Query it with DuckDB over HTTP

Nothing needs to be downloaded first. DuckDB reads only the parts of each parquet file that a query needs, using HTTP range requests. Replace `OWNER/REPO` with the owner and name of this repository.

```sql
INSTALL httpfs; LOAD httpfs;
SET VARIABLE base = 'https://raw.githubusercontent.com/OWNER/REPO/main/data';

-- Foundations in one county that accept requests, largest first
SELECT ein, name, city, fmv_assets_eoy, giving_latest, median_grant, website, app_deadlines
FROM read_parquet(getvariable('base') || '/foundations.parquet')
WHERE state = 'KS' AND county = 'Sedgwick County' AND NOT efile_missing AND NOT only_preselected
ORDER BY fmv_assets_eoy DESC LIMIT 50;

-- Who funds senior services in Texas: private foundations, last three fiscal years
SELECT funder_ein, any_value(funder_name) AS funder, count(*) AS grants, sum(amount) AS dollars
FROM read_parquet([getvariable('base') || '/grants_all/TX_01.parquet', getvariable('base') || '/grants_all/TX_02.parquet'])
WHERE list_contains(tags, 'aging_seniors') AND funder_type = 'private_foundation'
  AND fiscal_year >= 2023 AND amount > 0 AND NOT amount_flag
GROUP BY 1 ORDER BY dollars DESC LIMIT 50;

-- Every grant to one organization in a state, by its EIN (reported or matched)
SELECT fiscal_year, funder_name, funder_state, amount, purpose
FROM read_parquet(getvariable('base') || '/grants_all/KS.parquet')
WHERE recipient_ein_best = '480873431' ORDER BY fiscal_year DESC, amount DESC;

-- The same by name (recipient_key is the normalized name: upper case, punctuation removed)
SELECT fiscal_year, funder_name, amount, purpose
FROM read_parquet(getvariable('base') || '/grants_all/KS.parquet')
WHERE recipient_key LIKE 'HABITAT FOR HUMANITY%' ORDER BY 1 DESC, 3 DESC;

-- Trustees: find a person across foundation boards
SELECT DISTINCT funder_name, funder_state, name, title, fiscal_year
FROM read_parquet(getvariable('base') || '/pf_officers.parquet')
WHERE name ILIKE '%buffett%' ORDER BY fiscal_year DESC;
```

Notes:

- A web server cannot list a folder, so name the files. `data/MANIFEST.json` lists them all. States with one file use `<ST>.parquet`; California, New York and Texas are split into numbered parts.
- Always filter grants with `amount > 0 AND NOT amount_flag` unless you want the flagged rows.
- To work offline, clone the repository and replace the base URL with the local `data` folder. Local paths accept wildcards: `read_parquet('data/grants_all/*.parquet')`.

### The command-line tool

```
pip install duckdb
python3 fdb.py foundation 480873431                      # profile by EIN
python3 fdb.py foundation "kansas health" --state KS     # profile by name fragment; lists other matches
python3 fdb.py funders-of "meals on wheels" --state TX   # who has funded this organization, by year
python3 fdb.py funders-of 237202594                      # the same by recipient EIN
python3 fdb.py search --state KS --tags aging_seniors,food_hunger --years 2022-2025 --preselected no --min-grant 5000
python3 fdb.py search --state TX --county Williamson --text "financial literacy" --funder-type pf --limit 40
python3 fdb.py peers-funders --eins 751568798,237202594,741948646 --min-peers 2
python3 fdb.py tags          # the subject tags with counts
python3 fdb.py info          # build date, row counts, caveats
```

Run it from a clone of this repository; it reads `data/` (or pass `--data DIR`). Every command accepts `--json`. `search` ranks funders by the dollars and number of grants that match the filters and prints example grants for each funder. `--preselected no` keeps private foundations whose latest return does not check the preselected-only box, plus public charities. `--county` matches the start of the county name.

## Subject tags

Tags are keyword matches on the grant purpose and recipient name, plus the recipient NTEE code. A grant can carry several tags or none. They are a search aid, not human coding.

| tag | meaning | grants |
|---|---|---|
| `education_scholarships` | Education and scholarships | 4,997,476 |
| `religion` | Religion | 2,941,251 |
| `children_families` | Children and families (human services) | 2,526,155 |
| `health_mental_health` | Health and mental health | 2,428,549 |
| `arts_culture` | Arts and culture | 1,756,690 |
| `youth_development` | Youth development and mentoring | 1,302,273 |
| `environment` | Environment | 1,044,311 |
| `community_development` | Community development | 986,912 |
| `international` | International | 898,878 |
| `animals` | Animals | 748,560 |
| `food_hunger` | Food and hunger | 718,206 |
| `science_research` | Science and research | 717,666 |
| `public_safety_justice` | Public safety and justice | 655,545 |
| `housing_homelessness` | Housing and homelessness | 634,381 |
| `sports_recreation` | Sports and recreation | 599,745 |
| `women` | Women and girls | 543,563 |
| `disability` | Disability | 346,342 |
| `workforce_financial` | Financial literacy and workforce | 289,942 |
| `aging_seniors` | Aging and seniors | 274,103 |
| `rural_agriculture` | Rural and agriculture | 237,752 |
| `veterans` | Veterans and military | 215,037 |
| `recovery_reentry` | Substance use recovery and reentry | 131,897 |
| `immigrants_refugees` | Immigrants and refugees | 104,652 |

## Schema

Money is in whole dollars as reported. EINs are text, nine digits, leading zeros kept. A fiscal year is the calendar year in which the tax period ends.

### `foundations` (165,592 rows)

One row per private foundation EIN: the profile table. Built from the latest e-filed 990-PF plus aggregates over every return on file, plus every Business Master File private foundation that has no e-filed return here (efile_missing = true).

| column | type | meaning |
|---|---|---|
| `ein` | VARCHAR | Employer Identification Number, nine digits, text (leading zeros kept). |
| `name` | VARCHAR | Foundation name on the latest return (BMF name for efile_missing rows). |
| `address` | VARCHAR | Street or PO box. |
| `city` | VARCHAR | City. |
| `state` | VARCHAR | State (two letters; foreign filers show a province or country here). |
| `zip` | VARCHAR | ZIP code as filed. |
| `county` | VARCHAR | County of the mailing address, from the Census ZIP to county file. |
| `phone` | VARCHAR | Phone number on the return. |
| `website` | VARCHAR | Website from the return, lower-cased; null when the filer wrote N/A, NONE, 0 or a third-party site (GuideStar, Candid, a state registry). Nothing is added, so it may lack http. |
| `signer` | VARCHAR | Officer who signed the latest return: name, title. |
| `fiscal_year_latest` | INTEGER | Fiscal year of the latest return on file. |
| `period_end_latest` | DATE | Period end date of the latest return on file. |
| `fmv_assets_eoy` | DOUBLE | Fair market value of all assets at end of year (990-PF page 1, item I), latest return. |
| `total_assets_eoy` | DOUBLE | Total assets at end of year, book value (Part II line 16), latest return. |
| `total_revenue` | DOUBLE | Total revenue per books (Part I line 12 column a), latest return. |
| `contributions_received` | DOUBLE | Contributions, gifts and grants received (Part I line 1), latest return. Shows whether the foundation is still being funded. |
| `total_grants_paid` | DOUBLE | Total grants and contributions paid during the year as reported in Part XIV line 3a, latest return. |
| `giving_latest` | DOUBLE | Giving on the latest return: Part XIV line 3a total; when that is blank or zero, Part I line 25 (charitable column, then books column). |
| `only_preselected` | BOOLEAN | True when the latest return checks the box "the foundation only makes contributions to preselected charitable organizations and does not accept unsolicited requests for funds". |
| `operating_foundation` | BOOLEAN | True when the latest return claims private operating foundation status (for efile_missing rows: BMF foundation code 02 or 03). |
| `final_return_latest` | BOOLEAN | True when the latest return is marked as a final return (the foundation is closing or has closed). |
| `first_fiscal_year` | INTEGER | Earliest fiscal year on file. |
| `last_fiscal_year` | INTEGER | Latest fiscal year on file. |
| `n_returns` | BIGINT | Number of returns on file for this EIN (0 for efile_missing rows). |
| `total_giving_all_years` | DOUBLE | Sum of giving across every return on file. |
| `n_grants_all_years` | BIGINT | Number of grant lines (with an amount) across every return on file. |
| `giving_fy2020` | DOUBLE | Giving reported for fiscal year 2020 (sum over returns ending that year). Null when no return is on file for that year. |
| `n_grants_fy2020` | BIGINT | Number of grant lines for fiscal year 2020. |
| `giving_fy2021` | DOUBLE | Giving reported for fiscal year 2021 (sum over returns ending that year). Null when no return is on file for that year. |
| `n_grants_fy2021` | BIGINT | Number of grant lines for fiscal year 2021. |
| `giving_fy2022` | DOUBLE | Giving reported for fiscal year 2022 (sum over returns ending that year). Null when no return is on file for that year. |
| `n_grants_fy2022` | BIGINT | Number of grant lines for fiscal year 2022. |
| `giving_fy2023` | DOUBLE | Giving reported for fiscal year 2023 (sum over returns ending that year). Null when no return is on file for that year. |
| `n_grants_fy2023` | BIGINT | Number of grant lines for fiscal year 2023. |
| `giving_fy2024` | DOUBLE | Giving reported for fiscal year 2024 (sum over returns ending that year). Null when no return is on file for that year. |
| `n_grants_fy2024` | BIGINT | Number of grant lines for fiscal year 2024. |
| `giving_fy2025` | DOUBLE | Giving reported for fiscal year 2025 (sum over returns ending that year). Null when no return is on file for that year. |
| `n_grants_fy2025` | BIGINT | Number of grant lines for fiscal year 2025. |
| `giving_by_year` | VARCHAR | JSON text keyed by fiscal year with the dollars and number of grants for every year on file. |
| `median_grant` | DOUBLE | Median grant amount across all years on file (grants above zero, flagged amounts excluded). |
| `avg_grant` | DOUBLE | Average grant amount across all years on file (grants above zero, flagged amounts excluded). |
| `max_grant` | DOUBLE | Largest single grant on file (flagged amounts excluded). |
| `top_recipient_states` | VARCHAR | Top five recipient states by dollars across all years, with each state share of all listed dollars, for example "TX 62%; OK 10%". |
| `top_recipient_state` | VARCHAR | The single largest recipient state by dollars. |
| `home_state_share` | DOUBLE | Share (0 to 1) of listed grant dollars that went to recipients in the funder home state. |
| `ever_listed_application_info` | BOOLEAN | True when any return on file filled in the application information block. |
| `app_fiscal_year` | INTEGER | Fiscal year of the return the application details below come from (the most recent one that has them). |
| `app_contact_name` | VARCHAR | Who applications should be addressed to. |
| `app_address` | VARCHAR | Mailing address for applications. |
| `app_phone` | VARCHAR | Phone for application questions. |
| `app_email` | VARCHAR | Email for applications, as typed. |
| `app_form_and_materials` | VARCHAR | The form in which applications should be submitted and what to include. |
| `app_deadlines` | VARCHAR | Submission deadlines. |
| `app_restrictions` | VARCHAR | Restrictions and limitations on awards (geography, fields, kinds of institutions). |
| `officers` | VARCHAR | Officers, directors and trustees on the latest return as one text: "name, title; name, title; ..." (first 60). |
| `direct_charitable_activities` | VARCHAR | Summary of direct charitable activities from the latest return (Part VIII-A), when filled in. |
| `ntee_cd` | VARCHAR | NTEE code from the Business Master File (letter = major group, for example B education, P human services). Null when the IRS has not assigned one or the EIN is not in the BMF. |
| `in_bmf` | BOOLEAN | True when the EIN appears in the Business Master File extract. |
| `bmf_foundation_code` | VARCHAR | BMF FOUNDATION code: 02 exempt operating foundation, 03 private operating foundation, 04 private non-operating foundation, 00 not coded, 10 to 17 public charity classes. |
| `bmf_subsection` | VARCHAR | BMF SUBSECTION code (03 means 501(c)(3)). |
| `bmf_ruling` | VARCHAR | BMF RULING: year and month of the IRS exemption ruling (YYYYMM). |
| `bmf_asset_amt` | BIGINT | Assets per the Business Master File. |
| `bmf_income_amt` | BIGINT | Income per the Business Master File. |
| `bmf_tax_period` | VARCHAR | Tax period (YYYYMM) the BMF amounts refer to. |
| `efile_missing` | BOOLEAN | True for foundations listed in the BMF (foundation code 02, 03 or 04) that have no e-filed 990-PF in this database. They have no grants, officers or application details here. |
| `flag_amounts_suspect` | BOOLEAN | True when the latest return has negative or impossible asset or giving figures. Treat its numbers with care. |
| `latest_object_id` | VARCHAR | object_id of the latest return, for tracing back to the source. |

### `pc_funders` (93,322 rows)

One row per public charity that lists grants on Form 990 Schedule I (community foundations, donor-advised fund sponsors, United Ways, hospitals and universities that regrant, corporate-style public charities).

| column | type | meaning |
|---|---|---|
| `ein` | VARCHAR | Employer Identification Number, nine digits, text (leading zeros kept). |
| `name` | VARCHAR | Organization name on the latest return. |
| `address` | VARCHAR | Street or PO box. |
| `city` | VARCHAR | City. |
| `state` | VARCHAR | State. |
| `zip` | VARCHAR | ZIP code as filed. |
| `county` | VARCHAR | County of the mailing address, from the Census ZIP to county file. |
| `phone` | VARCHAR | Phone number on the return. |
| `website` | VARCHAR | Website from the return, lower-cased; null when the filer wrote N/A, NONE, 0 or a third-party site (GuideStar, Candid, a state registry). Nothing is added, so it may lack http. |
| `mission` | VARCHAR | Mission or most significant activities (Form 990 Part I line 1), latest return. |
| `principal_officer` | VARCHAR | Principal officer named on the latest return. |
| `fiscal_year_latest` | INTEGER | Fiscal year of the latest return on file. |
| `period_end_latest` | DATE | Period end date of the latest return on file. |
| `total_assets_eoy` | DOUBLE | Total assets at end of year, latest return. |
| `total_revenue` | DOUBLE | Total revenue, latest return. |
| `total_grants_paid` | DOUBLE | Grants and similar amounts paid (Form 990 Part I line 13), latest return. Includes grants to individuals and foreign grants. |
| `grants_listed_latest` | DOUBLE | Sum of the Schedule I recipient lines (cash plus noncash) on the latest return. |
| `n_grants_latest` | INTEGER | Number of Schedule I recipient lines on the latest return. |
| `first_fiscal_year` | INTEGER | Earliest fiscal year on file. |
| `last_fiscal_year` | INTEGER | Latest fiscal year on file. |
| `n_returns` | BIGINT | Number of returns on file for this EIN (0 for efile_missing rows). |
| `grants_listed_all_years` | DOUBLE | Sum of Schedule I recipient lines across every return on file (flagged amounts excluded, as in the per-year columns). |
| `n_grants_all_years` | BIGINT | Number of Schedule I recipient lines across every return on file. |
| `grants_listed_fy2020` | DOUBLE | Schedule I dollars listed for fiscal year 2020. |
| `n_grants_fy2020` | BIGINT | Number of grant lines for fiscal year 2020. |
| `grants_listed_fy2021` | DOUBLE | Schedule I dollars listed for fiscal year 2021. |
| `n_grants_fy2021` | BIGINT | Number of grant lines for fiscal year 2021. |
| `grants_listed_fy2022` | DOUBLE | Schedule I dollars listed for fiscal year 2022. |
| `n_grants_fy2022` | BIGINT | Number of grant lines for fiscal year 2022. |
| `grants_listed_fy2023` | DOUBLE | Schedule I dollars listed for fiscal year 2023. |
| `n_grants_fy2023` | BIGINT | Number of grant lines for fiscal year 2023. |
| `grants_listed_fy2024` | DOUBLE | Schedule I dollars listed for fiscal year 2024. |
| `n_grants_fy2024` | BIGINT | Number of grant lines for fiscal year 2024. |
| `grants_listed_fy2025` | DOUBLE | Schedule I dollars listed for fiscal year 2025. |
| `n_grants_fy2025` | BIGINT | Number of grant lines for fiscal year 2025. |
| `giving_by_year` | VARCHAR | JSON text keyed by fiscal year with the dollars and number of grants for every year on file. |
| `median_grant` | DOUBLE | Median grant amount across all years on file (grants above zero, flagged amounts excluded). |
| `avg_grant` | DOUBLE | Average grant amount across all years on file (grants above zero, flagged amounts excluded). |
| `max_grant` | DOUBLE | Largest single grant on file (flagged amounts excluded). |
| `top_recipient_states` | VARCHAR | Top five recipient states by dollars across all years, with each state share of all listed dollars, for example "TX 62%; OK 10%". |
| `top_recipient_state` | VARCHAR | The single largest recipient state by dollars. |
| `home_state_share` | DOUBLE | Share (0 to 1) of listed grant dollars that went to recipients in the funder home state. |
| `ntee_cd` | VARCHAR | NTEE code from the Business Master File (letter = major group, for example B education, P human services). Null when the IRS has not assigned one or the EIN is not in the BMF. |
| `in_bmf` | BOOLEAN | True when the EIN appears in the Business Master File extract. |
| `bmf_foundation_code` | VARCHAR | BMF FOUNDATION code: 02 exempt operating foundation, 03 private operating foundation, 04 private non-operating foundation, 00 not coded, 10 to 17 public charity classes. |
| `bmf_subsection` | VARCHAR | BMF SUBSECTION code (03 means 501(c)(3)). |
| `bmf_ruling` | VARCHAR | BMF RULING: year and month of the IRS exemption ruling (YYYYMM). |
| `latest_object_id` | VARCHAR | object_id of the latest return, for tracing back to the source. |

### `grants_all` (20,181,044 rows)

Every grant in one shape: 990-PF grants paid during the year plus Schedule I grants. Sorted by recipient_state, then recipient_key. This is the table to search.

| column | type | meaning |
|---|---|---|
| `funder_type` | VARCHAR | private_foundation (from Form 990-PF) or public_charity (from Form 990 Schedule I). |
| `funder_ein` | VARCHAR | EIN of the grantmaker. |
| `funder_name` | VARCHAR | Name of the grantmaker as written on that return. |
| `funder_city` | VARCHAR | City of the grantmaker on that return. |
| `funder_state` | VARCHAR | State of the grantmaker on that return. |
| `fiscal_year` | INTEGER | Calendar year in which the tax period ends (year of period_end). |
| `recipient_name` | VARCHAR | Recipient name as typed on the return. |
| `recipient_key` | VARCHAR | Normalized recipient name for grouping and matching: upper case, accents stripped, & becomes AND, apostrophes and periods deleted, other punctuation turned into spaces, spaces collapsed, leading THE removed. The command-line tool applies the same rule to the names you type. |
| `recipient_ein` | VARCHAR | Recipient EIN as reported on the return. Only Schedule I has this; it is null for private foundation grants. |
| `recipient_ein_matched` | VARCHAR | EIN found by matching the recipient name and location to the Business Master File when the return gave no EIN. Unique matches only. |
| `recipient_match_method` | VARCHAR | How recipient_ein_matched was found: name_zip (same name key and ZIP, most reliable), name_state (same name key, unique in the state), simplified_name_state (legal suffixes and small words removed, common abbreviations expanded, unique in the state). |
| `recipient_ein_best` | VARCHAR | recipient_ein when reported, otherwise recipient_ein_matched. Indexed; use this to find all grants to one organization. |
| `recipient_city` | VARCHAR | Recipient city as typed. |
| `recipient_state` | VARCHAR | Two-letter US state or territory of the recipient. Null for foreign or unknown (exported as XX). Filled from the ZIP code when the state field was unusable. |
| `recipient_zip` | VARCHAR | Five-digit recipient ZIP code (US addresses only). |
| `recipient_county` | VARCHAR | Recipient county from the Census ZIP to county file. |
| `recipient_country` | VARCHAR | US, or the country code for a foreign address. |
| `recipient_ntee` | VARCHAR | NTEE code of the recipient from the BMF (via reported or matched EIN). |
| `recipient_status` | VARCHAR | For 990-PF grants: the foundation status of the recipient as typed (PC = public charity, and so on). For Schedule I: the IRC section, for example 501(c)(3). |
| `purpose` | VARCHAR | Purpose of the grant as typed on the return. |
| `amount` | DOUBLE | Grant amount in dollars. For Schedule I this is cash plus noncash assistance. |
| `amount_flag` | BOOLEAN | True when the amount is negative or implausible. The row is kept; profile aggregates and the query tool leave it out. |
| `amount_flag_reason` | VARCHAR | negative, absurd (over 20 billion dollars), or exceeds_twice_funder_reported_totals (at least 1 million dollars and more than twice anything the funder reported for the year). |
| `period_end` | DATE | Last day of the tax period. |
| `object_id` | VARCHAR | IRS e-file object ID of the return (18 digits). The source XML is <object_id>_public.xml inside the zip named in source_zip. |
| `seq` | INTEGER | Line number of the row within its return (order of appearance). |
| `tags` | VARCHAR[] | List of subject tags from taxonomy.json (keyword matches on purpose and recipient name, plus recipient NTEE). Can be empty. |

### `pf_officers` (2,922,561 rows)

Officers, directors, trustees and key employees listed on each kept 990-PF.

| column | type | meaning |
|---|---|---|
| `object_id` | VARCHAR | IRS e-file object ID of the return (18 digits). The source XML is <object_id>_public.xml inside the zip named in source_zip. |
| `seq` | INTEGER | Line number of the row within its return (order of appearance). |
| `funder_ein` | VARCHAR | EIN of the grantmaker. |
| `funder_name` | VARCHAR | Name of the grantmaker as written on that return. |
| `funder_state` | VARCHAR | State of the grantmaker on that return. |
| `fiscal_year` | INTEGER | Calendar year in which the tax period ends (year of period_end). |
| `period_end` | DATE | Last day of the tax period. |
| `name` | VARCHAR | Person or business name. |
| `title` | VARCHAR | Title. |
| `hours` | DOUBLE | Average hours per week. |
| `compensation` | DOUBLE | Compensation paid by the foundation. |
| `city` | VARCHAR | City of the address given for the person. |
| `state` | VARCHAR | State of that address. |

### `pf_app_info` (229,675 rows)

Application information from Form 990-PF Part XIV line 2 (who to write to, form, deadlines, restrictions) for each kept return that filled it in.

| column | type | meaning |
|---|---|---|
| `object_id` | VARCHAR | IRS e-file object ID of the return (18 digits). The source XML is <object_id>_public.xml inside the zip named in source_zip. |
| `seq` | INTEGER | Line number of the row within its return (order of appearance). |
| `funder_ein` | VARCHAR | EIN of the grantmaker. |
| `funder_name` | VARCHAR | Name of the grantmaker as written on that return. |
| `funder_state` | VARCHAR | State of the grantmaker on that return. |
| `fiscal_year` | INTEGER | Calendar year in which the tax period ends (year of period_end). |
| `period_end` | DATE | Last day of the tax period. |
| `contact_name` | VARCHAR | Person or office to address applications to. |
| `addr1` | VARCHAR | Street or PO box. |
| `city` | VARCHAR | City. |
| `state` | VARCHAR | State. |
| `zip` | VARCHAR | ZIP. |
| `phone` | VARCHAR | Phone. |
| `email` | VARCHAR | Email as typed. |
| `form_and_materials` | VARCHAR | Form of application and required materials. |
| `deadlines` | VARCHAR | Submission deadlines. |
| `restrictions` | VARCHAR | Restrictions and limitations on awards. |

### `subject_tags` (23 rows)

The subject taxonomy with counts: one row per tag.

| column | type | meaning |
|---|---|---|
| `tag` | VARCHAR | Tag name used in grants_all.tags. |
| `label` | VARCHAR | Plain-language label. |
| `n_grants` | BIGINT | Grants carrying the tag. |
| `dollars` | DOUBLE | Their dollars (flagged amounts excluded). |
| `keywords` | VARCHAR | Keyword patterns. |
| `exclude` | VARCHAR | Phrases blanked before matching. |
| `ntee_prefixes` | VARCHAR | NTEE prefixes that also assign the tag. |

### `meta` (41 rows)

Build facts as key and value: build date, sources, zip list, row counts, caveats.

| column | type | meaning |
|---|---|---|
| `key` | VARCHAR | Name of the fact. |
| `value` | VARCHAR | Value as text (some are JSON). |

### `qa_completeness` (9 rows)

Per IRS index year: 990PF rows in the index against 990-PF returns parsed.

| column | type | meaning |
|---|---|---|
| `index_year` | INTEGER | IRS index year. |
| `index_rows` | BIGINT | Rows in the IRS index file. |
| `index_990pf` | BIGINT | Index rows with RETURN_TYPE 990PF. |
| `parsed_990pf` | BIGINT | Of those, object IDs parsed into this database. |
| `missing_990pf` | BIGINT | Index 990PF rows not parsed. |
| `pct_parsed` | DOUBLE | parsed / index, percent. |
| `index_990` | BIGINT | Index rows with RETURN_TYPE 990. |
| `parsed_990_with_schedule_i` | BIGINT | Form 990 returns that had a Schedule I recipient table. |
| `zips_in_list` | INTEGER | Zip files listed for the year. |
| `zips_done` | INTEGER | Zip files processed. |
| `xml_members` | BIGINT | XML files inside those zips. |
| `parse_errors` | BIGINT | XML files that failed to parse. |

### `funder_web_profiles` (website profiles; not IRS data)

Everything else in this repository comes from IRS filings. This table does not. Each row was read from the funder's **own website** (never an aggregator) on the date in `checked_on`, and carries the pages it was read from in `source_urls`. It exists because a tax return rarely says how a funder actually takes requests today.

Coverage is partial and grows over time: the first pass (2026-11-13) covers 612 funders. Rows are also published as small JSON files, `data/web_profiles/NN.json` (NN = first two digits of the 9-digit EIN, an object keyed by EIN), with counts in `data/web_profiles/index.json`. The search service adds the row to `/api/foundation` as `web_profile`.

| Column | Meaning |
|---|---|
| `ein`, `name` | The funder (9-digit EIN as text). |
| `status` | `profile` (site found and read), `no website found`, `site unreachable`, `ambiguous identity` (a site was found but could not be tied to this EIN). |
| `website`, `website_source` | The site read, and how it was found (`return` = listed on the tax return, `search`). |
| `accepts_unsolicited` | `yes`, `LOI first`, `invitation only`, `restricted program`, `paused`, or `not stated`. |
| `accepts_evidence` | The words on the site that support that value, with the page. |
| `priorities` | JSON list of stated funding priorities. |
| `geographic_limits`, `eligibility`, `not_funded`, `types_of_support`, `grant_range_stated` | As stated on the site, or `not stated`. |
| `deadlines`, `how_to_apply`, `application_url`, `contact` | As stated on the site. Deadlines go stale; check the page. |
| `page_dated` | Any date on the pages read (copyright year, cycle year, news date). |
| `conflicts_with_return` | Where the site and the latest return disagree on whether or how the funder takes requests. |
| `source_urls` | JSON list of the pages read. |
| `checked_on`, `confidence`, `notes` | When it was read, how sure the reader was and why, anything else worth knowing. |

A website can be out of date or describe one program among several. Treat a profile as a lead to verify on the page itself, not as the funder's final word.

## Index files (`data/index/`)

The tables above are laid out for analysis. `data/index/` holds a second, compact layout of the same facts so that a small read-only HTTP service can answer common questions with a few range requests and no database server. It is built by `pipeline/build_web_index.py`. Only grants with an amount above zero and no amount flag are indexed.

| path | rows | contents |
|---|---|---|
| `index.json` | | Manifest for the index: build date, the tag list with the bit each tag uses, and for every state its county list and its files with row counts and sizes. |
| `funder_directory.parquet` | 258,914 | One row per funder (private foundation or grantmaking public charity): EIN as an integer, `is_pf`, name, city, state, assets, latest giving, latest fiscal year, preselected-only flag, application flag and deadlines, website, top recipient states. Sorted by EIN. Flags use 1 for yes, 0 for no, -1 for unknown; missing amounts are NaN; missing text is an empty string. |
| `funder_profiles/p_NN.parquet` | 258,914 | One row per funder, sorted by EIN, 200 rows per row group, with statistics on `ein`. `profile` is the full `foundations` or `pc_funders` row as JSON text. `top_recipients` (up to 15), `top_tags` (up to 8) and `latest_grants` (up to 25) are precomputed JSON arrays. `index.json` gives the EIN range of each file. |
| `grants/<ST>[_NN].parquet` | 20,104,028 | One row per grant, integers only, sorted by recipient: `funder_id` (EIN as integer), `is_pf`, `fiscal_year`, `amount`, `tag_mask` (one bit per tag, bit numbers in `index.json`), `county_id` (position in the state's county list, -1 for none), `rid` (row number in the state's recipient files), `rk` (number of the normalized recipient name), `purpose_id` (row number in the state's purposes file, -1 for none), `rein` (recipient EIN as integer, reported or matched, -1 for none). |
| `recipient_keys/<ST>.parquet` | 3,925,372 | One row per recipient in the state (normalized name plus city): `recipient_key`, `rk`, and the row range of its grants in `grants/<ST>` (`row_start`, `n_rows`). Sorted by key. |
| `recipient_info/<ST>.parquet` | same | Same rows in the same order: most common spelling of the name, city, most common EIN, number of grants, number of funders, dollars. |
| `purposes/<ST>.parquet` | 2,583,483 | The distinct purpose texts in the state, most frequent first. The row number is the `purpose_id`. |
| `recipient_eins.parquet` | 1,987,660 | Recipient EIN to state, recipient row and row range in `grants/<ST>`, sorted by EIN with statistics on `ein`. |

Every index file is under 20 MB.

## How it was built

1. `pipeline/build.py` downloads each IRS zip file, parses every Form 990-PF and every Form 990 with a Schedule I recipient table, and writes parquet parts. It is resumable.
2. `pipeline/consolidate.py` removes duplicates and builds every table in a DuckDB database, matches recipients to the Business Master File, assigns counties and applies `taxonomy.json`.
3. `pipeline/qa.py` writes a quality report. `pipeline/export.py` writes the parquet files in `data/`. `pipeline/build_web_index.py` writes `data/index/`. `pipeline/make_manifest.py` writes `data/MANIFEST.json`.

Rules for duplicates: a return that appears in more than one zip file is counted once (by object ID); one Form 990-PF is kept per EIN and period end, and the latest filing timestamp wins, so amended returns replace originals; for Form 990 the highest object ID wins. Grants, officers and application rows are kept only for the surviving returns.

The 81 source zip files total about 34 GB. The scripts expect to sit together in one working folder. See `pipeline/README.md`.

## Limitations

- Source is IRS e-filed returns only (Form 990 series XML zips for 2018 to 2026). Paper-filed returns are not included. Private foundations were required to e-file for tax years beginning after July 1, 2019, so fiscal years before 2020 are incomplete.
- Data lags: a return is usually filed 5 to 11 months after the fiscal year ends and released by the IRS some months later. The most recent fiscal year is partial.
- Foundations found only in the IRS Business Master File (efile_missing = true) have no return data here: no grants, officers or application details.
- Grant purpose text is whatever the filer typed. Much of it is generic (for example GENERAL SUPPORT). Subject tags are keyword matches and will contain false positives and misses.
- Form 990-PF grant lists do not carry recipient EINs. recipient_ein_matched is a name and location match to the Business Master File that accepts only unique matches; it can still be wrong, and most unmatched recipients are simply not matched, not absent from the IRS list.
- Public charity grants (Schedule I) include donor-advised fund sponsors and community foundations. Those grants show the sponsor as funder; the advising donor is not disclosed.
- Schedule I lists only grants to domestic organizations and governments above 5,000 dollars per recipient. Grants to individuals and foreign grants are not in grants_all.
- Some foundations attach their grant list as a PDF or statement instead of structured data; those returns show a reported total with few or no listed grants.
- Amounts are as reported. Rows with negative or implausible amounts are kept and marked amount_flag = true; profile aggregates exclude them.
- ZIP to county uses Census 2020 ZCTA relationships (largest land overlap). ZIP codes with no ZCTA (mostly PO boxes) take the dominant county of their city as seen in the Business Master File when at least 80 percent of that city's organizations sit in one county; otherwise the county is blank.
- Transfers between related foundations (for example Gates Foundation Trust to Gates Foundation) and in-kind patient assistance programs are reported as grants and are included as reported, so national dollar totals double count some money.
- Some returns list a placeholder instead of real recipients (SEE ATTACHED, VARIOUS, SEE STATEMENT). Those rows are kept as reported and cannot be attributed to a recipient.
- Recipient state comes from the address on the return. When the state field was blank or invalid it was filled from the ZIP code; foreign and unknown locations are null (XX in the exports).
- The website field on Form 990-PF is often N/A or a third-party site (GuideStar, Candid, a state charity registry). Those values are nulled; the raw text is not published here.
- One return is kept per EIN and period end (latest filing timestamp wins), so amended returns replace originals.
- The published `taxonomy.json` leaves out four organization-name patterns that were used when the tags were built, so re-applying it changes a small number of tags.

## No warranty

This dataset is compiled from public IRS records by automated parsing. It is not an official IRS product and is not endorsed by the IRS. It is provided as is, with no warranty of any kind. It may contain errors from the original filings, from parsing, or from name matching. Do not rely on it as the sole basis for any decision.
