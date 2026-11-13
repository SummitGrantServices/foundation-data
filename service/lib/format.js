// format.js : compact plain-text renderings (format=text), close to the output of the fdb.py command-line tool.

const money = v => (v === null || v === undefined || Number.isNaN(v) ? '-' : '$' + Math.round(v).toLocaleString('en-US'))
const pct = v => (v === null || v === undefined ? '-' : Math.round(100 * v) + '%')
const cut = (s, n) => (s || '').slice(0, n)
const pad = (s, n) => String(s).padStart(n)

export function infoText(o) {
  const L = []
  L.push(o.dataset)
  L.push(`build date: ${o.build_date}   index generated: ${o.index_generated}`)
  L.push('', 'ROW COUNTS')
  for (const [k, v] of Object.entries(o.row_counts || {})) L.push(`  ${k.padEnd(22)} ${pad(Number(v).toLocaleString('en-US'), 12)}`)
  L.push('', 'ENDPOINTS (all GET; add format=text for plain text)')
  for (const [k, v] of Object.entries(o.endpoints)) L.push(`  ${k}: ${v}`)
  L.push('', 'TAGS (use the tag name in tags=)')
  for (const t of o.tags) L.push(`  ${t.tag.padEnd(24)} ${t.label.padEnd(42)} ${pad(Number(t.grants).toLocaleString('en-US'), 10)} grants`)
  L.push('', 'STATES', '  ' + o.states.join(' '))
  L.push('', 'NOTES'); for (const n of o.notes) L.push('  - ' + n)
  L.push('', 'CAVEATS'); for (const c of o.caveats || []) L.push('  - ' + c)
  return L.join('\n')
}

export function foundationText(o) {
  if (o.matches) return `No foundation or grantmaking public charity matches "${o.query}"` + (o.state ? ` in ${o.state}` : '') + '.'
  const r = o.profile, ft = o.funder_type, L = []
  L.push(`${r.name}  (EIN ${r.ein}, ${ft.replace('_', ' ')})`)
  L.push(`  ${r.address || ''}, ${r.city || ''}, ${r.state || ''} ${r.zip || ''}   county: ${r.county || '-'}`)
  L.push(`  phone: ${r.phone || '-'}   website: ${r.website || '-'}   NTEE: ${r.ntee_cd || '-'}   in IRS master file: ${r.in_bmf}`)
  if (ft === 'private_foundation') {
    if (r.efile_missing) {
      L.push(`  NO E-FILED RETURN IN THIS DATABASE. Master file only: assets ${money(r.bmf_asset_amt)}, income ${money(r.bmf_income_amt)}, tax period ${r.bmf_tax_period}`)
      return L.join('\n')
    }
    L.push(`  latest return: FY${r.fiscal_year_latest} (period end ${r.period_end_latest})   signer: ${r.signer || '-'}`)
    L.push(`  assets (FMV): ${money(r.fmv_assets_eoy)}   book assets: ${money(r.total_assets_eoy)}   revenue: ${money(r.total_revenue)}   contributions received: ${money(r.contributions_received)}`)
    L.push(`  grants paid (latest): ${money(r.total_grants_paid)}   returns on file: ${r.n_returns} (FY${r.first_fiscal_year} to FY${r.last_fiscal_year})`)
    L.push(`  only gives to preselected organizations (latest return): ${r.only_preselected ? 'YES' : 'no'}   operating foundation: ${r.operating_foundation ? 'yes' : 'no'}`
      + (r.final_return_latest ? '   FINAL RETURN FILED' : '') + (r.flag_amounts_suspect ? '   AMOUNTS FLAGGED AS SUSPECT' : ''))
  } else {
    L.push(`  latest return: FY${r.fiscal_year_latest}   principal officer: ${r.principal_officer || '-'}`)
    L.push(`  total assets: ${money(r.total_assets_eoy)}   revenue: ${money(r.total_revenue)}   grants paid (Part IX): ${money(r.total_grants_paid)}   Schedule I grants listed: ${money(r.grants_listed_latest)} (${r.n_grants_latest} grants)`)
  }
  L.push(`  grant size: median ${money(r.median_grant)}, average ${money(r.avg_grant)}, largest ${money(r.max_grant)}`)
  L.push(`  where the money goes: ${r.top_recipient_states || '-'}   share staying in ${r.state}: ${pct(r.home_state_share)}`)
  if (ft !== 'private_foundation' && r.mission) L.push(`  mission: ${cut(r.mission, 400)}`)
  L.push('', 'GIVING BY FISCAL YEAR')
  for (const y of Object.keys(o.giving_by_year).sort().reverse()) {
    const d = o.giving_by_year[y]
    if (ft === 'private_foundation') L.push(`  FY${y}: ${pad(money(d.giving), 16)}   grants listed: ${d.grants}`)
    else L.push(`  FY${y}: ${pad(money(d.grants_listed), 16)}   grants listed: ${d.grants}   (Part IX total ${money(d.grants_paid_total)})`)
  }
  if (ft === 'private_foundation') {
    L.push('', 'APPLICATION INFORMATION' + (r.app_fiscal_year ? ` (from FY${r.app_fiscal_year} return)` : ''))
    if (r.ever_listed_application_info) {
      for (const [k, lab] of [['app_contact_name', 'contact'], ['app_address', 'address'], ['app_phone', 'phone'], ['app_email', 'email'],
        ['app_form_and_materials', 'form and materials'], ['app_deadlines', 'deadlines'], ['app_restrictions', 'restrictions']]) if (r[k]) L.push(`  ${lab}: ${cut(String(r[k]), 600)}`)
    } else L.push('  none listed on any e-filed return')
    if (r.only_preselected) L.push('  NOTE: latest return checks the box "only makes contributions to preselected charitable organizations and does not accept unsolicited requests".')
    L.push('', 'OFFICERS AND TRUSTEES (latest return)', '  ' + cut(r.officers || '-', 1500))
  }
  if (o.top_tags.length) {
    L.push('', 'SUBJECT MIX (keyword tags, share of listed dollars; a grant can carry several tags)')
    L.push('  ' + o.top_tags.map(t => `${t.tag} ${pct(t.share)}`).join('; '))
  }
  L.push('', 'TOP RECIPIENTS (all years on file)')
  for (const t of o.top_recipients) L.push(`  ${pad(money(t.dollars), 15)}  ${pad(t.grants, 4)} grants  FY${t.first_year}-${t.last_year}  ${t.recipient_name} (${t.city || '-'}, ${t.state || '-'})` + (t.ein ? `  EIN ${t.ein}` : ''))
  L.push('', 'LATEST GRANTS')
  for (const g of o.latest_grants) L.push(`  FY${g.fiscal_year} ${pad(money(g.amount), 13)}  ${g.recipient_name} (${g.recipient_city || '-'}, ${g.recipient_state || '-'})  | ${cut(g.purpose, 90)}` + (g.amount_flag ? '  [FLAGGED]' : ''))
  if (o.other_matches && o.other_matches.length) {
    L.push('', `OTHER NAME MATCHES (${o.total_name_matches} in all; query again with the EIN)`)
    for (const c of o.other_matches) L.push(`  ${c.ein}  ${c.name} (${c.city || '-'}, ${c.state || '-'})  assets ${money(c.assets)}  ${c.funder_type}` + (c.efile_missing ? '  [no e-filed return]' : ''))
  }
  return L.join('\n')
}

export function fundersOfText(o) {
  const L = []
  L.push(`Funders of "${o.query}"` + (o.state ? ` in ${o.state}` : '') + (o.years ? ` (fiscal years ${o.years})` : '') + `: ${o.totals.funders} funders, ${o.totals.grants} grants, ${money(o.totals.dollars)}`)
  for (const n of o.notes || []) L.push('NOTE: ' + n)
  L.push('', `RECIPIENT RECORDS MATCHED (${o.recipients_matched_total} in all; check these are the organization you mean; add state or use the EIN to narrow)`)
  for (const r of o.recipients_matched) L.push(`  ${pad(money(r.dollars), 15)}  ${pad(r.grants, 5)} grants  ${pad(r.funders, 4)} funders  ${r.recipient_name} (${r.city || '-'}, ${r.state || '-'})  EIN ${r.ein || '-'}`)
  L.push('', 'FUNDERS (ranked by dollars)')
  for (const f of o.funders) {
    L.push(`  ${pad(money(f.dollars), 15)}  ${pad(f.grants, 4)} grants  ${f.funder_name} (${f.funder_city || '-'}, ${f.funder_state || '-'})  EIN ${f.funder_ein}  [${f.funder_type === 'private_foundation' ? (f.only_preselected ? 'PF, preselected only' : 'PF') : 'PC'}]`)
    L.push(`                   ${f.by_year}` + (f.typical_purpose ? `  | ${cut(f.typical_purpose, 70)}` : ''))
  }
  return L.join('\n')
}

export function searchText(o) {
  const t = o.totals_before_preselected_filter, L = []
  L.push(`Search ${JSON.stringify(o.filters)}: ${t.funders} funders, ${t.grants} matching grants, ${money(t.dollars)} (before any preselected filter); ${o.funders_after_preselected_filter} funders after it, showing ${o.funders.length}`)
  if (o.counties_matched) L.push('counties matched: ' + o.counties_matched.join(', '))
  o.funders.forEach((r, i) => {
    const flag = r.funder_type === 'public_charity' ? 'PC' : (r.only_preselected ? 'PF, preselected only' : 'PF, accepts requests' + (r.ever_listed_application_info ? ' (application info listed)' : ''))
    L.push('', `${pad(i + 1, 3)}. ${r.funder_name} (${r.funder_city || '-'}, ${r.funder_state || '-'})  EIN ${r.funder_ein}  [${flag}]`)
    L.push(`     matching: ${money(r.dollars)} in ${r.grants} grants to ${r.recipients} recipients, FY${r.first_year}-${r.last_year}, median ${money(r.median_matching_grant)}   | assets ${money(r.assets)}, latest total giving ${money(r.total_giving_latest)}`)
    const extra = [r.website ? 'web: ' + r.website : '', r.top_recipient_states ? 'gives in: ' + r.top_recipient_states : '', r.app_deadlines ? 'deadlines: ' + cut(r.app_deadlines, 80) : ''].filter(Boolean).join('   ')
    if (extra) L.push('     ' + extra)
    for (const e of r.examples || []) L.push(`       FY${e.fy} ${pad(money(e.amount), 12)}  ${e.recipient} (${e.city || '-'})  | ${cut(e.purpose, 80)}`)
  })
  return L.join('\n')
}

export function peersText(o) {
  const L = ['PEER ORGANIZATIONS FOUND AS RECIPIENTS']
  for (const p of o.peers) L.push(`  ${p.ein}  ${p.name} (${p.state || '-'}): ${p.grants} grants from ${p.funders} funders, ${money(p.dollars)}`)
  if (o.peers_with_no_grants_found.length) L.push('  no grants found for: ' + o.peers_with_no_grants_found.join(', ') + '  (990-PF grants only link to an EIN when the name match was unique)')
  L.push('', `FUNDERS SUPPORTING SEVERAL OF THESE PEERS (${o.funders_total} in all, showing ${o.funders.length})`)
  for (const r of o.funders) {
    const flag = r.funder_type === 'public_charity' ? 'PC' : (r.only_preselected ? 'PF, preselected only' : 'PF, accepts requests')
    L.push(`  ${r.peers_funded} peers  ${pad(money(r.dollars), 14)}  ${r.funder_name} (${r.funder_city || '-'}, ${r.funder_state || '-'})  EIN ${r.funder_ein}  [${flag}]  assets ${money(r.assets)}`)
    L.push(`           ${cut(r.peers, 400)}`)
  }
  return L.join('\n')
}
