/* Simulation lab: scenario editor, background runs, results with emergent patterns. */
(function () {
  'use strict';
  const { esc, fmtDateTime, timeAgo, pct, chip, num } = window.U;
  const VALUE_FIELD = {
    news: ['value', -0.3, 'value −1..1'], market_shock: ['magnitude', -0.07, 'magnitude'],
    disaster: ['severity', 0.8, 'severity 0..1'], rate_shock: ['magnitude', -0.25, 'magnitude'],
    price_change: ['pct', -0.1, 'price change'], marketing_push: ['multiplier', 2, 'multiplier'],
    csr_campaign: ['budget', 1, 'budget (months)'],
  };
  const SEV_CLASS = { high: 'critical', medium: 'warning', low: '' };

  const Sim = {
    presets: [], eventTypes: {}, limits: {}, events: [], runs: [], currentId: null, loaded: false,

    async init() {
      if (this.loaded) { await this.loadRuns(); return; }
      this.loaded = true;
      const data = await window.API.get('/api/sim/presets');
      this.presets = data.presets;
      this.eventTypes = data.event_types;
      this.limits = data.limits;
      const sel = document.getElementById('sim-preset');
      sel.innerHTML = this.presets.map((p) => `<option value="${esc(p.id)}">${esc(p.name)}</option>`).join('') + '<option value="">Custom (no preset)</option>';
      sel.addEventListener('change', () => this.applyPreset(sel.value));
      document.getElementById('sim-consumers').value = this.limits.default_consumers;
      document.getElementById('sim-consumers').max = this.limits.max_consumers;
      document.getElementById('sim-ticks').value = this.limits.default_ticks;
      document.getElementById('sim-ticks').max = this.limits.max_ticks;
      document.getElementById('sim-add-event').addEventListener('click', () => { this.readEvents(); this.events.push({ type: 'news', region: 'ALL', start: 10, value: -0.3 }); this.renderEvents(); });
      document.getElementById('sim-run').addEventListener('click', () => this.run());
      document.getElementById('calibrate-btn').addEventListener('click', () => this.calibrate());
      document.getElementById('sim-runs').addEventListener('click', (e) => {
        const row = e.target.closest('[data-run]');
        if (row) location.hash = `#sim/${row.dataset.run}`;
      });
      this.applyPreset(this.presets[0] ? this.presets[0].id : '');
      await Promise.all([this.loadRuns(), this.loadCalibrations()]);
    },

    applyPreset(id) {
      const p = this.presets.find((x) => x.id === id);
      this.events = p ? JSON.parse(JSON.stringify(p.events)) : [];
      document.getElementById('sim-preset-desc').textContent = p ? p.description : 'Build your own list of events below.';
      document.getElementById('sim-compare').checked = p ? !!p.compare_baseline : false;
      this.renderEvents();
    },

    renderEvents() {
      const box = document.getElementById('sim-events');
      if (!this.events.length) { box.innerHTML = '<p class="small muted">No events - a business-as-usual run.</p>'; return; }
      const types = Object.keys(this.eventTypes);
      box.innerHTML = this.events.map((e, i) => {
        const [field, , label] = VALUE_FIELD[e.type] || ['value', 0, 'value'];
        const who = e.type === 'price_change' ? (e.company || 'Challenger') : (Array.isArray(e.region) ? e.region.join(',') : (e.region || 'ALL'));
        return `<div class="event-row" data-i="${i}" title="${esc(this.eventTypes[e.type] || '')}">
          <select data-k="type">${types.map((t) => `<option ${t === e.type ? 'selected' : ''}>${t}</option>`).join('')}</select>
          <input type="text" data-k="who" value="${esc(who)}" aria-label="${e.type === 'price_change' ? 'company' : 'region (ISO2 or ALL)'}" placeholder="${e.type === 'price_change' ? 'company' : 'IN / ALL'}">
          <input type="number" data-k="start" value="${esc(e.start || 0)}" min="0" aria-label="start tick" title="start tick">
          <input type="number" step="0.01" data-k="value" value="${esc(e[field] != null ? e[field] : '')}" aria-label="${label}" title="${label}">
          <button class="btn sm ghost" data-remove="${i}" aria-label="remove event">✕</button></div>`;
      }).join('') + '<p class="small muted">Columns: type · region or company · start tick · value. Other settings come from the preset.</p>';
      box.querySelectorAll('[data-remove]').forEach((b) => b.addEventListener('click', () => { this.readEvents(); this.events.splice(+b.dataset.remove, 1); this.renderEvents(); }));
      box.querySelectorAll('select[data-k=type]').forEach((s) => s.addEventListener('change', () => { this.readEvents(); this.renderEvents(); }));
    },

    readEvents() {
      document.querySelectorAll('#sim-events .event-row').forEach((row) => {
        const i = +row.dataset.i;
        const old = this.events[i] || {};
        const type = row.querySelector('[data-k=type]').value;
        const ev = type === old.type ? { ...old } : { type };
        const who = row.querySelector('[data-k=who]').value.trim();
        if (type === 'price_change') { ev.company = who || 'Challenger'; delete ev.region; }
        else {
          ev.region = who.includes(',') ? who.split(',').map((s) => s.trim().toUpperCase()).filter(Boolean) : (who.toUpperCase() || 'ALL');
          if (['marketing_push', 'csr_campaign'].includes(type)) { ev.company = ev.company || 'startup'; ev.treatment = true; }
        }
        ev.start = parseInt(row.querySelector('[data-k=start]').value || '0', 10);
        const [field, def] = VALUE_FIELD[type] || ['value', 0];
        const raw = row.querySelector('[data-k=value]').value;
        ev[field] = raw === '' ? def : parseFloat(raw);
        this.events[i] = ev;
      });
    },

    async run() {
      this.readEvents();
      const btn = document.getElementById('sim-run');
      btn.disabled = true;
      try {
        const res = await window.API.post('/api/sim/run', {
          preset_id: document.getElementById('sim-preset').value || null,
          events: this.events,
          consumers: parseInt(document.getElementById('sim-consumers').value, 10),
          ticks: parseInt(document.getElementById('sim-ticks').value, 10),
          shards: parseInt(document.getElementById('sim-shards').value || '0', 10),
          seed: parseInt(document.getElementById('sim-seed').value || '42', 10),
          compare_baseline: document.getElementById('sim-compare').checked,
        });
        window.App.toast({ level: 'info', title: 'Simulation queued', message: res.run.name });
        await this.loadRuns();
        location.hash = `#sim/${res.run.id}`;
      } catch (ex) { window.App.toast({ level: 'warning', title: 'Could not start simulation', message: ex.message }); }
      finally { btn.disabled = false; }
    },

    async loadRuns() {
      const data = await window.API.get('/api/sim/runs');
      this.runs = data.items;
      const box = document.getElementById('sim-runs');
      box.innerHTML = this.runs.length ? this.runs.map((r) => `<div class="card clickable" data-run="${r.id}">
          <div class="row"><strong class="grow">${esc(r.name)}</strong>${chip(r.status, { done: 'good', failed: 'critical', running: 'info', queued: '', cancelled: '' }[r.status] || '')}</div>
          <div class="meta">${num(r.consumers, 0)} agents · ${r.ticks} days · ${esc(fmtDateTime(r.created_at))}${r.pattern_count ? ` · ${r.pattern_count} patterns` : ''}</div>
          ${r.status === 'running' || r.status === 'queued' ? `<div class="progress" style="margin-top:6px"><i id="run-prog-${r.id}" style="width:2%"></i></div><div class="small muted" id="run-note-${r.id}"></div>` : ''}
        </div>`).join('') : '<div class="empty">No runs yet.</div>';
    },

    onTask(t) {
      if (t.kind !== 'sim.run') return;
      const run = this.runs.find((r) => r.task_id === t.id);
      if (!run) { if (t.status === 'running') this.loadRuns(); return; }
      const bar = document.getElementById(`run-prog-${run.id}`);
      if (bar) bar.style.width = `${Math.max(2, Math.round((t.progress || 0) * 100))}%`;
      const note = document.getElementById(`run-note-${run.id}`);
      if (note && t.note) note.textContent = t.note;
      if (this.currentId === run.id) {
        const live = document.getElementById('sim-live-note');
        if (live && t.note) live.textContent = `${Math.round((t.progress || 0) * 100)}% · ${t.note}`;
      }
    },

    async onSimEvent(evt) {
      await this.loadRuns();
      if (evt.run_id === this.currentId) await this.open(this.currentId);
    },

    async open(id) {
      this.currentId = +id;
      const box = document.getElementById('sim-detail');
      let run;
      try { run = await window.API.get(`/api/sim/runs/${id}`); } catch (ex) { box.innerHTML = `<div class="empty">${esc(ex.message)}</div>`; return; }
      this.render(run);
    },

    render(run) {
      const box = document.getElementById('sim-detail');
      const head = `<div class="row wrap"><h2 class="grow" style="margin:0">${esc(run.name)}</h2>${chip(run.status, run.status === 'done' ? 'good' : run.status === 'failed' ? 'critical' : 'info')}
        ${['queued', 'running'].includes(run.status) ? `<button class="btn sm" id="sim-cancel">Cancel</button>` : ''}</div>
        <p class="small muted">${esc(run.scenario.description || '')} Created ${esc(fmtDateTime(run.created_at))}${run.finished_at ? ` · finished ${esc(fmtDateTime(run.finished_at))}` : ''}</p>`;
      if (run.status !== 'done') {
        box.innerHTML = head + (run.status === 'failed' ? `<div class="card"><strong>Failed</strong><pre class="small" style="white-space:pre-wrap">${esc(run.error)}</pre></div>`
          : `<div class="card"><p id="sim-live-note" class="small">Waiting for a worker…</p><p class="small muted">${num(run.consumers, 0)} consumer agents · ${run.ticks} days. You can keep using the app; a notification arrives when it finishes.</p></div>`);
        const c = document.getElementById('sim-cancel');
        if (c) c.addEventListener('click', async () => { await window.API.post(`/api/sim/runs/${run.id}/cancel`); this.loadRuns(); });
        return;
      }
      const res = run.results || {}, meta = res.meta || {}, s = res.series || {}, fin = res.final || {};
      const regions = (meta.regions || []).slice(0, 8);
      const labels = (s.t || []).map((t) => t + 1);
      const startup = (fin.companies || []).find((c) => c.is_startup) || {};
      const up = run.uplift || {};
      const tiles = [
        window.R.tile('Agents', num(meta.agents, 0), `${num(meta.traders, 0)} traders · ${meta.shards} shard(s), ${meta.backend}`),
        window.R.tile('Run time', `${meta.duration_s}s`, `${meta.ticks} simulated days`),
        window.R.tile(`${startup.name || 'Startup'}: customer share`, pct((startup.share || 0) * 100, 1, false), up.share_pp != null ? `vs baseline ${up.share_pp > 0 ? '+' : ''}${up.share_pp}pp` : ''),
        window.R.tile('Awareness', pct((startup.awareness || 0) * 100, 0, false), up.awareness_pp != null ? `vs baseline ${up.awareness_pp > 0 ? '+' : ''}${up.awareness_pp}pp` : ''),
      ];
      if (up.revenue_pct != null) tiles.push(window.R.tile('Revenue vs baseline', pct(up.revenue_pct, 1), up.roi != null ? `campaign ROI ${up.roi}` : 'same seed, without your actions/shocks'));
      tiles.push(window.R.tile('Emergent patterns', String((run.patterns || []).length), 'detected, not scripted'));

      const spansFor = (types, region) => (run.patterns || []).filter((p) => types.includes(p.type) && (!region || p.label.includes(region)))
        .map((p) => ({ start: p.start, end: p.end }));
      const mkSeries = regions.filter((r) => r.has_market && s.market && s.market[r.name]).map((r) => {
        const pr = s.market[r.name].price; const base = pr[0] || 1;
        return { name: r.name, data: pr.map((v) => +(v / base * 100).toFixed(3)), colorIndex: meta.regions.indexOf(r) };
      });
      const confSeries = regions.map((r) => ({ name: r.name, data: s.conf[r.name], colorIndex: meta.regions.indexOf(r) }));
      const panicSeries = regions.map((r) => ({ name: r.name, data: s.panic[r.name].map((v) => +(v * 100).toFixed(2)), colorIndex: meta.regions.indexOf(r) }));
      const companies = meta.companies || [];
      const shareSeries = companies.map((c, i) => ({ name: c, data: s.share[c].map((v) => +(v * 100).toFixed(3)), colorIndex: i }));
      if (res.baseline_series && startup.name && res.baseline_series.share[startup.name]) {
        shareSeries.push({ name: `${startup.name} (baseline)`, data: res.baseline_series.share[startup.name].map((v) => +(v * 100).toFixed(3)), colorIndex: 0, dashed: true, width: 1.5 });
      }
      const awSeries = regions.map((r) => ({ name: r.name, data: (s.startup_awareness_by_region[r.name] || []).map((v) => +(v * 100).toFixed(2)), colorIndex: meta.regions.indexOf(r) }));
      const priceSeries = companies.map((c, i) => ({ name: c, data: s.price[c], colorIndex: i }));
      const extra = (meta.regions || []).length > 8 ? `<p class="small muted">Showing the first 8 of ${(meta.regions || []).length} regions.</p>` : '';

      const chartCard = (id, title, sub, series) => `<div class="chart-card"><h3>${esc(title)}</h3><div class="sub">${esc(sub)}</div>
        <div class="chart-box"><canvas id="${id}" role="img" aria-label="${esc(title)}"></canvas></div>${window.Charts.table(labels, series)}</div>`;

      const patterns = (run.patterns || []).map((p) => `<div class="pattern"><div>${chip(p.severity, SEV_CLASS[p.severity] || '')}</div>
        <div class="grow"><strong>${esc(p.label)}</strong> <span class="small muted">days ${p.start + 1}–${p.end + 1}</span><div class="small">${esc(p.description)}</div></div></div>`).join('') || '<p class="muted small">No emergent patterns detected in this run.</p>';
      const byRegion = up.by_region ? `<div class="chart-card"><h3>Effect by region vs baseline</h3><table class="data"><thead><tr><th>Region</th><th>Customer share</th><th>Awareness</th><th>Reputation</th></tr></thead><tbody>
        ${Object.entries(up.by_region).map(([k, v]) => `<tr><td>${esc(k)}</td><td>${v.share_pp > 0 ? '+' : ''}${v.share_pp}pp</td><td>${v.awareness_pp > 0 ? '+' : ''}${v.awareness_pp}pp</td><td>${v.reputation_delta > 0 ? '+' : ''}${v.reputation_delta}</td></tr>`).join('')}</tbody></table></div>` : '';
      const report = run.report_insight_id
        ? `<button class="btn primary" data-insight="${run.report_insight_id}">Open the AI report</button>`
        : '<span class="small muted">The AI is writing the report… (live notification when ready)</span>';

      box.innerHTML = head + `<div class="tiles">${tiles.join('')}</div><div class="row" style="margin-bottom:12px">${report}</div>` + extra +
        (mkSeries.length ? chartCard('ch-markets', 'Stock markets (indexed to 100)', 'Trader agents; shaded = crash episodes', mkSeries) : '') +
        chartCard('ch-conf', 'Consumer confidence by region', '−1 = deep pessimism, +1 = euphoria', confSeries) +
        chartCard('ch-panic', 'Consumers in panic mode (%)', 'Fear spreading through social ties; shaded = panic cascades', panicSeries) +
        chartCard('ch-share', 'Customer share by company (%)', res.baseline_series ? 'Dashed = your startup in the baseline run' : 'Word of mouth, social proof and prices at work', shareSeries) +
        chartCard('ch-aw', `${startup.name || 'Startup'}: awareness by region (%)`, 'Ads, media coverage and word of mouth, minus forgetting', awSeries) +
        chartCard('ch-price', 'Prices set by the companies', 'Adaptive competitors learn pricing from profit', priceSeries) +
        byRegion + `<div class="chart-card"><h3>Emergent patterns</h3>${patterns}</div>`;

      const C = window.Charts;
      if (mkSeries.length) C.line(document.getElementById('ch-markets'), { labels, series: mkSeries, yFormat: (v) => Number(v).toFixed(0), spans: spansFor(['crash']) });
      C.line(document.getElementById('ch-conf'), { labels, series: confSeries, yFormat: (v) => Number(v).toFixed(2) });
      C.line(document.getElementById('ch-panic'), { labels, series: panicSeries, yFormat: (v) => `${Number(v).toFixed(0)}%`, spans: spansFor(['panic_cascade']), beginAtZero: true });
      C.line(document.getElementById('ch-share'), { labels, series: shareSeries, yFormat: (v) => `${Number(v).toFixed(1)}%`, beginAtZero: true });
      C.line(document.getElementById('ch-aw'), { labels, series: awSeries, yFormat: (v) => `${Number(v).toFixed(0)}%`, beginAtZero: true });
      C.line(document.getElementById('ch-price'), { labels, series: priceSeries, yFormat: (v) => Number(v).toFixed(2) });
    },

    async calibrate() {
      try {
        const res = await window.API.post('/api/sim/calibrate', { symbol: null });
        window.App.toast({ level: 'info', title: res.created ? 'Calibration queued' : 'Calibration already queued', message: 'Fitting market models to real index data.' });
      } catch (ex) { window.App.toast({ level: 'warning', title: 'Calibration', message: ex.message }); }
    },

    async loadCalibrations() {
      const data = await window.API.get('/api/sim/calibrations');
      const box = document.getElementById('calibrations');
      if (!data.items.length) { box.innerHTML = '<p class="small muted">Not calibrated yet (runs weekly, or click above).</p>'; return; }
      box.innerHTML = `<div class="table-scroll"><table class="data"><thead><tr><th>Index</th><th>Vol real/sim</th><th>Kurt real/sim</th><th>Clust. real/sim</th><th>Loss</th></tr></thead><tbody>
        ${data.items.map((c) => `<tr><td title="${esc(fmtDateTime(c.created_at))}">${esc(c.symbol)}</td>
          <td>${(c.target_moments.std * 100).toFixed(2)}% / ${(c.sim_moments.std * 100).toFixed(2)}%</td>
          <td>${c.target_moments.kurt.toFixed(1)} / ${c.sim_moments.kurt.toFixed(1)}</td>
          <td>${c.target_moments.acf_abs.toFixed(2)} / ${c.sim_moments.acf_abs.toFixed(2)}</td><td>${c.loss.toFixed(2)}</td></tr>`).join('')}</tbody></table></div>
        <p class="small muted">Vol = daily volatility, Kurt = excess kurtosis (fat tails), Clust. = autocorrelation of |returns| (volatility clustering). ${esc(timeAgo(data.items[0].created_at))}</p>`;
    },
  };

  window.Sim = Sim;
})();
