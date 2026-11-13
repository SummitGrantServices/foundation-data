#!/usr/bin/env python3
"""make_manifest.py : write data/MANIFEST.json, the list of every published data file with bytes, rows and SHA-256.

  python3 pipeline/make_manifest.py [data_dir]      (default: ./data)
"""
import os, sys, json, hashlib, time
import pyarrow.parquet as pq

def sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''): h.update(chunk)
    return h.hexdigest()

def main():
    root = sys.argv[1] if len(sys.argv) > 1 else 'data'
    files = []
    for d, _, names in os.walk(root):
        for n in sorted(names):
            if n == 'MANIFEST.json' or n.startswith('.'): continue
            p = os.path.join(d, n); rel = os.path.relpath(p, root).replace(os.sep, '/')
            e = {'file': rel, 'bytes': os.path.getsize(p), 'sha256': sha256(p)}
            if n.endswith('.parquet'): e['rows'] = pq.ParquetFile(p).metadata.num_rows
            files.append(e)
    files.sort(key=lambda e: e['file'])
    def group(e):
        parts = e['file'].split('/')
        if parts[0] == 'index': return 'index/' + (parts[1] if len(parts) > 2 else parts[1].split('.')[0])
        return parts[0] if len(parts) > 1 else parts[0].split('.')[0]
    tables = {}
    for e in files:
        t = tables.setdefault(group(e), {'files': 0, 'bytes': 0, 'rows': 0})
        t['files'] += 1; t['bytes'] += e['bytes']; t['rows'] += e.get('rows', 0)
    build = None
    try:
        import duckdb
        build = duckdb.connect().execute(f"SELECT value FROM read_parquet('{root}/meta.parquet') WHERE key = 'build_date'").fetchone()[0]
    except Exception: pass
    man = {'generated': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()), 'database_build_date': build, 'compression': 'zstd',
           'n_files': len(files), 'total_bytes': sum(e['bytes'] for e in files), 'max_file_bytes': max(e['bytes'] for e in files),
           'tables': tables, 'files': files}
    with open(os.path.join(root, 'MANIFEST.json'), 'w') as f: json.dump(man, f, indent=1)
    print(f"{man['n_files']} files, {man['total_bytes'] / 1e6:.1f} MB, largest {man['max_file_bytes'] / 1e6:.1f} MB")

if __name__ == '__main__':
    main()
