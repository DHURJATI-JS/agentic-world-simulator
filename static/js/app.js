/* Main application controller: routing, globe view panels, live updates, notifications. */
(function () {
  'use strict';
  const { esc, safeUrl, fmtDateTime, timeAgo, num, pct, deltaHtml, chip, debounce, storeGet, storeSet, ALERT_STATUS } = window.U;
  const API = window.API;
  const HZ_LETTER = { earthquake: 'E', cyclone: 'C', flood: 'F', volcano: 'V', wildfire: 'W', drought: 'D' };
  const KIND_GROUPS = [['index', 'Stock indices'], ['volatility', 'Volatility'], ['fx', 'Currencies'], ['commodity', 'Commodities'], ['crypto', 'Crypto']];
  const LEVEL_DOT = { info: 'muted', success: 'good', warning: 'warning', critical: 'critical' };

  const App = {
    status: null, overview: null, hazards: [], markets: [], unread: 0, profile: null, lastView: 'globe',
    selectedCountry: null, news: { country: '', category: '', q: '' },

    async boot() {
      this.bindChrome();
      await this.refreshStatus();
      try {
        const prof = await API.get('/api/profile');
        this.profile = prof.profile;
        if (!prof.onboarding_complete) window.Onboarding.open(prof.messages);
      } catch (e) { /* locked or offline */ }
      await window.WorldGlobe.init(document.getElementById('globe'), {
        onCountry: (iso) => { location.hash = `#country/${iso}`; },
        onHazard: (h) => this.showHazard(h),
      });
      this.connectStream();
      window.addEventListener('hashchange', () => this.route());
      await this.route();
      await Promise.all([this.loadWorld(), this.loadBrief(), this.loadNotifications()]);
      if (!this.selectedCountry) this.renderRightDefault();
      setInterval(() => this.refreshStatus(), 60000);
    },

    /* ------------------------------------------------------------ chrome */
    bindChrome() {
      document.getElementById('theme-toggle').addEventListener('click', () => this.toggleTheme());
      try {
        window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => {
          if (!document.documentElement.getAttribute('data-theme')) this.themeChanged();
        });
      } catch (e) { /* old browser */ }
      document.getElementById('logout').addEventListener('click', async () => { await API.post('/api/auth/logout'); location.href = '/login'; });
      document.getElementById('bell').addEventListener('click', () => this.toggleDrawer());
      document.getElementById('notif-close').addEventListener('click', () => this.toggleDrawer(false));
      document.getElementById('notif-read-all').addEventListener('click', async () => {
        await API.post('/api/notifications/read', { all: true }); this.unread = 0; this.updateBell(); this.loadNotifications();
      });
      document.getElementById('ai-pill').addEventListener('click', () => this.toggleAiPopover());
      document.getElementById('ai-recheck').addEventListener('click', async () => {
        try { const r = await API.post('/api/system/ai/recheck'); this.applyAiStatus(r.ai); } catch (e) { /* still down */ }
      });
      document.getElementById('modal-close').addEventListener('click', () => this.closeModal());
      document.getElementById('modal').addEventListener('click', (e) => { if (e.target.id === 'modal') this.closeModal(); });
      document.addEventListener('keydown', (e) => { if (e.key === 'Escape') { this.closeModal(); this.toggleDrawer(false); } });
      document.addEventListener('click', (e) => {
        const ins = e.target.closest('[data-insight]');
        if (ins) { e.preventDefault(); this.openInsight(ins.dataset.insight); }
      });
      document.querySelectorAll('.tabs button').forEach((b) => b.addEventListener('click', () => this.showTab(b.dataset.tab)));
      document.getElementById('layer-select').addEventListener('change', (e) => { window.WorldGlobe.setMetric(e.target.value); storeSet('worldsim-layer', e.target.value); });
      const savedLayer = storeGet('worldsim-layer');
      if (savedLayer) { document.getElementById('layer-select').value = savedLayer; window.WorldGlobe.metric = savedLayer; }
      document.getElementById('toggle-hazards').addEventListener('change', (e) => window.WorldGlobe.setShowHazards(e.target.checked));
      document.getElementById('toggle-satellite').addEventListener('change', (e) => window.WorldGlobe.setSatellite(e.target.checked));
      document.getElementById('toggle-rotate').addEventListener('change', (e) => window.WorldGlobe.setRotate(e.target.checked));
      document.getElementById('insight-kind').addEventListener('change', () => this.loadInsights());
      document.getElementById('insight-search').addEventListener('input', debounce(() => this.loadInsights(), 400));
    },

    effectiveTheme() {
      const attr = document.documentElement.getAttribute('data-theme');
      if (attr) return attr;
      return window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
    },
    toggleTheme() {
      const next = this.effectiveTheme() === 'dark' ? 'light' : 'dark';
      document.documentElement.setAttribute('data-theme', next);
      storeSet('worldsim-theme', next);
      this.themeChanged();
    },
    themeChanged() { window.Charts.rerenderAll(); window.WorldGlobe.applyTheme(); },

    /* ------------------------------------------------------------ routing */
    showView(view) {
      document.querySelectorAll('.view').forEach((v) => v.classList.toggle('active', v.id === `view-${view}`));
      document.querySelectorAll('.views a').forEach((a) => a.classList.toggle('active', a.dataset.view === view));
      this.currentView = view;
    },
    async route() {
      const hash = location.hash.replace(/^#/, '') || 'globe';
      const [name, arg] = hash.split('/');
      if (name === 'insight') { if (arg) this.openInsight(arg, false); return; }
      const views = { globe: 'globe', country: 'globe', news: 'globe', markets: 'globe', hazards: 'globe', sim: 'sim', insights: 'insights', tasks: 'tasks', profile: 'profile' };
      const view = views[name] || 'globe';
      this.lastView = hash;
      this.showView(view);
      if (name === 'country' && arg) this.selectCountry(arg.toUpperCase());
      if (['news', 'markets', 'hazards'].includes(name)) this.showTab(name);
      if (view === 'globe') setTimeout(() => window.WorldGlobe.resize(), 30);
      if (view === 'sim') { try { await window.Sim.init(); if (arg) await window.Sim.open(arg); } catch (e) { this.toast({ level: 'warning', title: 'Simulation lab', message: e.message }); } }
      if (view === 'insights') this.loadInsights();
      if (view === 'tasks') this.loadTasks();
      if (view === 'profile') this.loadProfileView();
    },
    showTab(tab) {
      document.querySelectorAll('.tabs button').forEach((b) => b.classList.toggle('active', b.dataset.tab === tab));
      ['brief', 'markets', 'hazards', 'news'].forEach((t) => { document.getElementById(`tab-${t}`).hidden = t !== tab; });
      if (tab === 'news') this.loadNews();
      if (tab === 'markets') this.renderMarkets();
      if (tab === 'hazards') this.renderHazards();
    },

    /* ------------------------------------------------------------ status, lock, first run */
    async refreshStatus() {
      try {
        this.status = await API.get('/api/system/status');
        this.applyAiStatus(this.status.ai);
        this.updateFirstRun();
      } catch (e) { /* handled by API (lock / login) */ }
    },
    applyAiStatus(ai) {
      if (!ai) return;
      const recovered = this.aiStatus && !this.aiStatus.available && ai.available;
      this.aiStatus = ai;
      if (recovered) {
        // data requests were blocked while the AI was absent - load everything again
        this.afterProfileSaved();
        this.loadBrief();
        this.loadNotifications();
      }
      const pill = document.getElementById('ai-pill');
      const cls = ai.available ? (ai.is_fallback ? 'warning' : 'good') : 'critical';
      const label = ai.available ? `AI · ${ai.active_label}${ai.is_fallback ? ' (fallback)' : ''}` : 'AI not present';
      pill.innerHTML = `<span class="dot ${cls}"></span><span>${esc(label)}</span>`;
      if (ai.available) document.getElementById('ai-lock').hidden = true;
      else this.showLock(ai);
      if (!document.getElementById('ai-popover').hidden) this.renderAiPopover();
    },
    showLock(ai) {
      if (!ai) return;
      document.getElementById('ai-lock').hidden = false;
      document.getElementById('ai-lock-detail').innerHTML = this.chainTable(ai);
    },
    chainTable(ai) {
      const stateCls = { ok: 'good', down: 'critical', not_configured: 'muted', disabled: 'muted', unknown: 'muted' };
      return `<table class="data"><thead><tr><th>#</th><th>Tier</th><th>State</th><th>Detail</th></tr></thead><tbody>
        ${(ai.providers || []).map((p) => `<tr><td>${p.order}</td><td style="text-align:left">${esc(p.label)}<div class="muted">${esc(p.model || '')}</div></td>
          <td style="text-align:left"><span class="dot ${stateCls[p.state] || 'muted'}"></span> ${esc(p.state.replace('_', ' '))}${p.circuit && p.circuit !== 'closed' ? ` · circuit ${esc(p.circuit)}` : ''}</td>
          <td style="text-align:left;max-width:260px">${esc(p.last_error || (p.last_latency_ms ? `${p.last_latency_ms} ms` : ''))}</td></tr>`).join('')}</tbody></table>`;
    },
    toggleAiPopover() {
      const pop = document.getElementById('ai-popover');
      pop.hidden = !pop.hidden;
      if (!pop.hidden) this.renderAiPopover();
    },
    renderAiPopover() {
      const ai = this.aiStatus || {};
      const g = ai.guard || {};
      document.getElementById('ai-popover').innerHTML = `<h3>AI failover chain</h3>
        <p class="small muted" style="margin:4px 0 8px">OpenAI Gemma (LAN gateway) → Local Gemma → Anthropic → unavailable. Every request falls through to the next tier.</p>
        ${this.chainTable(ai)}
        <h4 style="margin:12px 0 4px">Spam protection</h4>
        <div class="small">Concurrency ${g.active || 0}/${g.max_concurrency} · waiting ${g.waiting || 0}/${g.max_waiting} · ${g.rate_per_min}/min app-wide · ${g.user_rate_per_min}/min per admin</div>
        <div class="small muted">Cache hits ${(g.stats || {}).cache_hits || 0} · merged duplicates ${(g.stats || {}).coalesced || 0} · rejected (rate/queue/busy) ${((g.stats || {}).rejected_rate || 0) + ((g.stats || {}).rejected_queue || 0) + ((g.stats || {}).rejected_busy || 0)} · user throttled ${g.user_rate_rejections || 0}</div>
        <div class="small muted">Claude today: ${g.claude_calls_today || 0}/${g.claude_call_limit} calls, ${num(g.claude_tokens_today || 0, 0)}/${num(g.claude_token_limit || 0, 0)} tokens</div>`;
    },
    updateFirstRun(task) {
      const app = this.status && this.status.app;
      const banner = document.getElementById('firstrun-banner');
      const show = app && app.first_run;
      banner.hidden = !show;
      document.body.classList.toggle('has-banner', !!show);
      if (task && show) {
        document.getElementById('firstrun-progress').style.width = `${Math.max(3, Math.round((task.progress || 0) * 100))}%`;
        if (task.note) document.getElementById('firstrun-text').textContent = `Collecting global headlines from reliable sources - ${task.note}`;
      }
      setTimeout(() => window.WorldGlobe.resize(), 20);
    },

    /* ------------------------------------------------------------ live stream */
    connectStream() {
      const es = new EventSource('/api/stream');
      const on = (name, fn) => es.addEventListener(name, (e) => { try { fn(JSON.parse(e.data)); } catch (err) { console.error(name, err); } });
      on('ai_status', (ai) => this.applyAiStatus(ai));
      on('notification', (n) => { this.toast(n); this.unread += 1; this.updateBell(); if (!document.getElementById('notif-drawer').hidden) this.loadNotifications(); });
      on('task', (t) => this.onTask(t));
      on('world_update', (u) => this.onWorldUpdate(u));
      on('insight', (i) => this.onInsight(i));
      on('risk', () => { if (!this.selectedCountry) this.refreshStatus().then(() => this.renderRightDefault()); });
      on('sim', (s) => { if (window.Sim.loaded) window.Sim.onSimEvent(s); this.loadWorld(); });
      on('profile', (p) => { this.profile = p; if (!this.selectedCountry) this.renderRightDefault(); });
    },
    onTask(t) {
      if (t.kind === 'news.fetch' && this.status && this.status.app.first_run) {
        this.updateFirstRun(t);
        if (t.status === 'done') this.refreshStatus();
      }
      if (window.Sim.loaded) window.Sim.onTask(t);
      if (this.currentView === 'tasks') this.debouncedTasks();
    },
    debouncedTasks: debounce(function () { App.loadTasks(); }, 600),
    onWorldUpdate: debounce(function (u) {
      App.loadWorld();
      if (u.what === 'news' && !document.getElementById('tab-news').hidden) App.loadNews();
      if (App.selectedCountry) App.selectCountry(App.selectedCountry, false);
    }, 800),
    onInsight(i) {
      if (i.kind === 'daily_brief' || i.kind === 'persona_pulse') this.loadBrief();
      if (this.currentView === 'insights') this.loadInsights();
      if (this.selectedCountry && i.region === this.selectedCountry) this.selectCountry(this.selectedCountry, false);
      if (!this.selectedCountry && (i.kind === 'risk_alert' || i.kind === 'persona_pulse')) this.renderRightDefault();
      if (i.kind === 'geo_impact') this.loadWorld();
      if (i.kind === 'simulation_report' && window.Sim.loaded && window.Sim.currentId) window.Sim.open(window.Sim.currentId);
    },

    /* ------------------------------------------------------------ world data */
    async loadWorld() {
      try {
        const [overview, hz, mk] = await Promise.all([
          API.get('/api/world/overview'), API.get('/api/world/hazards?days=7'), API.get('/api/world/markets'),
        ]);
        this.overview = overview; this.hazards = hz.items; this.markets = mk.items;
        window.WorldGlobe.setData(overview);
        window.WorldGlobe.setHazards(hz.items);
        if (!document.getElementById('tab-markets').hidden) this.renderMarkets();
        if (!document.getElementById('tab-hazards').hidden) this.renderHazards();
      } catch (e) { /* lock/offline */ }
    },

    async loadBrief() {
      const box = document.getElementById('tab-brief');
      let brief = null, pulse = null, recent = [];
      try {
        [brief, pulse, recent] = await Promise.all([
          API.get('/api/insights/latest?kind=daily_brief').then((r) => r.item),
          API.get('/api/insights/latest?kind=persona_pulse').then((r) => r.item),
          API.get('/api/insights?limit=6').then((r) => r.items),
        ]);
      } catch (e) { return; }
      let html = '';
      if (brief) {
        const c = brief.content || {};
        html += `<div class="section"><div class="meta">${chip('Daily global brief', 'info')}<span>${esc(fmtDateTime(brief.created_at))}</span></div>
          <h2 style="margin:6px 0">${esc(brief.title)}</h2><p class="small">${esc(brief.summary)}</p>
          ${(c.themes || []).slice(0, 5).map((t) => `<div class="card soft"><h4>${esc(t.title)}</h4><div class="meta">${(t.regions || []).map((r) => `<a href="#country/${esc(r)}">${esc(r)}</a>`).join(' ')}</div><div class="small" style="margin-top:3px">${esc(t.summary)}</div></div>`).join('')}
          <h3 style="margin:10px 0 6px">For your startup</h3>${window.R.recommendations((brief.recommendations || []).slice(0, 3), brief.hidden_recommendations)}
          <button class="btn sm" data-insight="${brief.id}">Open the full brief</button></div>`;
      } else {
        const first = this.status && this.status.app && this.status.app.first_run;
        html += `<div class="empty">${first ? 'First run: the AI is collecting global headlines from reliable sources. The brief appears once they are read and summarised.' : 'The daily brief will appear once today\'s headlines are classified and summarised.'}</div>`;
      }
      if (pulse) {
        const reactions = (pulse.content || {}).reactions || [];
        html += `<div class="section"><h3>How people are reacting <span class="small muted">${esc(timeAgo(pulse.created_at))}</span></h3>
          ${reactions.slice(0, 4).map((r) => `<div class="card soft"><div class="row"><strong class="grow">${esc(r.persona)} · ${esc(r.region)}</strong><span class="${r.mood >= 0 ? 'up' : 'down'} small">${r.mood >= 0 ? '▲' : '▼'} ${Number(r.mood).toFixed(2)}</span></div><div class="small">“${esc(r.feeling)}”</div></div>`).join('')}
          <button class="btn sm" data-insight="${pulse.id}">All reactions</button></div>`;
      }
      const others = (recent || []).filter((i) => i.kind !== 'daily_brief');
      if (others.length) html += `<div class="section"><h3>Latest AI analyses</h3>${others.slice(0, 5).map((i) => window.R.insightCard(i, { compact: true })).join('')}</div>`;
      box.innerHTML = html;
    },

    renderMarkets() {
      const box = document.getElementById('tab-markets');
      if (!this.markets.length) { box.innerHTML = '<div class="empty">Market data loads after the first headline collection.</div>'; return; }
      const hq = this.profile && this.profile.hq_country;
      const sorted = this.markets.slice().sort((a, b) => (b.country === hq) - (a.country === hq));
      box.innerHTML = KIND_GROUPS.map(([kind, title]) => {
        const items = sorted.filter((m) => m.kind === kind);
        if (!items.length) return '';
        return `<div class="section"><h3>${title}</h3>${items.map((m) => `<div class="list-row" data-symbol="${esc(m.symbol)}">
          <div class="grow"><div class="title">${esc(m.name)} ${m.country ? `<span class="chip">${esc(m.country)}</span>` : ''}</div>
            <div class="small muted">${m.ok ? esc((m.analytics || {}).regime || '') : 'unavailable'}</div></div>
          ${window.Charts.spark(m.spark)}
          <div class="value-col"><div>${m.price != null ? num(m.price, 2) : '–'}</div><div class="small">${deltaHtml(m.change_1d)}</div></div></div>`).join('')}</div>`;
      }).join('') + `<p class="small muted">Source: Yahoo Finance. Updated ${esc(timeAgo((this.markets[0] || {}).fetched_at))}.</p>`;
      box.querySelectorAll('[data-symbol]').forEach((row) => row.addEventListener('click', () => this.openMarket(row.dataset.symbol)));
    },

    renderHazards() {
      const box = document.getElementById('tab-hazards');
      const items = this.hazards.slice().sort((a, b) => (b.severity - a.severity) || (new Date(b.occurred_at) - new Date(a.occurred_at)));
      if (!items.length) { box.innerHTML = '<div class="empty">No hazards in the last 7 days (USGS M4.5+, GDACS alerts).</div>'; return; }
      box.innerHTML = items.slice(0, 120).map((h) => {
        const status = ALERT_STATUS[h.alert_level] || 'muted';
        return `<div class="list-row" data-hazard="${h.id}">
          <span class="hz-icon ${status}" aria-hidden="true">${HZ_LETTER[h.kind] || '!'}</span>
          <div class="grow"><div class="title">${esc(h.title)}</div>
            <div class="small muted">${h.magnitude ? `M${Number(h.magnitude).toFixed(1)} · ` : ''}${esc(h.kind)} · ${h.alert_level ? esc(h.alert_level) + ' alert · ' : ''}${esc(timeAgo(h.occurred_at))}${h.near_hq_km != null && h.near_hq_km < 1500 ? ` · ${Math.round(h.near_hq_km)} km from HQ` : ''}</div></div>
          ${h.insight_id ? '<span class="chip info">AI impact</span>' : ''}</div>`;
      }).join('') + '<p class="small muted">Sources: USGS Earthquake Hazards Program, GDACS. Letters: E earthquake, C cyclone, F flood, V volcano, W wildfire, D drought.</p>';
      box.querySelectorAll('[data-hazard]').forEach((row) => row.addEventListener('click', () => {
        const h = this.hazards.find((x) => String(x.id) === row.dataset.hazard);
        if (h) this.showHazard(h);
      }));
    },

    async loadNews() {
      const box = document.getElementById('tab-news');
      if (!box.dataset.ready) {
        box.dataset.ready = '1';
        box.innerHTML = `<input type="search" id="news-q" placeholder="Semantic search, e.g. 'rupee falls'…" style="margin-bottom:6px">
          <div class="row" style="margin-bottom:8px">
            <select id="news-cat" class="grow"><option value="">All topics</option>${['economy', 'markets', 'business', 'technology', 'politics', 'conflict', 'disaster', 'climate', 'energy', 'health', 'society', 'other'].map((c) => `<option>${c}</option>`).join('')}</select>
            <input type="text" id="news-country" placeholder="Country code (IN)" style="width:150px" maxlength="2"></div>
          <div id="news-list"></div>`;
        const reload = debounce(() => { this.news = { q: box.querySelector('#news-q').value.trim(), category: box.querySelector('#news-cat').value, country: box.querySelector('#news-country').value.trim().toUpperCase() }; this.loadNews(); }, 400);
        box.querySelector('#news-q').addEventListener('input', reload);
        box.querySelector('#news-cat').addEventListener('change', reload);
        box.querySelector('#news-country').addEventListener('input', reload);
      }
      const p = new URLSearchParams({ limit: '80' });
      if (this.news.q) p.set('q', this.news.q);
      if (this.news.category) p.set('category', this.news.category);
      if (this.news.country && this.news.country.length === 2) p.set('country', this.news.country);
      const list = box.querySelector('#news-list');
      try {
        const data = await API.get(`/api/world/news?${p}`);
        list.innerHTML = data.items.length ? data.items.map((a) => `<div class="news-item">
            <a href="${safeUrl(a.url)}" target="_blank" rel="noopener noreferrer">${esc(a.title)}</a>
            <div class="meta"><span>${esc(a.outlet)}</span><span>${esc(timeAgo(a.published_at || a.fetched_at))}</span>
              ${(a.countries || []).slice(0, 3).map((c) => `<a href="#country/${esc(c)}" class="chip">${esc(c)}</a>`).join('')}
              ${a.category ? chip(a.category) : ''}${a.sentiment != null ? chip(`tone ${a.sentiment > 0 ? '+' : ''}${Number(a.sentiment).toFixed(2)}`) : ''}</div></div>`).join('')
          + `<p class="small muted">${esc(data.mode)} · headlines refresh once every 24 hours from the reliable-source list.</p>`
          : '<div class="empty">No headlines match.</div>';
      } catch (e) { list.innerHTML = `<div class="empty">${esc(e.message)}</div>`; }
    },

    /* ------------------------------------------------------------ right panel */
    renderRightDefault() {
      this.selectedCountry = null;
      const box = document.getElementById('right-body');
      const p = this.profile;
      const risk = (this.status && this.status.risk) || {};
      let html = '<h2 style="margin-bottom:8px">Your startup</h2>';
      if (p && p.onboarding_complete) {
        const caps = [p.sells_physical_products && 'sells products', p.can_ship_domestic && 'ships domestically', p.can_ship_international && 'ships internationally', p.offers_services && 'services', p.digital_only && 'digital-only'].filter(Boolean);
        html += `<div class="card"><h3>${esc(p.name)}</h3><div class="small muted">${esc(p.description)}</div>
          <div class="meta" style="margin-top:6px">${caps.map((c) => chip(c, 'info')).join('') || chip('market insights only')}</div>
          <div class="small" style="margin-top:6px">HQ: ${esc(p.hq_city || '–')}, ${esc(p.hq_country)} · targets: ${esc((p.target_markets || []).join(', ') || '–')}</div></div>`;
      } else {
        html += '<div class="empty">Finish onboarding so the AI can tailor insights and watch your HQ location.</div>';
      }
      html += '<h3 style="margin:14px 0 8px">Location risk at HQ</h3>';
      if (risk.seismic) {
        const s = risk.seismic;
        html += `<div class="tiles" style="grid-template-columns:1fr 1fr">${window.R.tile('Seismic hazard', s.long_term_hazard, `${s.m5_plus_last_50y} M5+ in 50 years`)}
          ${window.R.tile('Last 7 days', `${s.quakes_last_7d} quakes`, s.abnormal ? 'ABNORMAL vs baseline' : `normal ≈ ${s.baseline_weekly_rate}/week`)}</div>`;
        if ((risk.weather_extremes || []).length) html += `<div class="card soft small"><strong>Weather extremes ahead</strong>${window.R.list(risk.weather_extremes.map((x) => `${x.date}: ${x.type} (${x.value})`))}</div>`;
        html += `<p class="small muted">Checked ${esc(timeAgo(risk.checked_at))} · USGS + Open-Meteo</p>`;
      } else {
        html += '<p class="small muted">Not scanned yet.</p>';
      }
      html += '<button class="btn sm" id="risk-rescan">Re-scan location risk</button><div id="risk-alerts" style="margin-top:10px"></div>';
      box.innerHTML = html;
      document.getElementById('risk-rescan').addEventListener('click', () => this.analyze({ kind: 'risk_scan', force: true }));
      API.get('/api/insights?kind=risk_alert&limit=3').then((r) => {
        const el = document.getElementById('risk-alerts');
        if (el) el.innerHTML = r.items.map((i) => window.R.insightCard(i)).join('');
      }).catch(() => {});
    },

    async selectCountry(iso, focus = true) {
      this.selectedCountry = iso;
      const panel = document.getElementById('right-panel');
      panel.classList.add('open');
      const box = document.getElementById('right-body');
      if (focus) window.WorldGlobe.focusCountry(iso);
      let d;
      try { d = await API.get(`/api/world/country/${iso}`); } catch (e) { box.innerHTML = `<div class="empty">${esc(e.message)}</div>`; return; }
      if (this.selectedCountry !== iso) return;
      const st = d.stats || {};
      const index = d.markets.find((m) => m.kind === 'index');
      box.innerHTML = `<div class="row"><h2 class="grow">${esc(d.name)}</h2><button class="btn sm ghost" id="country-close">Close</button></div>
        <p class="small muted" style="margin:2px 0 10px">${esc(d.subregion || d.continent || '')}</p>
        <div class="tiles" style="grid-template-columns:1fr 1fr">
          ${window.R.tile('Population', st.population ? `${num(st.population / 1e6, 1)} M` : '–', st.year ? `World Bank ${st.year}` : '')}
          ${window.R.tile('GDP per capita', st.gdp_per_capita ? `$${num(st.gdp_per_capita, 0)}` : '–', st.inflation != null ? `inflation ${num(st.inflation, 1)}%` : '')}
          ${index ? window.R.tile(index.name, num(index.price, 0), `today ${pct(index.change_1d)}`) : ''}
          ${window.R.tile('News sentiment', d.news_sentiment != null ? (d.news_sentiment > 0 ? '+' : '') + d.news_sentiment.toFixed(2) : '–', `${d.news.length} recent headlines`)}
        </div>
        <div class="row wrap" style="margin-bottom:12px">
          <button class="btn sm primary" id="analyze-region">Analyze region with AI</button>
          ${index ? '<button class="btn sm" id="analyze-market">Market outlook</button>' : ''}
        </div>
        ${d.markets.length ? `<div class="section"><h3>Markets</h3>${d.markets.map((m) => `<div class="list-row" data-symbol="${esc(m.symbol)}"><div class="grow"><div class="title">${esc(m.name)}</div></div>${window.Charts.spark(m.spark)}<div class="value-col">${deltaHtml(m.change_1d)}</div></div>`).join('')}</div>` : ''}
        ${d.insights.length ? `<div class="section"><h3>AI insights</h3>${d.insights.slice(0, 4).map((i) => window.R.insightCard(i, { compact: true })).join('')}</div>` : ''}
        ${d.hazards.length ? `<div class="section"><h3>Hazards (14 days)</h3>${d.hazards.map((h) => `<div class="list-row" data-hazard="${h.id}"><span class="hz-icon ${ALERT_STATUS[h.alert_level] || 'muted'}">${HZ_LETTER[h.kind] || '!'}</span><div class="grow"><div class="title">${esc(h.title)}</div><div class="small muted">${esc(timeAgo(h.occurred_at))}</div></div></div>`).join('')}</div>` : ''}
        ${d.personas.length ? `<div class="section"><h3>How people there react</h3>${d.personas.map((r) => `<div class="card soft"><div class="row"><strong class="grow">${esc(r.data.persona || r.persona_key)}</strong><span class="small ${r.mood >= 0 ? 'up' : 'down'}">${r.mood >= 0 ? '▲' : '▼'} ${Number(r.mood).toFixed(2)}</span></div><div class="small">${esc(r.content)}</div></div>`).join('')}</div>` : ''}
        <div class="section"><h3>Headlines</h3>${d.news.length ? d.news.map((a) => `<div class="news-item"><a href="${safeUrl(a.url)}" target="_blank" rel="noopener noreferrer">${esc(a.title)}</a><div class="meta"><span>${esc(a.outlet)}</span><span>${esc(timeAgo(a.published_at || a.fetched_at))}</span>${a.sentiment != null ? chip(`tone ${Number(a.sentiment).toFixed(2)}`) : ''}</div></div>`).join('') : '<p class="small muted">No recent headlines.</p>'}</div>`;
      document.getElementById('country-close').addEventListener('click', () => { panel.classList.remove('open'); location.hash = '#globe'; this.renderRightDefault(); });
      document.getElementById('analyze-region').addEventListener('click', () => this.analyze({ kind: 'region_analysis', region: iso }));
      const am = document.getElementById('analyze-market');
      if (am) am.addEventListener('click', () => this.analyze({ kind: 'market_outlook', region: iso }));
      box.querySelectorAll('[data-symbol]').forEach((r) => r.addEventListener('click', () => this.openMarket(r.dataset.symbol)));
      box.querySelectorAll('[data-hazard]').forEach((r) => r.addEventListener('click', () => {
        const h = d.hazards.find((x) => String(x.id) === r.dataset.hazard); if (h) this.showHazard(h);
      }));
    },

    showHazard(h) {
      window.WorldGlobe.focus(h.lat, h.lon, 1.4);
      const insight = h.insight_id || (this.hazards.find((x) => x.id === h.id) || {}).insight_id;
      this.openModal(`<div class="meta">${chip(h.kind)}${h.alert_level ? chip(h.alert_level + ' alert', ALERT_STATUS[h.alert_level] || '') : ''}${chip(h.source)}</div><h2 style="margin-top:6px">${esc(h.title)}</h2>`,
        `<div class="tiles">${window.R.tile('Magnitude / severity', h.magnitude ? `M${Number(h.magnitude).toFixed(1)}` : h.severity.toFixed(2), `severity score ${h.severity.toFixed(2)}`)}
          ${window.R.tile('When', timeAgo(h.occurred_at), fmtDateTime(h.occurred_at))}
          ${window.R.tile('Where', h.country_name || h.place || '–', h.depth_km != null ? `depth ${Math.round(h.depth_km)} km` : '')}
          ${h.near_hq_km != null ? window.R.tile('Distance to your HQ', `${num(h.near_hq_km, 0)} km`, '') : ''}</div>
          ${h.url ? `<p><a href="${safeUrl(h.url)}" target="_blank" rel="noopener noreferrer">Official event page →</a></p>` : ''}
          <div class="row">${insight ? `<button class="btn primary" data-insight="${insight}">Open AI impact analysis</button>` : ''}
          ${h.country ? `<button class="btn" id="hz-analyze">${insight ? 'Re-analyze' : 'Analyze impact with AI'}</button>` : '<span class="small muted">Open ocean - no country affected.</span>'}</div>
          <p class="small muted">The impact analysis covers market & currency reaction, what people need, and whether your startup can help - with a simulated estimate of the attention it may bring.</p>`);
      const b = document.getElementById('hz-analyze');
      if (b) b.addEventListener('click', () => this.analyze({ kind: 'geo_impact', event_id: h.id, force: !!insight }));
    },

    async openMarket(symbol) {
      let d;
      try { d = await API.get(`/api/world/markets/${encodeURIComponent(symbol)}`); } catch (e) { this.toast({ level: 'warning', title: symbol, message: e.message }); return; }
      const a = d.analytics || {}, mc = d.monte_carlo;
      this.openModal(`<div class="meta">${chip(d.kind)}${d.country ? chip(d.country) : ''}<span>updated ${esc(timeAgo(d.fetched_at))}</span></div><h2 style="margin-top:6px">${esc(d.name)} <span class="muted small">${esc(d.symbol)}</span></h2>`,
        `<div class="tiles">${window.R.tile('Last', num(a.last, 2), d.currency || '')}${window.R.tile('Today', pct(a.chg_1d), `5 days ${pct(a.chg_5d)}`)}
          ${window.R.tile('1 month', pct(a.chg_1m, 1), `3 months ${pct(a.chg_3m, 1)}`)}${window.R.tile('vs 52-week high', pct(a.drawdown_pct, 1), a.regime || '')}
          ${window.R.tile('Volatility (20d, ann.)', a.vol_20d_ann != null ? `${a.vol_20d_ann}%` : '–', a.rsi14 != null ? `RSI ${a.rsi14}` : '')}</div>
          <div class="chart-card"><h3>Price and Monte Carlo outlook</h3><div class="sub">${mc ? `${mc.paths} bootstrap paths scaled to today's volatility · median ${pct(mc.median_pct, 1)} in ${mc.horizon_days} trading days · P(fall >5%) ${Math.round(mc.prob_down_5 * 100)}% · P(rise >5%) ${Math.round(mc.prob_up_5 * 100)}%` : 'Not enough history for a forecast'}</div>
          <div class="chart-box tall"><canvas id="market-chart" role="img" aria-label="Price history and forecast"></canvas></div>
          ${window.Charts.table((d.history || []).slice(-60).map((p) => new Date(p[0] * 1000).toLocaleDateString()), [{ name: 'Close', data: (d.history || []).slice(-60).map((p) => p[1]) }], { format: (x) => num(x, 2) })}</div>
          ${d.calibration ? `<p class="small muted">Simulation model calibrated to this index ${esc(timeAgo(d.calibration.created_at))} (loss ${d.calibration.loss.toFixed(2)}).</p>` : ''}
          <div class="row">${d.outlook ? `<button class="btn" data-insight="${d.outlook.id}">Latest AI outlook (${esc(timeAgo(d.outlook.created_at))})</button>` : ''}
          ${d.country ? '<button class="btn primary" id="mk-analyze">Ask the AI what happens next</button>' : ''}</div>
          <p class="small muted">Forecasts are model estimates, not financial advice.</p>`);
      window.Charts.fan(document.getElementById('market-chart'), { history: d.history, mc, yFormat: (v) => num(v, 0) });
      const b = document.getElementById('mk-analyze');
      if (b) b.addEventListener('click', () => this.analyze({ kind: 'market_outlook', region: d.country }));
    },

    async analyze(body) {
      try {
        const res = await API.post('/api/insights/analyze', body);
        if (res.reused && res.insight) {
          this.toast({ level: 'info', title: 'Recent analysis reused', message: res.message, link: `#insight/${res.insight.id}` });
          this.openInsight(res.insight.id);
        } else {
          this.toast({ level: 'info', title: res.created ? 'AI analysis queued' : 'Already queued', message: `${res.message} You'll get a live notification when it's ready.`, link: '#tasks' });
        }
      } catch (e) { this.toast({ level: 'warning', title: 'Not queued', message: e.message }); }
    },

    /* ------------------------------------------------------------ modal & insights */
    openModal(head, body) {
      document.getElementById('modal-head').innerHTML = head;
      document.getElementById('modal-body').innerHTML = body;
      document.getElementById('modal').hidden = false;
    },
    closeModal() {
      const m = document.getElementById('modal');
      if (m.hidden) return;
      m.hidden = true;
      if (location.hash.startsWith('#insight/')) history.replaceState(null, '', `#${this.lastView || 'globe'}`);
    },
    async openInsight(id, pushHash = true) {
      let i;
      try { i = await API.get(`/api/insights/${id}`); } catch (e) { this.toast({ level: 'warning', title: 'Insight', message: e.message }); return; }
      if (pushHash && !location.hash.startsWith('#insight/')) history.replaceState(null, '', `#insight/${id}`);
      this.openModal(window.R.head(i), window.R.body(i));
    },
    async loadInsights() {
      const box = document.getElementById('insight-list');
      const q = document.getElementById('insight-search').value.trim();
      const kind = document.getElementById('insight-kind').value;
      try {
        const data = q.length >= 2 ? await API.get(`/api/insights/search?q=${encodeURIComponent(q)}`)
                                    : await API.get(`/api/insights?limit=60${kind ? `&kind=${kind}` : ''}`);
        box.innerHTML = data.items.length ? `<div class="tiles" style="grid-template-columns:repeat(auto-fill,minmax(300px,1fr))">${data.items.map((i) => window.R.insightCard(i)).join('')}</div>`
          : '<div class="empty">No insights yet. They appear as the AI analyses headlines, markets, hazards and simulations.</div>';
      } catch (e) { box.innerHTML = `<div class="empty">${esc(e.message)}</div>`; }
    },

    /* ------------------------------------------------------------ notifications */
    toast(n) {
      const box = document.getElementById('toasts');
      const el = document.createElement('div');
      el.className = `toast ${n.level || 'info'}`;
      el.innerHTML = `<strong>${esc(n.title)}</strong>${n.message ? `<div class="small">${esc(n.message)}</div>` : ''}`;
      el.addEventListener('click', () => { if (n.link) location.hash = n.link; el.remove(); });
      box.appendChild(el);
      while (box.children.length > 4) box.firstChild.remove();
      if (n.level !== 'critical') setTimeout(() => el.remove(), 7000);
    },
    updateBell() {
      const b = document.getElementById('bell-count');
      b.hidden = this.unread <= 0;
      b.textContent = this.unread > 99 ? '99+' : String(this.unread);
    },
    toggleDrawer(force) {
      const d = document.getElementById('notif-drawer');
      d.hidden = force === undefined ? !d.hidden : !force;
      if (!d.hidden) this.loadNotifications();
    },
    async loadNotifications() {
      try {
        const data = await API.get('/api/notifications?limit=60');
        this.unread = data.unread; this.updateBell();
        document.getElementById('notif-list').innerHTML = data.items.length ? data.items.map((n) => `<div class="news-item" style="${n.read ? 'opacity:.7' : ''}">
            <div class="row"><span class="dot ${LEVEL_DOT[n.level] || 'muted'}"></span><strong class="grow">${n.link ? `<a href="${esc(n.link)}" data-nid="${n.id}">${esc(n.title)}</a>` : esc(n.title)}</strong></div>
            ${n.message ? `<div class="small">${esc(n.message)}</div>` : ''}<div class="small muted">${esc(fmtDateTime(n.created_at))}</div></div>`).join('')
          : '<div class="empty">No notifications.</div>';
        document.querySelectorAll('[data-nid]').forEach((a) => a.addEventListener('click', () => API.post('/api/notifications/read', { ids: [+a.dataset.nid] }).catch(() => {})));
      } catch (e) { /* ignore */ }
    },

    /* ------------------------------------------------------------ tasks */
    async loadTasks() {
      try {
        const data = await API.get('/api/tasks?limit=100');
        const counts = { queued: 0, running: 0 };
        data.items.forEach((t) => { if (counts[t.status] != null) counts[t.status] += 1; });
        document.getElementById('task-counts').textContent = `${counts.running} running · ${counts.queued} queued`;
        const statusCls = { done: 'good', failed: 'critical', running: 'info', queued: '', cancelled: '' };
        document.getElementById('task-table').innerHTML = `<thead><tr><th>#</th><th>Task</th><th>Status</th><th>Progress</th><th>By</th><th>Created</th><th>Took</th><th></th></tr></thead><tbody>
          ${data.items.map((t) => {
            const took = t.started_at && t.finished_at ? `${Math.round((new Date(t.finished_at) - new Date(t.started_at)) / 1000)}s` : '';
            return `<tr><td>${t.id}</td><td style="text-align:left">${esc(t.kind)}<div class="muted">${esc(t.lane)} lane${t.attempts > 1 ? ` · attempt ${t.attempts}` : ''}</div></td>
              <td style="text-align:left">${chip(t.status, statusCls[t.status] || '')}</td>
              <td style="text-align:left;min-width:180px"><div class="progress"><i style="width:${Math.round((t.progress || 0) * 100)}%"></i></div><div class="muted" style="white-space:normal">${esc(t.error && t.status !== 'done' ? t.error : t.progress_note || '')}</div></td>
              <td>${esc(t.requested_by)}</td><td>${esc(fmtDateTime(t.created_at))}</td><td>${took}</td>
              <td>${['queued', 'running'].includes(t.status) ? `<button class="btn sm" data-cancel="${t.id}">Cancel</button>` : ''}</td></tr>`;
          }).join('')}</tbody>`;
        document.querySelectorAll('[data-cancel]').forEach((b) => b.addEventListener('click', async () => {
          try { await API.post(`/api/tasks/${b.dataset.cancel}/cancel`); this.loadTasks(); } catch (e) { this.toast({ level: 'warning', title: 'Cancel', message: e.message }); }
        }));
      } catch (e) { /* ignore */ }
    },

    /* ------------------------------------------------------------ profile & system */
    async loadProfileView() {
      await this.refreshStatus();
      const card = document.getElementById('profile-form-card');
      card.innerHTML = window.ProfileForm.html(this.profile || {}, { submitLabel: 'Save changes' }) +
        '<p class="small" style="margin-top:12px"><a href="#" id="redo-onboarding">Redo the onboarding chat with the AI</a></p>';
      window.ProfileForm.bind(card, async (data) => {
        const r = await API.put('/api/profile', data);
        this.profile = r.profile;
        this.toast({ level: 'success', title: 'Profile saved', message: 'Recommendations now follow the updated capabilities.' });
      });
      document.getElementById('redo-onboarding').addEventListener('click', async (e) => {
        e.preventDefault();
        const r = await API.post('/api/onboarding/reset');
        window.Onboarding.open(r.messages);
      });
      const st = this.status || {};
      document.getElementById('ai-chain').innerHTML = this.chainTable(st.ai || {}) +
        `<p class="small muted" style="margin-top:8px">Embeddings: ${esc((st.embeddings || {}).model || '–')} (${(st.embeddings || {}).dimensions || ''} dims, pgvector). If no tier answers, the app stops and says the AI is not present.</p>`;
      document.getElementById('schedule').innerHTML = `<table class="data"><thead><tr><th>Job</th><th>Every</th><th>Last run</th><th>Next</th></tr></thead><tbody>
        ${(st.schedule || []).map((j) => `<tr><td>${esc(j.label)}</td><td>${j.every_s >= 86400 ? `${Math.round(j.every_s / 86400)} d` : j.every_s >= 3600 ? `${Math.round(j.every_s / 3600)} h` : `${Math.round(j.every_s / 60)} min`}</td>
          <td>${esc(j.last ? timeAgo(j.last) : 'pending')}</td><td>${esc(j.next ? timeAgo(j.next) : '–')}</td></tr>`).join('')}</tbody></table>`;
      try {
        const src = await API.get('/api/system/sources');
        const cls = { ok: 'good', stale: 'warning', failed: 'critical', empty: 'warning' };
        document.getElementById('sources').innerHTML = `<p class="small muted" style="margin-top:0">Headlines come only from this allow-list, collected once every ${src.refresh_every_hours} hours.</p>
          <table class="data"><thead><tr><th>Outlet</th><th>Feed</th><th>Last run</th></tr></thead><tbody>
          ${src.news.map((s) => `<tr><td>${esc(s.outlet)}</td><td>${esc(s.feed)}</td><td>${s.last ? `${chip(s.last.status, cls[s.last.status] || '')} ${s.last.kept} kept` : '<span class="muted">–</span>'}</td></tr>`).join('')}</tbody></table>
          <ul class="bullets small">${src.other.map((o) => `<li><strong>${esc(o.name)}</strong> - ${esc(o.use)}</li>`).join('')}</ul>`;
      } catch (e) { /* ignore */ }
    },

    afterProfileSaved() {
      API.get('/api/profile').then((p) => { this.profile = p.profile; this.renderRightDefault(); this.loadWorld(); });
    },
  };

  window.App = App;
  document.addEventListener('DOMContentLoaded', () => App.boot().catch((e) => console.error(e)));
})();
