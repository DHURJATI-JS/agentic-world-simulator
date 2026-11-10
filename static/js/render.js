/* Renderers for stored AI insights and shared UI fragments. */
(function () {
  'use strict';
  const { esc, safeUrl, fmtDateTime, timeAgo, pct, chip, SEVERITY_STATUS } = window.U;

  const KIND = {
    daily_brief: 'Daily global brief', market_outlook: 'Market outlook', region_analysis: 'Region analysis',
    geo_impact: 'Geographic impact', risk_alert: 'Risk alert', persona_pulse: 'How people react',
    simulation_report: 'Simulation report',
  };
  const REQUIRES = {
    none: 'any startup', physical_products: 'sells products', shipping_domestic: 'ships domestically',
    shipping_international: 'ships internationally', services: 'offers services',
  };
  const NEED_ICON = { critical: 'critical', high: 'serious', medium: 'warning' };

  function kindLabel(kind) { return KIND[kind] || kind; }
  function severityChip(sev) { return sev && sev !== 'info' ? chip(sev, SEVERITY_STATUS[sev] || '') : ''; }
  function list(items) {
    const arr = (items || []).filter(Boolean);
    return arr.length ? `<ul class="bullets">${arr.map((x) => `<li>${esc(x)}</li>`).join('')}</ul>` : '<p class="muted small">–</p>';
  }
  function section(title, html) { return html ? `<div class="section"><h3>${esc(title)}</h3>${html}</div>` : ''; }

  function insightCard(i, { compact = false } = {}) {
    return `<div class="card clickable" data-insight="${i.id}">
      <div class="meta">${chip(kindLabel(i.kind), 'info')}${i.region && i.region !== 'GLOBAL' ? chip(i.region) : ''}${severityChip(i.severity)}
        <span title="${esc(fmtDateTime(i.created_at))}">${esc(fmtDateTime(i.created_at))}</span></div>
      <h4 style="margin-top:6px">${esc(i.title)}</h4>
      ${compact ? '' : `<div class="small muted">${esc((i.summary || '').slice(0, 220))}${(i.summary || '').length > 220 ? '…' : ''}</div>`}
      ${i.provider ? `<div class="small muted" style="margin-top:4px">by ${esc(i.provider)}</div>` : ''}
    </div>`;
  }

  function recommendations(recs, hidden) {
    let html = (recs || []).map((r) => `<div class="rec ${esc(r.priority)}">
        <div class="row"><strong class="grow">${esc(r.title)}</strong>${chip(r.priority)}${r.requires !== 'none' ? chip(REQUIRES[r.requires] || r.requires) : ''}</div>
        <div class="small" style="margin-top:3px">${esc(r.detail)}</div></div>`).join('');
    if (!html) html = '<p class="muted small">No recommendations.</p>';
    if (hidden && hidden.length) {
      html += `<details class="small" style="margin-top:6px"><summary class="muted">${hidden.length} suggestion(s) hidden because they don't fit your startup's capabilities</summary>
        ${hidden.map((r) => `<div class="rec low" style="opacity:.75"><strong>${esc(r.title)}</strong><div>${esc(r.hidden_reason || '')}</div></div>`).join('')}</details>`;
    }
    return html;
  }

  function scenarios(items) {
    if (!items || !items.length) return '';
    const colors = { bear: 'var(--series-8)', base: 'var(--text-muted)', bull: 'var(--series-1)' };
    return items.map((s) => `<div class="scen">
        <div class="row"><strong class="grow">${esc((s.name || '').toUpperCase())} · ${Math.round((s.probability || 0) * 100)}%</strong>
          <span class="num">${esc(pct(s.expected_move_pct, 1))}</span></div>
        <div class="bar"><i style="width:${Math.round((s.probability || 0) * 100)}%;background:${colors[s.name] || 'var(--accent)'}"></i></div>
        <div class="small muted" style="margin-top:3px">${esc(s.description)}</div></div>`).join('');
  }

  function quant(q) {
    if (!q) return '';
    const mc = q.monte_carlo, abm = q.abm;
    const tiles = [];
    if (mc) {
      tiles.push(tile('Monte Carlo median', pct(mc.median_pct, 1), `${mc.horizon_days} trading days`));
      tiles.push(tile('P(fall > 5%)', `${Math.round(mc.prob_down_5 * 100)}%`, `P(rise > 5%) ${Math.round(mc.prob_up_5 * 100)}%`));
    }
    if (abm) {
      tiles.push(tile('Agent model median', pct(abm.median_change_pct, 1), `p10 ${pct(abm.p10_change_pct, 1)} · p90 ${pct(abm.p90_change_pct, 1)}`));
      tiles.push(tile('Simulated consumer panic', `${Math.round(abm.consumer_panic_peak * 100)}%`, abm.calibrated ? 'calibrated to the real index' : 'default parameters'));
    }
    return tiles.length ? `<div class="tiles">${tiles.join('')}</div>` : '';
  }

  function tile(label, value, sub) {
    return `<div class="tile"><div class="label">${esc(label)}</div><div class="value">${esc(value)}</div>${sub ? `<div class="sub">${esc(sub)}</div>` : ''}</div>`;
  }

  function sources(src) {
    if (!src || !src.length) return '';
    return section('Sources', `<ul class="clean small">${src.map((s) => `<li style="margin:3px 0"><a href="${safeUrl(s.url)}" target="_blank" rel="noopener noreferrer">${esc(s.title)}</a> <span class="muted">· ${esc(s.outlet)}</span></li>`).join('')}</ul>`);
  }

  function body(i) {
    const c = i.content || {};
    let html = '';
    switch (i.kind) {
      case 'daily_brief':
        html += `<p>${esc(i.summary)}</p>`;
        if (c.global_mood != null) html += `<p class="small muted">Global mood ${c.global_mood > 0 ? '+' : ''}${Number(c.global_mood).toFixed(2)} · ${c.headline_count || 0} headlines from ${esc((c.sources || []).join(', '))}</p>`;
        html += section('Key themes', (c.themes || []).map((t) => `<div class="card soft"><h4>${esc(t.title)}</h4>
            <div class="meta">${(t.regions || []).map((r) => chip(r)).join('')}<span>tone ${Number(t.sentiment || 0).toFixed(2)}</span></div>
            <div class="small" style="margin-top:4px">${esc(t.summary)}</div>
            ${t.market_impact ? `<div class="small muted" style="margin-top:4px"><strong>Market impact:</strong> ${esc(t.market_impact)}</div>` : ''}</div>`).join(''));
        html += section('Market implications', list(c.market_implications));
        html += section('Watchlist', list(c.watchlist));
        break;
      case 'market_outlook':
      case 'region_analysis':
        html += `<p>${esc(c.situation || i.summary)}</p>`;
        if (c.trigger) html += `<p class="small muted">Trigger: ${esc(c.trigger)}</p>`;
        html += quant(c.quant);
        html += section(`Outlook (next ~${c.horizon_days || 20} trading days)`, scenarios(c.scenarios));
        html += section('Key stories', c.key_stories ? list(c.key_stories) : '');
        html += section('Drivers', list(c.drivers));
        html += section('How consumers are reacting', c.consumer_reaction ? `<p class="small">${esc(c.consumer_reaction)}</p>` : '');
        html += section('How investors are reacting', c.investor_reaction ? `<p class="small">${esc(c.investor_reaction)}</p>` : '');
        html += section('Risks', c.risks ? list(c.risks) : '');
        html += section('Opportunities', c.opportunities ? list(c.opportunities) : '');
        html += section('Signals to watch', c.signals_to_watch ? list(c.signals_to_watch) : '');
        html += section('Impact on your startup', c.impact_on_startup ? `<p class="small">${esc(c.impact_on_startup)}</p>` : '');
        if (c.confidence != null) html += `<p class="small muted">Model confidence ${Math.round(c.confidence * 100)}% · estimates, not financial advice.</p>`;
        break;
      case 'geo_impact': {
        const ev = c.event || {};
        html += `<p>${esc(i.summary)}</p>`;
        html += `<div class="meta">${chip(ev.kind || 'event')}${ev.magnitude ? chip('M' + Number(ev.magnitude).toFixed(1)) : ''}${ev.alert_level ? chip(ev.alert_level + ' alert', window.U.ALERT_STATUS[ev.alert_level] || '') : ''}<span>${esc(fmtDateTime(ev.occurred_at))}</span></div>`;
        const mm = c.market_moves || {};
        const fit = { ship_supplies: 'Can ship relief supplies', offer_services: 'Can offer services', insights_only: 'Market insights only (no shipping)' }[c.fit] || '';
        html += `<div class="tiles" style="margin-top:10px">${tile('Your startup can', fit)}
          ${tile(mm.index_name || 'Local index', mm.index != null ? pct(mm.index) : 'n/a', 'since the event')}
          ${tile(mm.fx_name || 'Currency', mm.fx != null ? pct(mm.fx) : 'n/a', 'since the event')}</div>`;
        html += section('What people need now', (c.humanitarian_needs || []).length ? `<table class="data"><thead><tr><th>Need</th><th>Category</th><th>Urgency</th></tr></thead><tbody>
          ${c.humanitarian_needs.map((n) => `<tr><td>${esc(n.item)}<div class="muted">${esc(n.why)}</div></td><td>${esc(n.category)}</td><td>${chip(n.urgency, NEED_ICON[n.urgency] || '')}</td></tr>`).join('')}</tbody></table>` : '');
        const mi = c.market_impact || {};
        html += section('Market impact', `<p class="small">${esc(mi.local_market || '')}</p><p class="small"><strong>Currency:</strong> ${esc(mi.currency || '–')}</p>
          <div class="grid-2 small"><div><strong>Sectors up</strong>${list(mi.sectors_up)}</div><div><strong>Sectors down</strong>${list(mi.sectors_down)}</div></div>
          <p class="small muted">Expected duration: ${esc(mi.expected_duration || '–')}</p>`);
        html += section('Supply-chain risks', list(c.supply_chain_risks));
        const sim = c.simulation;
        if (sim) {
          const g = sim.genuine || {}, p = sim.promotional || {};
          html += section('Simulated effect of helping (vs. not helping, same seed)', `<div class="tiles">
            ${tile('Awareness there (genuine)', pct(g.awareness_pp_region, 1).replace('%', 'pp'), 'percentage points')}
            ${tile('Customer share there', pct(g.share_pp_region, 2).replace('%', 'pp'), 'genuine help')}
            ${tile('Revenue (genuine)', pct(g.revenue_pct, 1), g.roi != null ? `ROI ${Number(g.roi).toFixed(2)}` : '')}
            ${tile('Revenue (promotional)', pct(p.revenue_pct, 1), p.roi != null ? `ROI ${Number(p.roi).toFixed(2)} · backlash risk` : 'backlash risk')}
          </div><p class="small muted">Offering simulated: ${esc(sim.offering)}. ROI below 0 means the help does not pay back within 60 days - which is fine if helping is the goal.</p>`);
        }
        html += section('Opportunity for your startup', c.opportunity ? `<p class="small">${esc(c.opportunity)}</p>` : '');
        html += section('Ethics', c.ethics_note ? `<p class="small">${esc(c.ethics_note)}</p>` : '');
        break;
      }
      case 'risk_alert':
        html += `<p>${esc(i.summary)}</p>`;
        if (c.risk_level) html += `<p>${chip('risk: ' + c.risk_level, { critical: 'critical', high: 'serious', moderate: 'warning', low: 'good' }[c.risk_level] || '')}</p>`;
        if (c.facts && c.facts.seismic) {
          const s = c.facts.seismic;
          html += `<div class="tiles">${tile('Quakes M2.5+ (7 days)', s.quakes_last_7d, `normal ≈ ${s.baseline_weekly_rate}/week within ${s.radius_km} km`)}
            ${tile('Long-term seismic hazard', s.long_term_hazard, `${s.m5_plus_last_50y} M5+ quakes in 50 years`)}
            ${tile('Anomaly', s.abnormal ? 'Abnormal' : 'Normal', `p = ${s.p_value}`)}</div>`;
        }
        html += section('Triggers', c.triggers && c.triggers.length ? list(c.triggers) : '');
        html += section('Immediate actions', list(c.immediate_actions));
        html += section('Insurance to take or review', (c.insurance || []).map((x) => `<div class="rec ${esc(x.priority)}"><strong>${esc(x.type)}</strong><div class="small">${esc(x.why)}</div></div>`).join('') || '');
        html += section('Preparedness', list(c.preparedness));
        return html + sources(c.sources);
      case 'persona_pulse':
        html += `<p class="small">${esc(i.summary)}</p>`;
        html += (c.reactions || []).map((r) => `<div class="card"><div class="persona"><div class="avatar">${esc((r.persona || '?')[0])}</div><div class="grow">
          <div class="row"><strong class="grow">${esc(r.persona)} · ${esc(r.region)}</strong><span class="${r.mood >= 0 ? 'up' : 'down'}">${r.mood >= 0 ? '▲' : '▼'} mood ${Number(r.mood).toFixed(2)}</span></div>
          <div class="small muted">${esc(r.description)}</div>
          <p class="small" style="margin:6px 0">“${esc(r.feeling)}”</p>
          <div class="meta">${chip('spending: ' + (r.spending_intent || '–'))}${chip('investing: ' + (r.investing_intent || '–'))}${chip('would try startup: ' + (r.would_try_startup || '–'))}</div>
          ${r.actions && r.actions.length ? list(r.actions) : ''}</div></div></div>`).join('');
        return html;
      case 'simulation_report':
        html += `<p>${esc(i.summary)}</p>`;
        html += section('Emergent findings', (c.emergent_findings || []).map((f) => `<div class="card soft"><h4>${esc(f.pattern)}</h4><div class="small">${esc(f.explanation)}</div></div>`).join(''));
        html += section('Risks', list(c.risks));
        html += section('Opportunities', list(c.opportunities));
        if (c.run_id) html += `<p><a href="#sim/${c.run_id}">Open the simulation run →</a></p>`;
        break;
      default:
        html += `<p>${esc(i.summary)}</p>`;
    }
    html += section('Recommendations for your startup', recommendations(i.recommendations, i.hidden_recommendations));
    html += sources(c.sources);
    return html;
  }

  function head(i) {
    return `<div class="meta">${chip(kindLabel(i.kind), 'info')}${i.region && i.region !== 'GLOBAL' ? chip(i.region) : ''}${severityChip(i.severity)}</div>
      <h2 style="margin-top:6px">${esc(i.title)}</h2>
      <div class="small muted" style="margin-top:4px">Saved ${esc(fmtDateTime(i.created_at))} (${esc(timeAgo(i.created_at))})${i.provider ? ` · generated by ${esc(i.provider)}${i.model ? ' (' + esc(i.model) + ')' : ''}` : ''}</div>`;
  }

  window.R = { kindLabel, insightCard, recommendations, scenarios, body, head, tile, list, section, severityChip };
})();
