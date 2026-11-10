/* Theme-aware charts (Chart.js) + SVG sparklines + table views.
   Rules: thin 2px lines, one y-axis per chart, categorical colours in fixed order,
   legend for >= 2 series, crosshair tooltip, a data-table twin for every chart. */
(function () {
  'use strict';
  const { esc } = window.U;
  const mounted = new Map();

  function css(name) { return getComputedStyle(document.documentElement).getPropertyValue(name).trim(); }

  function theme() {
    return {
      series: [1, 2, 3, 4, 5, 6, 7, 8].map((i) => css(`--series-${i}`)),
      text: css('--text-primary'), secondary: css('--text-secondary'), muted: css('--text-muted'),
      grid: css('--grid'), axis: css('--axis'), surface: css('--surface-1'), page: css('--page'),
      good: css('--good'), warning: css('--warning'), serious: css('--serious'), critical: css('--critical'),
      divNeg: css('--div-neg'), divMid: css('--div-mid'), divPos: css('--div-pos'),
      dark: (css('color-scheme') || '').includes('dark'),
    };
  }

  function alpha(hex, a) {
    const h = hex.replace('#', '');
    if (h.length !== 6) return hex;
    const r = parseInt(h.slice(0, 2), 16), g = parseInt(h.slice(2, 4), 16), b = parseInt(h.slice(4, 6), 16);
    return `rgba(${r},${g},${b},${a})`;
  }

  /* Shaded spans (e.g. crash or panic episodes) drawn behind the lines. */
  const spanPlugin = {
    id: 'spans',
    beforeDatasetsDraw(chart, _args, opts) {
      const items = (opts && opts.items) || [];
      if (!items.length) return;
      const { ctx, chartArea, scales } = chart;
      ctx.save();
      for (const s of items) {
        const x0 = scales.x.getPixelForValue(s.start), x1 = scales.x.getPixelForValue(Math.max(s.end, s.start + 1));
        ctx.fillStyle = s.color;
        ctx.fillRect(Math.max(chartArea.left, x0), chartArea.top, Math.max(2, Math.min(chartArea.right, x1) - Math.max(chartArea.left, x0)), chartArea.bottom - chartArea.top);
      }
      ctx.restore();
    },
  };
  if (window.Chart) window.Chart.register(spanPlugin);

  function baseOptions(t, { yFormat, legend = true, yTitle, beginAtZero = false } = {}) {
    return {
      responsive: true, maintainAspectRatio: false, animation: { duration: 250 },
      interaction: { mode: 'index', intersect: false },
      plugins: {
        legend: { display: legend, position: 'top', align: 'start',
          labels: { color: t.secondary, boxWidth: 14, boxHeight: 2, padding: 12, font: { size: 11 } } },
        tooltip: {
          backgroundColor: t.surface, titleColor: t.text, bodyColor: t.secondary, borderColor: t.axis, borderWidth: 1,
          padding: 8, boxPadding: 4, usePointStyle: true,
          callbacks: { label: (c) => ` ${c.dataset.label}: ${yFormat ? yFormat(c.parsed.y) : c.formattedValue}` },
        },
        spans: { items: [] },
      },
      scales: {
        x: { grid: { display: false }, border: { color: t.axis }, ticks: { color: t.muted, maxTicksLimit: 8, maxRotation: 0, font: { size: 11 } } },
        y: { beginAtZero, grid: { color: t.grid }, border: { display: false },
             title: { display: !!yTitle, text: yTitle || '', color: t.muted, font: { size: 11 } },
             ticks: { color: t.muted, font: { size: 11 }, callback: yFormat ? (v) => yFormat(v) : undefined } },
      },
      elements: { point: { radius: 0, hoverRadius: 4, hitRadius: 12 }, line: { borderWidth: 2, tension: 0.15 } },
    };
  }

  function mount(canvas, build) {
    if (!canvas || !window.Chart) return null;
    const prev = mounted.get(canvas);
    if (prev && prev.chart) prev.chart.destroy();
    const chart = new window.Chart(canvas.getContext('2d'), build(theme()));
    mounted.set(canvas, { chart, build });
    return chart;
  }

  function rerenderAll() {
    for (const [canvas, entry] of mounted) {
      if (!document.body.contains(canvas)) { entry.chart.destroy(); mounted.delete(canvas); continue; }
      entry.chart.destroy();
      entry.chart = new window.Chart(canvas.getContext('2d'), entry.build(theme()));
    }
  }

  /* series: [{name, data:[...], colorIndex?}]  - colour follows the entity via colorIndex */
  function line(canvas, { labels, series, yFormat, yTitle, spans = [], beginAtZero = false }) {
    return mount(canvas, (t) => {
      const opts = baseOptions(t, { yFormat, legend: series.length > 1, yTitle, beginAtZero });
      opts.plugins.spans.items = spans.map((s) => ({ ...s, color: s.color || alpha(t.critical, t.dark ? 0.18 : 0.10) }));
      return {
        type: 'line',
        data: {
          labels,
          datasets: series.map((s, i) => {
            const color = t.series[(s.colorIndex != null ? s.colorIndex : i) % 8];
            return { label: s.name, data: s.data, borderColor: color, backgroundColor: color, pointBackgroundColor: color,
                     borderDash: s.dashed ? [5, 4] : undefined, borderWidth: s.width || 2 };
          }),
        },
        options: opts,
      };
    });
  }

  /* Price history + Monte Carlo fan (percentile bands) on one axis. */
  function fan(canvas, { history, mc, yFormat }) {
    return mount(canvas, (t) => {
      const hist = (history || []).slice(-120);
      const labels = hist.map((p) => new Date(p[0] * 1000).toLocaleDateString(undefined, { day: '2-digit', month: 'short' }));
      const n = hist.length;
      const h = mc ? mc.horizon_days : 0;
      for (let i = 1; i <= h; i++) labels.push(`+${i}d`);
      const pad = (arr) => Array(n - 1).fill(null).concat([hist.length ? hist[n - 1][1] : null]).concat(arr);
      const c = t.series[0];
      const ds = [{ label: 'Price', data: hist.map((p) => p[1]).concat(Array(h).fill(null)), borderColor: t.text,
                    backgroundColor: t.text, borderWidth: 2 }];
      if (mc) {
        ds.push(
          { label: '95th pct', data: pad(mc.p95), borderColor: alpha(c, 0.0), backgroundColor: alpha(c, 0.0), pointRadius: 0, borderWidth: 0 },
          { label: '5-95% range', data: pad(mc.p5), borderColor: alpha(c, 0.0), backgroundColor: alpha(c, t.dark ? 0.22 : 0.14), fill: '-1', pointRadius: 0, borderWidth: 0 },
          { label: '75th pct', data: pad(mc.p75), borderColor: alpha(c, 0.0), backgroundColor: alpha(c, 0.0), pointRadius: 0, borderWidth: 0 },
          { label: '25-75% range', data: pad(mc.p25), borderColor: alpha(c, 0.0), backgroundColor: alpha(c, t.dark ? 0.38 : 0.26), fill: '-1', pointRadius: 0, borderWidth: 0 },
          { label: 'Median path', data: pad(mc.p50), borderColor: c, backgroundColor: c, borderWidth: 2 },
        );
      }
      const opts = baseOptions(t, { yFormat, legend: true });
      opts.plugins.legend.labels.filter = (item) => !/pct$/.test(item.text);
      return { type: 'line', data: { labels, datasets: ds }, options: opts };
    });
  }

  function spark(values, { width = 88, height = 26 } = {}) {
    const v = (values || []).filter((x) => x != null && !isNaN(x));
    if (v.length < 2) return `<svg class="spark" width="${width}" height="${height}" aria-hidden="true"></svg>`;
    const min = Math.min(...v), max = Math.max(...v), span = max - min || 1;
    const pts = v.map((x, i) => `${(i / (v.length - 1)) * (width - 2) + 1},${height - 2 - ((x - min) / span) * (height - 4)}`).join(' ');
    const change = v[v.length - 1] - v[0];
    const color = change >= 0 ? 'var(--series-1)' : 'var(--series-8)';
    return `<svg class="spark" width="${width}" height="${height}" viewBox="0 0 ${width} ${height}" role="img">` +
      `<title>30-day trend: ${change >= 0 ? 'up' : 'down'}</title>` +
      `<polyline fill="none" stroke="${color}" stroke-width="1.6" stroke-linejoin="round" points="${pts}"/></svg>`;
  }

  /* Data-table twin of a chart (accessibility). */
  function table(labels, series, { maxRows = 400, format } = {}) {
    const fmt = format || ((x) => (x == null ? '–' : typeof x === 'number' ? x.toFixed(3) : esc(x)));
    const rows = labels.map((l, i) => `<tr><td>${esc(l)}</td>${series.map((s) => `<td>${fmt(s.data[i])}</td>`).join('')}</tr>`);
    const step = Math.max(1, Math.ceil(rows.length / maxRows));
    return `<details class="table-view"><summary>Show data table</summary><div class="table-scroll"><table class="data">` +
      `<thead><tr><th>t</th>${series.map((s) => `<th>${esc(s.name)}</th>`).join('')}</tr></thead>` +
      `<tbody>${rows.filter((_, i) => i % step === 0).join('')}</tbody></table></div></details>`;
  }

  window.Charts = { theme, line, fan, spark, table, rerenderAll, alpha };
})();
