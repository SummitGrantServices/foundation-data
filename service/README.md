# Foundation data search service

A small read-only HTTP service over the `data/index/` files of the foundation data repository. It is written as Vercel serverless functions (Node.js). It has no database server, no secrets and no write path: every request reads a few parquet files over HTTP with range requests and answers in JSON or plain text.

## Layout

| path | purpose |
|---|---|
| `api/*.js` | One function per endpoint: `foundation`, `funders-of`, `search`, `peers-funders`, `info`. |
| `lib/store.js` | HTTP range reader, in-memory byte cache, parquet decoding with `hyparquet`. |
| `lib/core.js` | The five queries. They mirror the commands of the `fdb.py` command-line tool. |
| `lib/format.js`, `lib/http.js` | Plain-text renderings, request wrapper, cache headers. |
| `public/index.html` | One static page with a search form that calls the endpoints. |
| `public/openapi.json` | OpenAPI description of the endpoints, for clients that need one (for example a custom GPT action). Replace the placeholder server URL after deployment. |
| `local/` | Local harness: a static file server with range support, a plain Node server for the functions, and the test run. Not deployed. |
| `vercel.json` | Function settings (30 second limit; `public/` and `local/` kept out of the function bundle). |

## Configuration

One environment variable:

- `DATA_BASE_URL`: the URL of the repository's `data` folder, with no trailing slash, for example `https://raw.githubusercontent.com/OWNER/REPO/main/data`. The host must answer `Range` requests with status 206.

Optional: `CACHE_BYTES` (bytes of fetched data kept in memory per warm instance, default 192000000).

## Endpoints

All are GET. All return JSON; add `format=text` for compact plain text. Successful answers carry `Cache-Control: public, max-age=3600, s-maxage=86400, stale-while-revalidate=604800`.

| endpoint | parameters |
|---|---|
| `/api/foundation` | `q` (funder EIN or name fragment), optional `state`, `pick` |
| `/api/funders-of` | `q` (recipient EIN, or a name fragment together with `state`), optional `state`, `years`, `limit` |
| `/api/search` | `state` (required), optional `county`, `tags`, `all_tags`, `text`, `years`, `preselected` (yes, no, any), `min_grant`, `funder_type` (pf, pc, any), `sort` (dollars, count), `limit`, `examples` |
| `/api/peers-funders` | `eins` (up to 12 recipient EINs), optional `min_peers`, `years`, `limit` |
| `/api/info` | none |

Work per request is bounded: a search reads one state partition; a recipient name needs a state; a recipient EIN reads at most 12 states; `limit` is capped at 100.

## Run locally

```
npm install
node local/static-server.mjs ../repo 8787                                  # serves the data repository with range support
DATA_BASE_URL=http://127.0.0.1:8787/data node local/server.mjs 8788        # the API and the page on http://127.0.0.1:8788
REPO_DIR=../repo node local/run-tests.mjs                                  # starts both itself and writes TESTS.md
```

## Why hyparquet

The functions decode parquet with `hyparquet`, a pure JavaScript reader (about 0.3 MB, plus 0.16 MB for its compression codecs). Nothing native is loaded, so a cold start costs milliseconds and the deployment is far below the function size limit. zstd pages are decompressed with the zstd built into Node (`node:zlib`, Node 22.15 or later) and fall back to the JavaScript codec on older runtimes. The alternative, `@duckdb/node-api`, would need a native binding of about 96 MB unpacked for Linux x64 plus the `httpfs` extension fetched or bundled at start, and the data files would still need a purpose-built index to answer inside one request.
