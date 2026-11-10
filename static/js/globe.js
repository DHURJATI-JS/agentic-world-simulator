/* 3D globe (globe.gl): choropleth by market move / sentiment / simulated confidence,
   hazards as status-coloured points + pulsing rings, startup HQ, opportunity arcs. */
(function () {
  'use strict';
  const { esc, pct, timeAgo, debounce } = window.U;
  const ISO_FIX = { France: 'FR', Norway: 'NO', Somaliland: 'SO', 'Northern Cyprus': 'CY', Kosovo: 'XK' };

  const RAMPS = {
    light: {
      neg: ['#f0efec', '#f5c8c7', '#ee9493', '#e34948', '#b33232'],
      pos: ['#f0efec', '#b7d3f6', '#6da7ec', '#2a78d6', '#184f95'],
      seq: ['#cde2fb', '#9ec5f4', '#6da7ec', '#3987e5', '#256abf', '#184f95'],
    },
    dark: {
      neg: ['#383835', '#6b3030', '#a23c3c', '#e66767', '#f2a0a0'],
      pos: ['#383835', '#184f95', '#256abf', '#3987e5', '#86b6ef'],
      seq: ['#184f95', '#1c5cab', '#256abf', '#3987e5', '#6da7ec', '#9ec5f4'],
    },
  };
  const STATUS = { red: '--critical', orange: '--serious', yellow: '--warning', green: '--good' };

  const METRICS = {
    market_1d: { label: 'Main stock index, change today', type: 'div', max: 2,
                 get: (c) => c.market && c.market.chg_1d, fmt: (v) => pct(v, 2) },
    market_1m: { label: 'Main stock index, change over 1 month', type: 'div', max: 10,
                 get: (c) => c.market && c.market.chg_1m, fmt: (v) => pct(v, 1) },
    news_sentiment: { label: 'AI-scored news sentiment, last 48h', type: 'div', max: 0.6,
                      get: (c) => c.news_sentiment, fmt: (v) => (v > 0 ? '+' : '') + Number(v).toFixed(2) },
    news_volume: { label: 'Headlines mentioning the country, last 48h', type: 'seq', max: null,
                   get: (c) => c.news_count, fmt: (v) => String(Math.round(v)) },
    sim_conf: { label: 'Simulated consumer confidence (latest run)', type: 'div', max: 0.6,
                get: (c) => c.sim_conf, fmt: (v) => (v > 0 ? '+' : '') + Number(v).toFixed(2) },
  };

  function hex2rgb(h) { const x = h.replace('#', ''); return [0, 2, 4].map((i) => parseInt(x.slice(i, i + 2), 16)); }
  function mix(a, b, t) {
    const A = hex2rgb(a), B = hex2rgb(b);
    return '#' + A.map((v, i) => Math.round(v + (B[i] - v) * t).toString(16).padStart(2, '0')).join('');
  }
  function rampAt(ramp, t) {
    const x = Math.max(0, Math.min(1, t)) * (ramp.length - 1);
    const i = Math.min(ramp.length - 2, Math.floor(x));
    return mix(ramp[i], ramp[i + 1], x - i);
  }
  function css(name) { return getComputedStyle(document.documentElement).getPropertyValue(name).trim(); }
  function rgba(hex, a) { const [r, g, b] = hex2rgb(hex); return `rgba(${r},${g},${b},${a})`; }

  const WG = {
    globe: null, el: null, features: [], data: { countries: {}, hq: null, opportunities: [] }, hazards: [],
    metric: 'market_1d', showHazards: true, satellite: false, hovered: null, seqMax: 1,

    async init(el, handlers) {
      this.el = el;
      this.handlers = handlers || {};
      const geo = await fetch('/static/vendor/countries.geojson').then((r) => r.json());
      this.features = geo.features
        .filter((f) => f.properties.ADMIN !== 'Antarctica')
        .map((f) => {
          const p = f.properties;
          let iso = p.ISO_A2;
          if (!iso || iso === '-99') iso = ISO_FIX[p.ADMIN] || null;
          f.iso = iso;
          return f;
        });
      const g = new window.Globe(el, { animateIn: true });
      this.globe = g;
      g.showGraticules(false)
        .polygonsData(this.features)
        .polygonAltitude((f) => this.altitude(f))
        .polygonCapColor((f) => this.capColor(f))
        .polygonSideColor(() => (this.dark() ? 'rgba(255,255,255,0.05)' : 'rgba(0,0,0,0.06)'))
        .polygonStrokeColor(() => css('--land-stroke'))
        .polygonLabel((f) => this.countryTooltip(f))
        .polygonsTransitionDuration(250)
        .onPolygonHover((f) => {
          this.hovered = f;
          g.polygonAltitude((d) => this.altitude(d));
          el.style.cursor = f ? 'pointer' : 'grab';
        })
        .onPolygonClick((f) => { if (f && f.iso && this.handlers.onCountry) this.handlers.onCountry(f.iso); })
        .pointLat('lat').pointLng('lon')
        .pointAltitude((d) => 0.012 + d.severity * 0.1)
        .pointRadius((d) => 0.22 + (d.magnitude && d.kind === 'earthquake' ? Math.max(0, d.magnitude - 4.5) * 0.18 : d.severity * 0.6))
        .pointColor((d) => this.statusColor(d.alert_level))
        .pointLabel((d) => this.hazardTooltip(d))
        .pointsMerge(false)
        .onPointClick((d) => { if (this.handlers.onHazard) this.handlers.onHazard(d); })
        .ringLat('lat').ringLng('lon')
        .ringColor((d) => (t) => rgba(d.color, Math.max(0, 1 - t)))
        .ringMaxRadius((d) => d.maxR)
        .ringPropagationSpeed((d) => d.speed)
        .ringRepeatPeriod((d) => d.period)
        .labelLat('lat').labelLng('lon').labelText('label').labelSize(0.85).labelDotRadius(0.45)
        .labelColor(() => css('--text-primary')).labelResolution(2).labelAltitude(0.015)
        .arcStartLat((d) => d.startLat).arcStartLng((d) => d.startLng).arcEndLat((d) => d.endLat).arcEndLng((d) => d.endLng)
        .arcColor((d) => d.colors).arcDashLength(0.45).arcDashGap(0.25).arcDashAnimateTime(2400).arcStroke(0.6)
        .arcLabel((d) => `<div class="tt-title">${esc(d.title)}</div><div class="muted">Your startup can help here</div>`);
      const mat = g.globeMaterial();
      this.oceanColor = mat.color ? mat.color : new mat.emissive.constructor('#dde7f1');
      g.controls().autoRotateSpeed = 0.35;
      g.pointOfView({ lat: 22, lng: 60, altitude: 2.4 }, 0);
      this.applyTheme();
      this.resize();
      window.addEventListener('resize', debounce(() => this.resize(), 150));
    },

    dark() { return (css('color-scheme') || '').includes('dark'); },
    resize() { if (this.globe && this.el) this.globe.width(this.el.clientWidth).height(this.el.clientHeight); },

    applyTheme() {
      const g = this.globe;
      if (!g) return;
      g.backgroundColor(css('--page'));
      g.atmosphereColor(this.dark() ? '#3987e5' : '#86b6ef').atmosphereAltitude(0.14);
      const mat = g.globeMaterial();
      if (this.satellite) {
        g.globeImageUrl(this.dark() ? '/static/vendor/img/earth-night.jpg' : '/static/vendor/img/earth-day.jpg');
      } else {
        g.globeImageUrl(null);
        mat.map = null;
        mat.color = this.oceanColor;
        mat.color.set(css('--ocean'));
        mat.needsUpdate = true;
      }
      this.refresh();
    },

    altitude(f) { return f === this.hovered ? 0.03 : (this.value(f) == null ? 0.005 : 0.012); },

    statusColor(alert) { return css(STATUS[alert] || '--text-muted'); },

    value(f) {
      const c = f.iso && this.data.countries[f.iso];
      if (!c) return null;
      const v = METRICS[this.metric].get(c);
      return v == null || isNaN(v) ? null : Number(v);
    },

    capColor(f) {
      const v = this.value(f);
      const ramps = this.dark() ? RAMPS.dark : RAMPS.light;
      const a = this.satellite ? 0.78 : 1;
      if (v == null) return this.satellite ? 'rgba(0,0,0,0)' : css('--land');
      const m = METRICS[this.metric];
      let hex;
      if (m.type === 'seq') hex = rampAt(ramps.seq, v / (this.seqMax || 1));
      else {
        const t = Math.max(-1, Math.min(1, v / m.max));
        hex = rampAt(t < 0 ? ramps.neg : ramps.pos, Math.abs(t));
      }
      return a < 1 ? rgba(hex, a) : hex;
    },

    countryTooltip(f) {
      const c = (f.iso && this.data.countries[f.iso]) || {};
      const rows = [];
      if (c.market) rows.push(['Index', `${esc(c.market.name)}`], ['Today', pct(c.market.chg_1d)], ['1 month', pct(c.market.chg_1m, 1)]);
      if (c.news_count) rows.push(['Headlines (48h)', c.news_count], ['News sentiment', c.news_sentiment != null ? Number(c.news_sentiment).toFixed(2) : '–']);
      if (c.hazards) rows.push(['Hazards (7d)', c.hazards]);
      if (c.sim_conf != null) rows.push(['Sim. confidence', Number(c.sim_conf).toFixed(2)]);
      return `<div class="tt-title">${esc(f.properties.NAME)}</div>` +
        (rows.length ? rows.map(([k, v]) => `<div class="tt-row"><span class="muted">${esc(k)}</span><span>${esc(v)}</span></div>`).join('')
                     : '<div class="muted">No data yet</div>') +
        '<div class="muted" style="margin-top:4px">Click for details</div>';
    },

    hazardTooltip(d) {
      const mag = d.magnitude ? `M${Number(d.magnitude).toFixed(1)} · ` : '';
      const dist = d.near_hq_km != null ? `<div>${Math.round(d.near_hq_km).toLocaleString()} km from your HQ</div>` : '';
      return `<div class="tt-title">${esc(d.title)}</div><div>${mag}${esc(d.kind)} · alert: ${esc(d.alert_level || 'n/a')}</div>` +
        `<div class="muted">${esc(timeAgo(d.occurred_at))} · ${esc(d.source)}</div>${dist}`;
    },

    setData(overview) {
      this.data = overview || this.data;
      const vols = Object.values(this.data.countries || {}).map((c) => c.news_count || 0);
      this.seqMax = Math.max(1, ...vols);
      this.refresh();
    },
    setHazards(list) { this.hazards = list || []; this.refresh(); },
    setMetric(m) { this.metric = m; this.refresh(); },
    setShowHazards(on) { this.showHazards = on; this.refresh(); },
    setSatellite(on) { this.satellite = on; this.applyTheme(); },
    setRotate(on) { if (this.globe) this.globe.controls().autoRotate = on; },

    refresh() {
      const g = this.globe;
      if (!g) return;
      g.polygonCapColor((f) => this.capColor(f)).polygonAltitude((f) => this.altitude(f));
      const recent = this.hazards.filter((h) => Date.now() - new Date(h.occurred_at).getTime() < 7 * 86400e3);
      g.pointsData(this.showHazards ? recent : []);
      const rings = this.showHazards
        ? recent.filter((h) => h.severity >= 0.5 && Date.now() - new Date(h.occurred_at).getTime() < 3 * 86400e3)
            .map((h) => ({ lat: h.lat, lon: h.lon, color: this.statusColor(h.alert_level || 'orange'), maxR: 2 + h.severity * 6, speed: 2.2, period: 1300 }))
        : [];
      const hq = this.data.hq;
      if (hq && hq.lat != null) rings.push({ lat: hq.lat, lon: hq.lon, color: css('--accent'), maxR: 2.2, speed: 0.9, period: 2600 });
      g.ringsData(rings);
      g.labelsData(hq && hq.lat != null ? [{ lat: hq.lat, lon: hq.lon, label: `HQ · ${hq.city || hq.name || ''}` }] : []);
      const accent = css('--accent'), good = css('--good');
      g.arcsData(hq && hq.lat != null ? (this.data.opportunities || []).map((o) => ({
        startLat: hq.lat, startLng: hq.lon, endLat: o.lat, endLng: o.lon, colors: [accent, good], title: o.title,
      })) : []);
      this.renderLegend();
    },

    renderLegend() {
      const el = document.getElementById('globe-legend');
      if (!el) return;
      const m = METRICS[this.metric];
      const ramps = this.dark() ? RAMPS.dark : RAMPS.light;
      let ramp, lo, mid, hi;
      if (m.type === 'seq') {
        ramp = `linear-gradient(90deg, ${ramps.seq.join(',')})`; lo = '0'; mid = ''; hi = m.fmt(this.seqMax);
      } else {
        const stops = ramps.neg.slice().reverse().concat(ramps.pos.slice(1));
        ramp = `linear-gradient(90deg, ${stops.join(',')})`; lo = m.fmt(-m.max); mid = '0'; hi = m.fmt(m.max);
      }
      let html = `<div><strong>${esc(m.label)}</strong></div><div class="legend-ramp" style="background:${ramp}"></div>` +
        `<div class="legend-scale"><span>${esc(lo)}</span><span>${esc(mid)}</span><span>${esc(hi)}</span></div>` +
        `<div class="small muted" style="margin-top:4px"><span class="dot" style="background:${css('--land')};border:1px solid var(--border-strong)"></span> no data</div>`;
      if (m.type === 'div' && this.metric.startsWith('market')) html += '<div class="small muted">Blue = up, red = down</div>';
      if (this.showHazards) {
        html += '<div class="legend-status">' + [['green', 'Green'], ['yellow', 'Yellow'], ['orange', 'Orange'], ['red', 'Red']]
          .map(([k, label]) => `<span class="small"><span class="dot ${window.U.ALERT_STATUS[k]}"></span> ${label} alert</span>`).join('') + '</div>';
      }
      el.innerHTML = html;
    },

    focus(lat, lon, altitude = 1.6) { if (this.globe && lat != null) this.globe.pointOfView({ lat, lng: lon, altitude }, 1000); },
    focusCountry(iso) {
      const c = this.data.countries && this.data.countries[iso];
      if (c && c.lat != null) { this.focus(c.lat, c.lon, 1.7); return; }
      const f = this.features.find((x) => x.iso === iso);
      if (f) {
        const coords = JSON.stringify(f.geometry.coordinates).match(/-?\d+\.?\d*/g).map(Number);
        const lons = coords.filter((_, i) => i % 2 === 0), lats = coords.filter((_, i) => i % 2 === 1);
        this.focus((Math.min(...lats) + Math.max(...lats)) / 2, (Math.min(...lons) + Math.max(...lons)) / 2, 1.7);
      }
    },
  };

  window.WorldGlobe = WG;
})();
