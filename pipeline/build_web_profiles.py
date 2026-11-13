#!/usr/bin/env python3
"""build_web_profiles.py : merge website-read funder profiles into the published data set.

Input : a folder of JSON Lines files, one object per funder, produced by reading each funder's OWN website
        (never an aggregator). Fields: ein, name, status, website, website_source, accepts_unsolicited,
        accepts_evidence, priorities, geographic_limits, eligibility, not_funded, types_of_support,
        grant_range_stated, deadlines, how_to_apply, application_url, contact, page_dated,
        conflicts_with_return, source_urls, checked_on, confidence, notes.
Output: data/funder_web_profiles.parquet      one row per funder (lists stored as JSON text)
        data/web_profiles/NN.json             NN = first two digits of the 9-digit EIN; {ein: profile}
        data/web_profiles/index.json          counts, date range, shard list

One funder can appear in several input files (a rerun after a failed first pass). The best row wins:
profile > ambiguous identity > site unreachable > no website found > not checked; ties go to the later file.
Rows with status "not checked" carry no information and are left out.

Usage: python3 pipeline/build_web_profiles.py <input folder> [data folder]
"""
import glob, json, os, sys
from collections import Counter, defaultdict

RANK = {'profile': 5, 'ambiguous identity': 4, 'site unreachable': 3, 'no website found': 2, 'not checked': 1}
FIELDS = ['ein', 'name', 'status', 'website', 'website_source', 'accepts_unsolicited', 'accepts_evidence', 'priorities',
          'geographic_limits', 'eligibility', 'not_funded', 'types_of_support', 'grant_range_stated', 'deadlines',
          'how_to_apply', 'application_url', 'contact', 'page_dated', 'conflicts_with_return', 'source_urls',
          'checked_on', 'confidence', 'notes']
LISTS = {'priorities', 'source_urls'}


def clean(v):
    if v is None:
        return ''
    if isinstance(v, (list, dict)):
        return v
    return str(v).strip()


def main():
    src = sys.argv[1]
    data = sys.argv[2] if len(sys.argv) > 2 else os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'data')
    best, bad = {}, 0
    for order, path in enumerate(sorted(glob.glob(os.path.join(src, '*.jsonl')))):
        for ln in open(path, encoding='utf-8'):
            ln = ln.strip()
            if not ln:
                continue
            try:
                r = json.loads(ln)
            except ValueError:
                bad += 1
                continue
            ein = ''.join(ch for ch in str(r.get('ein', '')) if ch.isdigit()).zfill(9)
            if len(ein) != 9 or r.get('status') not in RANK:
                bad += 1
                continue
            row = {k: clean(r.get(k)) for k in FIELDS}
            row['ein'] = ein
            for k in LISTS:
                v = row[k]
                row[k] = [str(x).strip() for x in v if str(x).strip()] if isinstance(v, list) else ([v] if v else [])
            key = (RANK[row['status']], order)
            if ein not in best or key >= best[ein][0]:
                best[ein] = (key, row)
    rows = [v[1] for _, v in sorted(best.items()) if v[1]['status'] != 'not checked']
    left_out = sum(1 for v in best.values() if v[1]['status'] == 'not checked')

    out_dir = os.path.join(data, 'web_profiles')
    os.makedirs(out_dir, exist_ok=True)
    shards = defaultdict(dict)
    for r in rows:
        shards[r['ein'][:2]][r['ein']] = r
    for nn, obj in shards.items():
        with open(os.path.join(out_dir, f'{nn}.json'), 'w', encoding='utf-8') as f:
            json.dump(obj, f, ensure_ascii=False, separators=(',', ':'), sort_keys=True)
    dates = sorted(r['checked_on'] for r in rows if r['checked_on'])
    index = {
        'what': "Profiles read from each funder's own website. Not IRS data. Each profile carries the pages it was read from and the date.",
        'funders': len(rows), 'by_status': dict(Counter(r['status'] for r in rows)),
        'checked_from': dates[0] if dates else None, 'checked_to': dates[-1] if dates else None,
        'shards': sorted(shards),
    }
    with open(os.path.join(out_dir, 'index.json'), 'w', encoding='utf-8') as f:
        json.dump(index, f, indent=1)

    import pyarrow as pa, pyarrow.parquet as pq
    cols = {k: [json.dumps(r[k], ensure_ascii=False) if k in LISTS else r[k] for r in rows] for k in FIELDS}
    pq.write_table(pa.table(cols), os.path.join(data, 'funder_web_profiles.parquet'), compression='zstd')
    print(json.dumps({**index, 'unreadable_lines': bad, 'left_out_not_checked': left_out}, indent=1))


if __name__ == '__main__':
    main()
