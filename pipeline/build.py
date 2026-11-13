#!/usr/bin/env python3
"""
Foundation database builder. Source: IRS Form 990 series e-file XML bulk downloads.
Usage:
  python3 build.py discover            -> writes zips.json (every zip URL per index year)
  python3 build.py run [YEAR ...]      -> downloads each zip, parses every Form 990-PF, writes parquet parts
  python3 build.py status              -> progress table
Resumable: a zip is skipped once parts/<zipname>.done exists. Zips are deleted after parsing.
"""
import sys, os, re, json, csv, time, zipfile, subprocess, traceback, hashlib
from multiprocessing import Pool
from lxml import etree
try:
    import zipfile_deflate64  # enables Deflate64 members in zipfile
except Exception as _e:
    print('WARNING no deflate64 support', _e)
import pyarrow as pa, pyarrow.parquet as pq

BASE = os.path.dirname(os.path.abspath(__file__))
# parts_v3: operating_foundation is read by value (parts_v2 treated presence of the element as true)
PARTS = os.path.join(BASE, 'parts_v3'); TMP = os.path.join(BASE, 'tmp'); LOGS = os.path.join(BASE, 'logs')
for d in (PARTS, TMP, LOGS): os.makedirs(d, exist_ok=True)
ROOT = 'https://apps.irs.gov/pub/epostcard/990/xml'
PAGES = {
    2026: 'https://www.irs.gov/charities-non-profits/form-990-series-downloads',
    2025: 'https://www.irs.gov/charities-non-profits/form-990-series-downloads-2025',
    2024: 'https://www.irs.gov/charities-non-profits/form-990-series-downloads-2024',
    2023: 'https://www.irs.gov/charities-non-profits/form-990-series-downloads-2023',
    2022: 'https://www.irs.gov/charities-non-profits/form-990-series-downloads-2022',
    2021: 'https://www.irs.gov/charities-non-profits/form-990-series-downloads-2021',
    2020: 'https://www.irs.gov/charities-non-profits/form-990-series-downloads-2020',
    2019: 'https://www.irs.gov/charities-non-profits/form-990-series-downloads-2019',
}

def sh(cmd, timeout=3600):
    return subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)

def discover(only_years=None):
    """only_years: probe just these years by file-name pattern and merge them into the existing zips.json
    (used to add 2018, whose zips hold the early-2019 index rows; the IRS page no longer links them)."""
    found = {}
    pages = set() if only_years else set(PAGES.values())
    html_all = ''
    for u in sorted(pages):
        r = sh(f'curl -sSL -m 60 "{u}"', 90)
        html_all += r.stdout
    for m in re.finditer(r'https://apps\.irs\.gov/pub/epostcard/990/xml/(\d{4})/([A-Za-z0-9_\-]+\.zip)', html_all):
        found.setdefault(m.group(1), set()).add(m.group(2))
    # add batches named in the index files
    years = only_years or list(range(2019, 2027))
    for y in years:
        p = os.path.join(BASE, f'idx_{y}.csv')
        if not os.path.exists(p): continue
        with open(p, newline='', encoding='utf-8', errors='replace') as f:
            for row in csv.DictReader(f):
                b = (row.get('XML_BATCH_ID') or '').strip()
                if b: found.setdefault(str(y), set()).add(b + '.zip')
    from concurrent.futures import ThreadPoolExecutor
    for y in years:
        c = found.setdefault(str(y), set())
        for m in range(1, 13):
            for L in 'ABCDEFGH': c.add(f'{y}_TEOS_XML_{m:02d}{L}.zip')
        for n in range(1, 13): c.add(f'download990xml_{y}_{n}.zip')
        for n in range(1, 6): c.add(f'{y}_TEOS_XML_CT{n}.zip')
    def head(args):
        y, n = args
        url = f'{ROOT}/{y}/{n}'
        r = sh(f'curl -sS -m 30 -I "{url}"', 40)
        mlen = re.search(r'content-length:\s*(\d+)', r.stdout, re.I)
        code = re.findall(r'HTTP/\S+\s+(\d+)', r.stdout)
        ctype = re.search(r'content-type:\s*(\S+)', r.stdout, re.I)
        if code and code[-1] == '200' and mlen and int(mlen.group(1)) > 1000 and (not ctype or 'html' not in ctype.group(1)):
            return y, {'name': n, 'url': url, 'bytes': int(mlen.group(1))}
        return y, None
    out = {}
    if only_years:
        found = {y: n for y, n in found.items() if int(y) in only_years}
        out = {y: v for y, v in json.load(open(os.path.join(BASE, 'zips.json'))).items() if int(y) not in only_years}
    with ThreadPoolExecutor(12) as ex:
        for y, d in ex.map(head, [(y, n) for y, names in sorted(found.items()) for n in sorted(names)]):
            if d: out.setdefault(y, []).append(d)
    for y in sorted(out):
        print(y, len(out[y]), 'zips', round(sum(z['bytes'] for z in out[y]) / 1e9, 1), 'GB')
    json.dump(out, open(os.path.join(BASE, 'zips.json'), 'w'), indent=1)

# ---------- parsing ----------
def ln(el):
    t = el.tag
    return t.rsplit('}', 1)[-1] if isinstance(t, str) else ''

def kids(el, name):
    return [c for c in el if ln(c) == name]

def first(el, *names):
    """depth-first: first descendant whose local name is in names"""
    if el is None: return None
    for c in el.iter():
        if ln(c) in names: return c
    return None

def txt(el, *names):
    e = first(el, *names)
    if e is None: return None
    s = ' '.join(''.join(e.itertext()).split())
    return s or None

def num(el, *names):
    s = txt(el, *names)
    if s is None: return None
    try: return float(s)
    except Exception: return None

def name_of(el):
    """person or business name inside a group"""
    if el is None: return None
    for c in el:
        n = ln(c)
        if n.endswith('PersonNm') or n == 'PersonNm' or n.endswith('PersonName'):
            s = ' '.join(''.join(c.itertext()).split())
            if s: return s
        if n.endswith('BusinessName') or n == 'BusinessName':
            s = ' '.join(' '.join(''.join(x.itertext()).split()) for x in c)
            s = ' '.join(s.split())
            if s: return s
    return None

def addr_of(el):
    """returns (line1, city, state, zip, country) from a US or foreign address child"""
    if el is None: return (None,) * 5
    for c in el:
        n = ln(c)
        if n.endswith('USAddress') or n == 'AddressUS':
            return (txt(c, 'AddressLine1Txt', 'AddressLine1'), txt(c, 'CityNm', 'City'),
                    txt(c, 'StateAbbreviationCd', 'State'), txt(c, 'ZIPCd', 'ZIPCode'), 'US')
        if n.endswith('ForeignAddress') or n == 'AddressForeign':
            return (txt(c, 'AddressLine1Txt', 'AddressLine1'), txt(c, 'CityNm', 'City'),
                    txt(c, 'ProvinceOrStateNm', 'ProvinceOrState'), txt(c, 'ForeignPostalCd', 'PostalCode'),
                    txt(c, 'CountryCd', 'Country'))
    return (None,) * 5

def parse_pf(xml_bytes, object_id, zipname):
    root = etree.fromstring(xml_bytes)
    hdr = first(root, 'ReturnHeader'); pf = first(root, 'IRS990PF')
    if pf is None: return None
    rtype = txt(hdr, 'ReturnTypeCd', 'ReturnType')
    filer = first(hdr, 'Filer')
    ein = txt(filer, 'EIN')
    bn = first(filer, 'BusinessName', 'Name')
    name1 = txt(bn, 'BusinessNameLine1Txt', 'BusinessNameLine1') if bn is not None else None
    name2 = txt(bn, 'BusinessNameLine2Txt', 'BusinessNameLine2') if bn is not None else None
    a1, city, st, zp, ctry = addr_of(filer)
    period_end = txt(hdr, 'TaxPeriodEndDt', 'TaxPeriodEndDate')
    period_begin = txt(hdr, 'TaxPeriodBeginDt', 'TaxPeriodBeginDate')
    off = first(hdr, 'BusinessOfficerGrp', 'Officer')
    supp = first(pf, 'SupplementaryInformationGrp', 'SupplementaryInformation')
    stmts = first(pf, 'StatementsRegardingActyGrp', 'StatementsRegardingActivities')
    rev = first(pf, 'AnalysisOfRevenueAndExpenses')
    bal = first(pf, 'Form990PFBalanceSheetsGrp', 'BalanceSheets')
    base = {'object_id': object_id, 'ein': ein, 'period_end': period_end}

    grants = []
    def add_grants(tagnames, kind):
        if supp is None: return
        seq = 0
        for g in supp.iter():
            if ln(g) in tagnames:
                seq += 1
                ga1, gcity, gst, gzip, gctry = addr_of(g)
                grants.append(dict(base, seq=seq, kind=kind, recipient_name=name_of(g),
                    recipient_addr1=ga1, recipient_city=gcity, recipient_state=gst, recipient_zip=gzip,
                    recipient_country=gctry,
                    relationship=txt(g, 'RecipientRelationshipTxt', 'RecipientRelationship'),
                    foundation_status=txt(g, 'RecipientFoundationStatusTxt', 'RecipientFoundationStatus'),
                    purpose=txt(g, 'GrantOrContributionPurposeTxt', 'PurposeOfGrantOrContribution'),
                    amount=num(g, 'Amt', 'Amount')))
    add_grants(('GrantOrContributionPdDurYrGrp', 'GrantOrContriPaidDuringYear'), 'paid')
    add_grants(('GrantOrContriApprvForFutGrp', 'GrantOrContriApprovedForFuture'), 'future')

    officers = []
    og = first(pf, 'OfficerDirTrstKeyEmplInfoGrp', 'OfcrDirTrusteesKeyEmployeeInfo')
    if og is not None:
        for o in og.iter():
            if ln(o) in ('OfficerDirTrstKeyEmplGrp', 'OfcrDirTrusteesOrKeyEmployee'):
                oa1, ocity, ost, ozip, octry = addr_of(o)
                officers.append(dict(base, name=name_of(o), title=txt(o, 'TitleTxt', 'Title'),
                    hours=num(o, 'AverageHrsPerWkDevotedToPosRt', 'AvgHoursPerWkDevotedToPosition'),
                    compensation=num(o, 'CompensationAmt', 'Compensation'),
                    city=ocity, state=ost))

    apps = []
    if supp is not None:
        for a in supp.iter():
            if ln(a) in ('ApplicationSubmissionInfoGrp', 'ApplicationSubmissionInformation'):
                aa1, acity, ast, azip, actry = addr_of(a)
                apps.append(dict(base, contact_name=name_of(a), addr1=aa1, city=acity, state=ast, zip=azip,
                    phone=txt(a, 'RecipientPhoneNum', 'RecipientPhone', 'RecipientForeignPhoneNum'),
                    email=txt(a, 'RecipientEmailAddressTxt', 'RecipientEmailAddress'),
                    form_and_materials=txt(a, 'FormAndInfoAndMaterialsTxt', 'FormAndInfoAndMaterials'),
                    deadlines=txt(a, 'SubmissionDeadlinesTxt', 'SubmissionDeadlines'),
                    restrictions=txt(a, 'RestrictionsOnAwardsTxt', 'RestrictionsOnAwards')))

    activities = []
    da = first(pf, 'SummaryOfDirectChrtblActyGrp', 'SummaryOfDirectCharitableActy')
    if da is not None:
        for c in da:
            if ln(c).startswith('Description'):
                s = ' '.join(''.join(c.itertext()).split())
                if s: activities.append(s)

    paid_amts = [g['amount'] for g in grants if g['kind'] == 'paid' and g['amount'] is not None]
    ret = dict(base, return_type=rtype, name=name1, name2=name2, period_begin=period_begin,
        tax_year=txt(hdr, 'TaxYr', 'TaxYear'), return_ts=txt(hdr, 'ReturnTs', 'Timestamp'),
        amended=1 if first(pf, 'AmendedReturnInd', 'AmendedReturn') is not None else 0,
        final_return=1 if first(pf, 'FinalReturnInd', 'FinalReturn') is not None else 0,
        addr1=a1, city=city, state=st, zip=zp, country=ctry,
        phone=txt(filer, 'PhoneNum', 'Phone') or txt(hdr, 'PhoneNum'),
        website=txt(stmts, 'WebsiteAddressTxt', 'WebsiteAddress') if stmts is not None else txt(pf, 'WebsiteAddressTxt'),
        signer_name=txt(off, 'PersonNm', 'Name') if off is not None else None,
        signer_title=txt(off, 'PersonTitleTxt', 'Title') if off is not None else None,
        fmv_assets_eoy=num(pf, 'FMVAssetsEOYAmt', 'FMVAssetsEOY'),
        total_assets_eoy=num(bal, 'TotalAssetsEOYAmt', 'TotalAssetsEOY') if bal is not None else None,
        net_assets_eoy=num(bal, 'TotNetAstOrFundBalancesEOYAmt', 'TotalNetAssetsOrFundBalancesEOY') if bal is not None else None,
        total_revenue=num(rev, 'TotalRevAndExpnssAmt', 'TotalRevenueAndExpenses') if rev is not None else None,
        contributions_received=num(rev, 'ContriRcvdRevAndExpnssAmt', 'ContributionsReceived') if rev is not None else None,
        total_expenses=num(rev, 'TotalExpensesRevAndExpnssAmt', 'TotalExpensesRevAndExpenses') if rev is not None else None,
        grants_paid_books=num(rev, 'ContriPaidRevAndExpnssAmt', 'ContriGiftsPaidRevAndExpenses') if rev is not None else None,
        grants_paid_charitable=num(rev, 'ContriPaidDsbrsChrtblAmt', 'ContriGiftsPaidDsbrsChrtblPrps') if rev is not None else None,
        total_grants_paid=num(supp, 'TotalGrantOrContriPdDurYrAmt', 'TotalGrantOrContriPaidDuringYear') if supp is not None else None,
        total_grants_future=num(supp, 'TotalGrantOrContriApprvFutAmt', 'TotalGrantOrContriApprovedFuture') if supp is not None else None,
        only_preselected=1 if (supp is not None and first(supp, 'OnlyContriToPreselectedInd', 'OnlyContriToPreselectedOrgs') is not None) else 0,
        operating_foundation=1 if (txt(pf, 'PrivateOperatingFoundationInd', 'PrivateOperatingFoundation') or '').strip().lower() in ('true', '1', 'x') else 0,
        n_grants_paid=len(paid_amts), sum_grants_paid_listed=float(sum(paid_amts)) if paid_amts else 0.0,
        n_officers=len(officers), n_app_contacts=len(apps),
        direct_charitable_activities=' | '.join(activities)[:4000] if activities else None,
        source_zip=zipname)
    return ret, grants, officers, apps

def parse_990_sched_i(xml_bytes, object_id, zipname):
    root = etree.fromstring(xml_bytes)
    hdr = first(root, 'ReturnHeader'); f990 = first(root, 'IRS990'); si = first(root, 'IRS990ScheduleI')
    if si is None: return None
    filer = first(hdr, 'Filer')
    ein = txt(filer, 'EIN')
    bn = first(filer, 'BusinessName', 'Name')
    name1 = txt(bn, 'BusinessNameLine1Txt', 'BusinessNameLine1') if bn is not None else None
    a1, city, st, zp, ctry = addr_of(filer)
    period_end = txt(hdr, 'TaxPeriodEndDt', 'TaxPeriodEndDate')
    base = {'object_id': object_id, 'ein': ein, 'period_end': period_end}
    grants = []
    seq = 0
    for g in si:
        if ln(g) != 'RecipientTable': continue
        seq += 1
        ga1, gcity, gst, gzip, gctry = addr_of(g)
        grants.append(dict(base, seq=seq, recipient_name=name_of(g), recipient_ein=txt(g, 'RecipientEIN', 'EINOfRecipient'),
            recipient_addr1=ga1, recipient_city=gcity, recipient_state=gst, recipient_zip=gzip, recipient_country=gctry,
            irc_section=txt(g, 'IRCSectionDesc', 'IRCSection'),
            purpose=txt(g, 'PurposeOfGrantTxt', 'PurposeOfGrant'),
            cash_amount=num(g, 'CashGrantAmt', 'AmountOfCashGrant'),
            noncash_amount=num(g, 'NonCashAssistanceAmt', 'AmountOfNonCashAssistance')))
    if not grants: return None
    def f(*names): return txt(f990, *names) if f990 is not None else None
    def fn(*names): return num(f990, *names) if f990 is not None else None
    mission = f('ActivityOrMissionDesc', 'MissionDesc', 'ActivityOrMissionDescription')
    ret = dict(base, name=name1, tax_year=txt(hdr, 'TaxYr', 'TaxYear'), addr1=a1, city=city, state=st, zip=zp,
        phone=txt(filer, 'PhoneNum', 'Phone'), website=f('WebsiteAddressTxt', 'WebsiteAddress'),
        mission=mission[:2000] if mission else None,
        principal_officer=f('PrincipalOfficerNm', 'NameOfPrincipalOfficerPerson'),
        total_revenue=fn('CYTotalRevenueAmt', 'TotalRevenueCurrentYear'),
        total_assets_eoy=fn('TotalAssetsEOYAmt', 'TotalAssetsEOY'),
        grants_paid_total=fn('CYGrantsAndSimilarPaidAmt', 'GrantsAndSimilarAmntsCY'),
        n_grants_listed=len(grants), sum_grants_listed=float(sum((g['cash_amount'] or 0) + (g['noncash_amount'] or 0) for g in grants)),
        source_zip=zipname)
    return ret, grants

def _work(args):
    zpath, names, zipname, ci = args
    out = {'returns': [], 'grants': [], 'officers': [], 'apps': [], 'filers990': [], 'grants990': []}
    errors = []; counts = {}
    with zipfile.ZipFile(zpath) as z:
        for n in names:
            oid = os.path.basename(n).split('_')[0]
            try:
                data = z.read(n)
                if b'<IRS990PF' in data:
                    r = parse_pf(data, oid, zipname)
                    if r is None: continue
                    out['returns'].append(r[0]); out['grants'] += r[1]; out['officers'] += r[2]; out['apps'] += r[3]
                elif b'<IRS990ScheduleI' in data and b'RecipientTable' in data:
                    r = parse_990_sched_i(data, oid, zipname)
                    if r is None: continue
                    out['filers990'].append(r[0]); out['grants990'] += r[1]
            except Exception as e:
                errors.append((n, repr(e)[:200]))
    for kind, rows in out.items():
        counts[kind] = len(rows)
        if rows: write_part(kind, rows, f'{zipname}__c{ci}')
    return counts, errors

SCHEMAS = {
 'returns': [('object_id','s'),('ein','s'),('period_end','s'),('return_type','s'),('name','s'),('name2','s'),('period_begin','s'),
   ('tax_year','s'),('return_ts','s'),('amended','i'),('final_return','i'),('addr1','s'),('city','s'),('state','s'),('zip','s'),('country','s'),
   ('phone','s'),('website','s'),('signer_name','s'),('signer_title','s'),('fmv_assets_eoy','f'),('total_assets_eoy','f'),('net_assets_eoy','f'),
   ('total_revenue','f'),('contributions_received','f'),('total_expenses','f'),('grants_paid_books','f'),('grants_paid_charitable','f'),
   ('total_grants_paid','f'),('total_grants_future','f'),('only_preselected','i'),('operating_foundation','i'),('n_grants_paid','i'),
   ('sum_grants_paid_listed','f'),('n_officers','i'),('n_app_contacts','i'),('direct_charitable_activities','s'),('source_zip','s')],
 'grants': [('object_id','s'),('ein','s'),('period_end','s'),('seq','i'),('kind','s'),('recipient_name','s'),('recipient_addr1','s'),
   ('recipient_city','s'),('recipient_state','s'),('recipient_zip','s'),('recipient_country','s'),('relationship','s'),
   ('foundation_status','s'),('purpose','s'),('amount','f')],
 'officers': [('object_id','s'),('ein','s'),('period_end','s'),('name','s'),('title','s'),('hours','f'),('compensation','f'),('city','s'),('state','s')],
 'apps': [('object_id','s'),('ein','s'),('period_end','s'),('contact_name','s'),('addr1','s'),('city','s'),('state','s'),('zip','s'),
   ('phone','s'),('email','s'),('form_and_materials','s'),('deadlines','s'),('restrictions','s')],
 'filers990': [('object_id','s'),('ein','s'),('period_end','s'),('name','s'),('tax_year','s'),('addr1','s'),('city','s'),('state','s'),('zip','s'),
   ('phone','s'),('website','s'),('mission','s'),('principal_officer','s'),('total_revenue','f'),('total_assets_eoy','f'),('grants_paid_total','f'),
   ('n_grants_listed','i'),('sum_grants_listed','f'),('source_zip','s')],
 'grants990': [('object_id','s'),('ein','s'),('period_end','s'),('seq','i'),('recipient_name','s'),('recipient_ein','s'),('recipient_addr1','s'),
   ('recipient_city','s'),('recipient_state','s'),('recipient_zip','s'),('recipient_country','s'),('irc_section','s'),('purpose','s'),
   ('cash_amount','f'),('noncash_amount','f')],
}
PT = {'s': pa.string(), 'i': pa.int32(), 'f': pa.float64()}

def write_part(kind, rows, zipname):
    sch = pa.schema([(n, PT[t]) for n, t in SCHEMAS[kind]])
    cols = {n: [r.get(n) for r in rows] for n, _ in SCHEMAS[kind]}
    tbl = pa.Table.from_pydict(cols, schema=sch)
    p = os.path.join(PARTS, f'{kind}__{zipname}.parquet')
    pq.write_table(tbl, p + '.tmp', compression='zstd'); os.replace(p + '.tmp', p)

def pf_ids(year):
    ids = set()
    p = os.path.join(BASE, f'idx_{year}.csv')
    with open(p, newline='', encoding='utf-8', errors='replace') as f:
        for row in csv.DictReader(f):
            if (row.get('RETURN_TYPE') or '').strip().upper() in ('990PF', '990PA'):
                ids.add((row.get('OBJECT_ID') or '').strip())
    return ids

def process_zip(year, z, ids, procs=2):
    zipname = z['name'][:-4]
    done = os.path.join(PARTS, zipname + '.done')
    if os.path.exists(done):
        try:
            i = json.load(open(done))
            if i.get('errors', 0) <= 0.02 * max(1, i.get('members', 1)): return
        except Exception: pass
    t0 = time.time()
    zpath = os.path.join(TMP, z['name'])
    for attempt in range(6):
        r = sh(f'curl -sS -L --retry 5 --retry-delay 5 -C - -o "{zpath}" "{z["url"]}"', 7200)
        if os.path.exists(zpath) and os.path.getsize(zpath) == z['bytes']: break
        time.sleep(10)
    else:
        raise RuntimeError(f'download failed {z["name"]} {r.stderr[-300:]}')
    t1 = time.time()
    with zipfile.ZipFile(zpath) as zf:
        infos = [i for i in zf.infolist() if i.filename.lower().endswith('.xml')]
    infos.sort(key=lambda i: -i.file_size)
    nch = max(procs * 6, len(infos) // 15000)  # bound rows held in memory per chunk for the very large yearly zips
    chunks = [[i.filename for i in infos[k::nch]] for k in range(nch)]
    tot = {}; errs = []
    with Pool(procs, maxtasksperchild=2) as pool:
        for counts, e in pool.imap_unordered(_work, [(zpath, c, zipname, k) for k, c in enumerate(chunks) if c]):
            for k, v in counts.items(): tot[k] = tot.get(k, 0) + v
            errs += e
    os.remove(zpath)
    info = dict({'zip': zipname, 'year': year, 'members': len(infos), 'errors': len(errs), 'error_samples': errs[:5],
            'download_s': round(t1 - t0), 'parse_s': round(time.time() - t1)}, **tot)
    json.dump(info, open(done, 'w'))
    print(json.dumps(info), flush=True)

def run(years):
    zips = json.load(open(os.path.join(BASE, 'zips.json')))
    for y in years:
        ids = pf_ids(y)
        for z in sorted(zips.get(str(y), []), key=lambda d: d['name'], reverse=True):
            try:
                process_zip(y, z, ids)
            except Exception as e:
                print('FAILED', z['name'], repr(e)[:300], flush=True)
                open(os.path.join(LOGS, 'failed.txt'), 'a').write(f"{z['name']}\t{repr(e)[:300]}\n")

def status():
    zips = json.load(open(os.path.join(BASE, 'zips.json')))
    tot = {'returns': 0, 'grants': 0}
    for y in sorted(zips, reverse=True):
        d = [z for z in zips[y] if os.path.exists(os.path.join(PARTS, z['name'][:-4] + '.done')) and json.load(open(os.path.join(PARTS, z['name'][:-4] + '.done'))).get('errors',0) <= 0.02*max(1,json.load(open(os.path.join(PARTS, z['name'][:-4] + '.done'))).get('members',1))]
        r = g = 0
        for z in d:
            i = json.load(open(os.path.join(PARTS, z['name'][:-4] + '.done'))); r += i.get('returns',0); g += i.get('grants',0) + i.get('grants990',0)
        tot['returns'] += r; tot['grants'] += g
        print(f"{y}: {len(d)}/{len(zips[y])} zips done, {r} returns, {g} grants")
    print('TOTAL', tot)

if __name__ == '__main__':
    cmd = sys.argv[1] if len(sys.argv) > 1 else 'status'
    if cmd == 'discover': discover([int(a) for a in sys.argv[2:]] or None)
    elif cmd == 'run': run([int(a) for a in sys.argv[2:]] or [2026, 2025, 2024, 2023, 2022, 2021, 2020, 2019])
    else: status()
