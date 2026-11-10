/* API client + small helpers shared by every module. */
(function () {
  'use strict';

  class ApiError extends Error {
    constructor(status, detail, data) { super(detail); this.status = status; this.data = data; }
  }

  async function request(method, url, body) {
    const opts = { method, credentials: 'same-origin', headers: { 'X-WorldSim': '1' } };
    if (body !== undefined) { opts.headers['Content-Type'] = 'application/json'; opts.body = JSON.stringify(body); }
    let res;
    try { res = await fetch(url, opts); } catch (e) { throw new ApiError(0, 'Server not reachable', null); }
    if (res.status === 401) { location.href = '/login'; throw new ApiError(401, 'Not logged in', null); }
    let data = null;
    try { data = await res.json(); } catch (e) { data = null; }
    if (!res.ok) {
      let detail = (data && data.detail) || res.statusText || 'Request failed';
      if (Array.isArray(detail)) detail = detail.map(d => d.msg || JSON.stringify(d)).join('; ');
      if (res.status === 503 && data && data.detail === 'AI is not present' && window.App) window.App.showLock(data.ai);
      throw new ApiError(res.status, String(detail), data);
    }
    return data;
  }

  const API = {
    get: (url) => request('GET', url),
    post: (url, body) => request('POST', url, body === undefined ? {} : body),
    put: (url, body) => request('PUT', url, body),
    ApiError,
  };

  const ESC = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };
  function esc(value) { return String(value == null ? '' : value).replace(/[&<>"']/g, (c) => ESC[c]); }
  function safeUrl(u) { return /^https?:\/\//i.test(String(u || '')) ? esc(u) : '#'; }

  function fmtDateTime(iso) {
    if (!iso) return '–';
    const d = new Date(iso);
    if (isNaN(d)) return '–';
    return d.toLocaleString(undefined, { day: '2-digit', month: 'short', year: 'numeric', hour: '2-digit', minute: '2-digit' });
  }
  function timeAgo(iso) {
    if (!iso) return '';
    const s = (Date.now() - new Date(iso).getTime()) / 1000;
    if (isNaN(s)) return '';
    if (s < 0) {
      const f = -s;
      if (f < 3600) return 'in ' + Math.max(1, Math.ceil(f / 60)) + ' min';
      if (f < 86400) return 'in ' + Math.round(f / 3600) + ' h';
      return 'in ' + Math.round(f / 86400) + ' d';
    }
    if (s < 60) return 'just now';
    if (s < 3600) return Math.floor(s / 60) + ' min ago';
    if (s < 86400) return Math.floor(s / 3600) + ' h ago';
    return Math.floor(s / 86400) + ' d ago';
  }
  function num(v, digits = 2) {
    if (v == null || isNaN(v)) return '–';
    return Number(v).toLocaleString(undefined, { maximumFractionDigits: digits, minimumFractionDigits: Math.min(digits, 0) });
  }
  function pct(v, digits = 2, signed = true) {
    if (v == null || isNaN(v)) return '–';
    const s = Number(v).toFixed(digits);
    return (signed && v > 0 ? '+' : '') + s + '%';
  }
  /* Direction is shown with an arrow + text colour, never colour alone. */
  function deltaHtml(v, digits = 2) {
    if (v == null || isNaN(v)) return '<span class="muted">–</span>';
    const cls = v > 0 ? 'up' : v < 0 ? 'down' : 'muted';
    const arrow = v > 0 ? '▲' : v < 0 ? '▼' : '•';
    return `<span class="${cls}">${arrow} ${esc(pct(v, digits))}</span>`;
  }
  const ALERT_STATUS = { red: 'critical', orange: 'serious', yellow: 'warning', green: 'good' };
  const SEVERITY_STATUS = { critical: 'critical', warning: 'warning', watch: 'warning', info: 'info', success: 'success' };
  function chip(text, cls = '') { return `<span class="chip ${esc(cls)}">${esc(text)}</span>`; }
  function debounce(fn, ms) { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; }
  function storeGet(key) { try { return localStorage.getItem(key); } catch (e) { return null; } }
  function storeSet(key, val) { try { localStorage.setItem(key, val); } catch (e) { /* storage blocked */ } }

  window.API = API;
  window.U = { esc, safeUrl, fmtDateTime, timeAgo, num, pct, deltaHtml, chip, debounce, storeGet, storeSet,
               ALERT_STATUS, SEVERITY_STATUS };
})();
