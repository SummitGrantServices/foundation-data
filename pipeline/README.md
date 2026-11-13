# Pipeline

These scripts built the dataset from the public IRS files. They are published for transparency and so that the build can be repeated. They are plain Python 3 with `duckdb`, `pyarrow` and `lxml`.

Run order, from one working folder that holds all the scripts and `taxonomy.json`:

| step | command | what it does |
|---|---|---|
| 1 | `python3 build.py discover` then `python3 build.py run` | Lists the IRS e-file zip files (`zips.json`), downloads each one, parses every Form 990-PF and every Form 990 with a Schedule I recipient table, and writes parquet parts. Resumable: finished zip files are skipped. `python3 build.py status` shows progress. |
| 2 | `python3 consolidate.py` | Removes duplicates and builds every table in `db/fdb.duckdb`. Downloads the IRS Business Master File and the Census ZIP to county file, matches recipients, assigns counties, applies the subject taxonomy. `python3 consolidate.py tags` re-applies `taxonomy.json` after an edit. |
| 3 | `python3 qa.py` | Writes a quality report with completeness against the IRS index files. |
| 4 | `python3 export.py` | Writes the parquet exports (the files published in `data/`). |
| 5 | `python3 build_web_index.py --db db/fdb.duckdb --out data/index` | Writes the compact index files in `data/index/`. Working files go to `./index_work` and are not published. |
| 6 | `python3 make_manifest.py data` | Writes `data/MANIFEST.json`. |

`make_readme.py` writes a schema reference from the database (the schema tables in the top-level README come from it).

Notes:

- The 81 source zip files total about 34 GB. Each one is parsed and then no longer needed.
- The scripts resolve paths from their own folder, so copy them out of `pipeline/` into the working folder before running, together with `taxonomy.json` and `fdb.py`.
- The published `taxonomy.json` leaves out four organization-name patterns that were used in the published build, so a rebuild assigns a small number of tags differently.
- Nothing here needs a password, a key or an account. Every source is a public download.
