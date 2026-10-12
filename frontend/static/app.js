/* RootRise FRD v4.0 — vanilla JS, no build step. */
(() => {
  'use strict';

  const API = '/api';
  const TOKEN_KEY = 'frd.token';
  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

  const STATUS_LABELS = {
    researching: 'Researching', preparing: 'Preparing', submitted: 'Applied', in_review: 'Interview',
    accepted: 'Won', rejected: 'Lost', closed: 'Closed',
  };
  const TYPE_LABELS = {
    accelerator: 'Accelerators', grant: 'Grants', vc_fund: 'VC funds', angel: 'Angel', corporate: 'Corporate',
    government: 'Government', competition: 'Competitions', cloud_credits: 'Cloud credits', rfp: 'RFPs',
    tender: 'Tenders', consulting: 'Consulting', procurement: 'Procurement', other: 'Other',
  };
  const TYPE_ORDER = Object.keys(TYPE_LABELS);
  const READINESS_CATEGORIES = ['legal', 'financial', 'pitch', 'technical', 'team', 'product', 'market', 'operations', 'documentation'];
  const READINESS_STATUS = { not_started: 'Not started', in_progress: 'In progress', complete: 'Complete', blocked: 'Blocked' };

  const state = {
    view: 'dashboard',
    pipeline: 'all',       // all | startup | bd
    type: 'all',           // all | <type>
    flag: null,            // null | starred | needs_check | expired
    q: '',
    sort: 'deadline:asc',
    readinessFilter: 'all',
    opps: [], counts: null, stats: null, items: [], activity: [],
    authMode: 'off',
  };

  // ── utilities ──
  const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const safeUrl = (u) => (/^https?:\/\//i.test(u || '') ? u : null);
  const fmtDate = (iso) => iso ? new Date(iso).toLocaleDateString(undefined, { day: 'numeric', month: 'short', year: 'numeric', timeZone: 'UTC' }) : '';
  const dateOnly = (iso) => (iso ? iso.slice(0, 10) : '');   // deadlines are stored at UTC midnight
  const daysUntil = (iso) => {
    if (!iso) return null;
    const d = new Date(iso);
    const today = new Date(); const t = Date.UTC(today.getFullYear(), today.getMonth(), today.getDate());
    return Math.round((Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate()) - t) / 86400000);
  };
  const ago = (iso) => {
    const mins = Math.floor((Date.now() - new Date(iso)) / 60000);
    if (mins < 1) return 'just now';
    if (mins < 60) return `${mins}m ago`;
    const h = Math.floor(mins / 60); if (h < 24) return `${h}h ago`;
    const d = Math.floor(h / 24); if (d < 7) return `${d}d ago`;
    return fmtDate(iso);
  };
  const plural = (n, w) => `${n} ${n === 1 ? w : w.endsWith('y') ? `${w.slice(0, -1)}ies` : `${w}s`}`;
  const debounce = (fn, ms) => { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; };
  const store = {
    get(k) { try { return localStorage.getItem(k); } catch { return null; } },
    set(k, v) { try { v == null ? localStorage.removeItem(k) : localStorage.setItem(k, v); } catch { /* private mode */ } },
  };

  function toast(msg, kind = 'info') {
    const el = document.createElement('div');
    el.className = `toast ${kind}`; el.textContent = msg;
    $('#toasts').appendChild(el);
    setTimeout(() => el.remove(), 3500);
  }

  // ── API with auth ──
  class AuthError extends Error {}

  async function api(path, opts = {}) {
    const headers = { 'Content-Type': 'application/json', ...(opts.headers || {}) };
    const token = store.get(TOKEN_KEY);
    if (token) headers.Authorization = `Bearer ${token}`;
    const res = await fetch(API + path, { ...opts, headers });
    if (res.status === 401) {
      store.set(TOKEN_KEY, null);
      const unlocked = await askPasscode(opts.method && opts.method !== 'GET'
        ? 'Saving changes needs the team passcode.' : 'Enter the team passcode to continue.');
      if (unlocked) return api(path, opts);
      throw new AuthError('Authentication required');
    }
    if (!res.ok) {
      let detail = `${res.status}`;
      try { const b = await res.json(); detail = typeof b.detail === 'string' ? b.detail : JSON.stringify(b.detail); } catch { /* not json */ }
      throw new Error(detail);
    }
    const body = await res.json();
    return { body, headers: res.headers };
  }
  const get = async (p) => (await api(p)).body;

  // ── passcode screen ──
  let lockResolve = null;
  function askPasscode(message) {
    if (lockResolve) return new Promise((r) => { const prev = lockResolve; lockResolve = (v) => { prev(v); r(v); }; });
    $('#lock-msg').textContent = message;
    $('#lock-err').textContent = '';
    $('#lock-cancel').hidden = state.authMode === 'full';
    $('#lock').classList.add('open');
    setTimeout(() => $('#lock-form input').focus(), 30);
    return new Promise((resolve) => { lockResolve = resolve; });
  }
  function closeLock(result) {
    $('#lock').classList.remove('open');
    const r = lockResolve; lockResolve = null; if (r) r(result);
  }
  $('#lock-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    const passcode = e.target.passcode.value;
    const res = await fetch(`${API}/auth/login`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ passcode }) });
    if (res.ok) {
      const b = await res.json();
      store.set(TOKEN_KEY, b.token);
      e.target.reset();
      $('#sign-out').hidden = false;
      closeLock(true);
    } else {
      $('#lock-err').textContent = res.status === 429 ? 'Too many attempts. Wait 10 minutes.' : 'Wrong passcode.';
    }
  });
  $('#lock-cancel').addEventListener('click', () => closeLock(false));
  $('#sign-out').addEventListener('click', () => { store.set(TOKEN_KEY, null); location.reload(); });

  // ── render helpers ──
  const loading = (n = 3, cls = 'skel-card') => Array.from({ length: n }, () => `<div class="skel ${cls}"></div>`).join('');
  const emptyState = (icon, title, sub = '') => `<div class="state"><div class="state-ico">${icon}</div><div class="state-title">${esc(title)}</div>${sub ? `<div>${esc(sub)}</div>` : ''}</div>`;
  const errorState = (what, err) => `<div class="state error"><div class="state-ico">⚠️</div><div class="state-title">Couldn't load ${esc(what)}</div><div>${esc(err.message || err)}</div><button class="btn btn-ghost" data-action="refresh">Try again</button></div>`;

  function deadlineBadge(o) {
    if (!o.deadline) return '<span class="badge-dl none">Rolling</span>';
    const days = daysUntil(o.deadline);
    const icon = '<svg><use href="#i-clock"/></svg>';
    if (o.is_expired || days < 0) return `<span class="badge-dl grey" title="Closed ${esc(fmtDate(o.deadline))}">${icon}${esc(fmtDate(o.deadline))}</span>`;
    const cls = days < 7 ? 'red' : days < 30 ? 'amber' : 'ok';
    const text = days === 0 ? 'Today' : days === 1 ? '1 day left' : days < 30 ? `${days} days left` : fmtDate(o.deadline);
    return `<span class="badge-dl ${cls}" title="${esc(fmtDate(o.deadline))}">${icon}${esc(text)}</span>`;
  }

  function oppCard(o) {
    const fit = o.fit_score >= 75 ? 'hi' : o.fit_score >= 50 ? 'mid' : 'lo';
    const url = safeUrl(o.url);
    return `<article class="card${o.is_expired ? ' is-expired' : ''}${o.needs_check ? ' needs-check' : ''}" data-opp="${o.id}" tabindex="0">
      <div class="card-top">
        <div class="card-main">
          <div class="card-name">${esc(o.name)}</div>
          <div class="card-meta">
            <span class="chip ${o.category}">${o.category === 'bd' ? '📋 BD' : '🚀 Startup'}</span>
            <span class="chip type">${esc((TYPE_LABELS[o.type] || o.type).replace(/s$/, ''))}</span>
            ${o.needs_check ? '<span class="chip check">🔒 Needs check</span>' : ''}
            ${o.source && o.source !== 'manual' ? `<span class="chip src">via ${esc(o.source)}</span>` : ''}
          </div>
        </div>
        <div class="fit ${fit}" title="Fit score">${o.fit_score}</div>
        <button class="star${o.is_starred ? ' on' : ''}" data-star="${o.id}" aria-pressed="${o.is_starred}" aria-label="${o.is_starred ? 'Unstar' : 'Star'} ${esc(o.name)}">${o.is_starred ? '★' : '☆'}</button>
      </div>
      <div class="card-info">${deadlineBadge(o)}${o.funding_amount ? `<span class="money">💰 ${esc(o.funding_amount)}</span>` : ''}</div>
      <div class="card-foot">
        <span class="status st-${o.status}">${STATUS_LABELS[o.status] || esc(o.status)}</span>
        <span class="prio p${o.priority}">P${o.priority}</span>
        <span class="spacer"></span>
        ${url ? `<a class="icon-btn" href="${esc(url)}" target="_blank" rel="noopener noreferrer" aria-label="Open link" data-stop><svg><use href="#i-link"/></svg></a>` : ''}
        <button class="icon-btn" data-delete="${o.id}" aria-label="Delete ${esc(o.name)}"><svg><use href="#i-trash"/></svg></button>
      </div>
    </article>`;
  }

  // ── Dashboard ──
  function renderDashboard(err) {
    const s = state.stats;
    if (err) { $('#stats').innerHTML = errorState('dashboard', err); $('#readiness-overview').innerHTML = ''; $('#upcoming').innerHTML = ''; $('#top-opps').innerHTML = ''; return; }
    if (!s) { $('#stats').innerHTML = loading(5, 'skel-card'); $('#readiness-overview').innerHTML = loading(4, 'skel-line'); $('#upcoming').innerHTML = loading(4, 'skel-line'); $('#top-opps').innerHTML = loading(3); return; }
    const cat = s.opportunities_by_category || {};
    $('#stats').innerHTML = `
      <button class="stat hero" data-view="opportunities" data-pipeline="all"><div class="stat-label">Active opportunities</div><div class="stat-value">${s.active_opportunities}</div><div class="stat-note">🚀 ${cat.startup ?? 0} · 📋 ${cat.bd ?? 0} · ${s.total_opportunities} tracked</div></button>
      <button class="stat" data-view="opportunities" data-sort="deadline:asc"><div class="stat-label">Due in 30 days</div><div class="stat-value">${s.upcoming_deadlines}</div><div class="stat-note">${s.upcoming_deadlines ? 'open deadlines' : 'nothing due soon'}</div></button>
      <button class="stat" data-view="readiness"><div class="stat-label">Readiness</div><div class="stat-value">${Math.round(s.overall_readiness_score)}<small>%</small></div><div class="stat-note">${s.completed_items}/${s.total_readiness_items} items complete</div></button>
      <button class="stat" data-view="opportunities" data-flag="needs_check"><div class="stat-label">🔒 Needs check</div><div class="stat-value">${s.needs_check_count}</div><div class="stat-note">${s.needs_check_count ? 'to verify by hand' : 'all verified'}</div></button>
      <button class="stat" data-view="opportunities" data-flag="expired"><div class="stat-label">Expired</div><div class="stat-value">${s.expired_opportunities}</div><div class="stat-note">in the archive</div></button>`;

    const circ = 2 * Math.PI * 64;
    const score = Math.max(0, Math.min(100, s.overall_readiness_score));
    const cats = READINESS_CATEGORIES.filter((c) => s.readiness_by_category[c]);
    $('#readiness-overview').innerHTML = s.total_readiness_items ? `
      <div class="readiness">
        <div class="ring"><svg viewBox="0 0 150 150" width="150" height="150"><circle class="ring-bg" cx="75" cy="75" r="64"/><circle class="ring-fg" cx="75" cy="75" r="64" stroke-dasharray="${circ}" stroke-dashoffset="${circ * (1 - score / 100)}"/></svg>
          <div class="ring-text"><div class="ring-val">${Math.round(score)}%</div><div class="ring-lbl">Overall</div></div></div>
        <div class="bars">${cats.map((c) => { const d = s.readiness_by_category[c]; return `<div class="bar-row"><span class="bar-name">${esc(c)}</span><div class="bar-track"><div class="bar-fill" style="width:${d.avg_completion}%"></div></div><span class="bar-pct">${Math.round(d.avg_completion)}%</span></div>`; }).join('')}</div>
      </div><div class="formula" title="${esc(s.readiness_formula)}">Weighted by priority: P1 counts 5×, P5 counts 1×.</div>`
      : emptyState('🧭', 'No readiness items yet', 'Add what investors will ask for.');

    $('#upcoming').innerHTML = s.upcoming_opportunities.length
      ? `<ul class="deadline-list">${s.upcoming_opportunities.map((o) => `<li data-opp="${o.id}"><span class="dl-name">${esc(o.name)}</span>${deadlineBadge(o)}</li>`).join('')}</ul>`
      : emptyState('📅', 'No deadlines in the next 30 days', s.expired_opportunities ? `${plural(s.expired_opportunities, 'opportunity')} already closed. See Expired.` : '');
  }

  function renderTopOpps(err) {
    if (err) { $('#top-opps').innerHTML = errorState('opportunities', err); return; }
    const live = state.allLive;
    if (!live) { $('#top-opps').innerHTML = loading(3); return; }
    const top = live.filter((o) => !['rejected', 'closed'].includes(o.status))
      .sort((a, b) => a.priority - b.priority || b.fit_score - a.fit_score).slice(0, 6);
    $('#top-opps').innerHTML = top.length ? top.map(oppCard).join('') : emptyState('✦', 'No active opportunities', 'The Hunter adds new ones every morning.');
  }

  // ── Opportunities ──
  function oppQuery() {
    const p = new URLSearchParams();
    if (state.pipeline !== 'all') p.set('category', state.pipeline);
    if (state.type !== 'all') p.set('type', state.type);
    if (state.flag === 'starred') p.set('is_starred', 'true');
    if (state.flag === 'needs_check') p.set('needs_check', 'true');
    p.set('expired', state.flag === 'expired' ? 'only' : state.flag === 'starred' ? 'include' : 'exclude');
    if (state.q) p.set('q', state.q);
    const [by, order] = state.sort.split(':');
    p.set('sort_by', by); p.set('sort_order', order);
    return p.toString();
  }

  async function loadOpportunities() {
    $('#opp-list').innerHTML = loading(6);
    const pipe = state.pipeline !== 'all' ? `pipeline=${state.pipeline}&` : '';
    const expired = state.flag === 'expired' ? 'only' : 'exclude';
    try {
      const [opps, counts] = await Promise.all([
        get(`/opportunities?${oppQuery()}`),
        get(`/opportunities/counts-by-type?${pipe}expired=${expired}`),
      ]);
      state.opps = opps; state.counts = counts;
      renderOpportunities();
    } catch (e) {
      if (e instanceof AuthError) return;
      $('#opp-list').innerHTML = errorState('opportunities', e);
    }
  }

  function renderOpportunities() {
    const c = state.counts;
    const titles = { all: 'All opportunities', startup: '🚀 Startup pipeline', bd: '📋 Business development' };
    const flagTitles = { starred: '★ Starred', needs_check: '🔒 Needs check', expired: 'Expired archive' };
    $('#opp-title').textContent = state.flag ? flagTitles[state.flag] : titles[state.pipeline];
    $('#opp-sub').textContent = state.flag === 'expired'
      ? 'Deadlines that passed while still researching or preparing'
      : state.pipeline === 'bd' ? 'RFPs, tenders, consulting and procurement for DEVONEERS'
        : state.pipeline === 'startup' ? 'Accelerators, grants, investors, competitions and credits' : 'Everything the team and the Hunter are tracking';
    $$('.pipe-btn').forEach((b) => b.classList.toggle('active', b.dataset.pipe === state.pipeline));

    const tabs = [`<button class="tab${state.type === 'all' && !state.flag ? ' active' : ''}" data-type="all">All <span class="n">${c.total}</span></button>`];
    TYPE_ORDER.filter((t) => c.counts[t]).forEach((t) => {
      tabs.push(`<button class="tab${state.type === t ? ' active' : ''}" data-type="${t}">${TYPE_LABELS[t]} <span class="n">${c.counts[t]}</span></button>`);
    });
    tabs.push('<span class="tab-sep"></span>');
    tabs.push(`<button class="tab flag${state.flag === 'starred' ? ' active' : ''}" data-flag="starred">★ Starred <span class="n">${c.starred_count}</span></button>`);
    tabs.push(`<button class="tab flag${state.flag === 'needs_check' ? ' active' : ''}" data-flag="needs_check">🔒 Needs check <span class="n">${c.needs_check_count}</span></button>`);
    tabs.push(`<button class="tab flag${state.flag === 'expired' ? ' active' : ''}" data-flag="expired">Expired <span class="n">${c.expired_count}</span></button>`);
    $('#type-tabs').innerHTML = tabs.join('');

    const list = state.opps;
    if (!list.length) {
      const msg = state.q ? ['🔍', `Nothing matches “${state.q}”`, 'Try another word or clear the search.']
        : state.flag === 'starred' ? ['☆', 'No starred opportunities yet', 'Tap the star on a card to keep it here.']
          : state.flag === 'expired' ? ['🗄️', 'No expired opportunities', '']
            : state.flag === 'needs_check' ? ['✅', 'Nothing needs a manual check', '']
              : ['✦', 'No opportunities in this view', 'Add one, or wait for the next Hunter run.'];
      $('#opp-list').innerHTML = emptyState(...msg);
      return;
    }
    $('#opp-list').innerHTML = list.map(oppCard).join('');
  }

  // ── Readiness ──
  function renderReadiness(err) {
    const body = $('#readiness-body');
    if (err) { body.innerHTML = `<tr><td colspan="7">${errorState('checklist', err)}</td></tr>`; return; }
    if (!state.itemsLoaded) { body.innerHTML = `<tr><td colspan="7">${loading(5, 'skel-line')}</td></tr>`; return; }
    const items = state.items;
    const counts = { all: items.length };
    items.forEach((i) => { counts[i.status] = (counts[i.status] || 0) + 1; });
    counts.overdue = items.filter((i) => i.is_overdue).length;
    $('#readiness-tabs').innerHTML = [['all', 'All'], ...Object.entries(READINESS_STATUS), ['overdue', '⚠ Overdue']]
      .map(([k, label]) => `<button class="tab${state.readinessFilter === k ? ' active' : ''}" data-rfilter="${k}">${label} <span class="n">${counts[k] || 0}</span></button>`).join('');
    const shown = items.filter((i) => state.readinessFilter === 'all' || (state.readinessFilter === 'overdue' ? i.is_overdue : i.status === state.readinessFilter))
      .sort((a, b) => a.priority - b.priority || (a.due_date || '9').localeCompare(b.due_date || '9'));
    if (!shown.length) { body.innerHTML = `<tr><td colspan="7">${emptyState('🧭', items.length ? 'Nothing in this filter' : 'No readiness items yet')}</td></tr>`; return; }
    body.innerHTML = shown.map((i) => {
      const d = daysUntil(i.due_date);
      const dueCls = i.is_overdue ? 'overdue' : d !== null && d <= 7 && i.status !== 'complete' ? 'soon' : '';
      const dueText = !i.due_date ? '–' : i.is_overdue ? `${fmtDate(i.due_date)} · ${-d}d late` : fmtDate(i.due_date);
      return `<tr data-item="${i.id}" tabindex="0">
        <td class="cell-name"><div class="item-name">${esc(i.name)}</div>${i.description ? `<div class="item-desc">${esc(i.description)}</div>` : ''}</td>
        <td data-label="Category"><span class="cat">${esc(i.category)}</span></td>
        <td data-label="Status"><span class="rs rs-${i.status}">${READINESS_STATUS[i.status] || esc(i.status)}</span></td>
        <td data-label="Owner">${esc(i.owner || '–')}</td>
        <td data-label="Due"><span class="due ${dueCls}">${esc(dueText)}</span></td>
        <td data-label="Progress"><div class="mini"><div class="mini-track"><div class="mini-fill" style="width:${i.status === 'complete' ? 100 : i.completion_percentage}%"></div></div><span>${i.status === 'complete' ? 100 : i.completion_percentage}%</span></div></td>
        <td data-label="Priority"><span class="prio p${i.priority}">P${i.priority}</span></td></tr>`;
    }).join('');
  }

  // ── Activity ──
  function renderActivity(err) {
    const feed = $('#feed');
    if (err) { feed.innerHTML = `<li>${errorState('activity', err)}</li>`; return; }
    if (!state.activityLoaded) { feed.innerHTML = `<li style="display:block">${loading(6, 'skel-line')}</li>`; return; }
    if (!state.activity.length) { feed.innerHTML = `<li style="display:block">${emptyState('🕊️', 'No activity yet')}</li>`; return; }
    const icons = { created: '+', updated: '✎', deleted: '×', saved: '★', unsaved: '☆', deduplicated: '⇄' };
    feed.innerHTML = state.activity.map((a) => {
      const k = icons[a.action] ? a.action : 'other';
      return `<li><div class="feed-ico fi-${k}">${icons[a.action] || '•'}</div><div><div class="feed-text"><strong>${esc(a.action)}</strong> ${esc(a.entity_type.replace(/_/g, ' '))}${a.details ? ` · ${esc(a.details)}` : ''}</div><div class="feed-time">${esc(ago(a.timestamp))}</div></div></li>`;
    }).join('');
  }

  // ── Sidebar / badges ──
  function renderCounts() {
    const s = state.stats;
    const by = state.counts?.by_category;
    const set = (key, val) => $$(`[data-count="${key}"]`).forEach((el) => { el.textContent = val ?? '–'; });
    if (s) {
      set('all', s.active_opportunities);
      set('startup', s.opportunities_by_category?.startup);
      set('bd', s.opportunities_by_category?.bd);
      set('starred', s.starred_count);
      const urgent = s.urgent_items.length;
      set('urgent', urgent);
      $$('[data-count="urgent"]').forEach((el) => el.classList.toggle('alert', urgent > 0));
      $$('[data-count="urgent-dot"]').forEach((el) => { el.hidden = !urgent; el.textContent = urgent; });
    } else if (by) {
      set('all', by.all); set('startup', by.startup); set('bd', by.bd);
    }
  }

  // ── Data loading ──
  async function loadAll({ manual = false, skipOpps = false } = {}) {
    const btns = $$('[data-action="refresh"]');
    btns.forEach((b) => { b.disabled = true; b.classList.add('spinning'); });
    state.stats = null; state.allLive = null;
    renderDashboard(); renderTopOpps();
    const jobs = {
      stats: get('/dashboard-stats').then((s) => { state.stats = s; renderDashboard(); renderCounts(); }, (e) => renderDashboard(e)),
      live: get('/opportunities?expired=exclude&sort_by=priority').then((o) => { state.allLive = o; renderTopOpps(); }, (e) => renderTopOpps(e)),
      items: get('/readiness-items').then((i) => { state.items = i; state.itemsLoaded = true; renderReadiness(); }, (e) => renderReadiness(e)),
      activity: get('/activity-log?limit=100').then((a) => { state.activity = a; state.activityLoaded = true; renderActivity(); }, (e) => renderActivity(e)),
      opps: state.view === 'opportunities' && !skipOpps ? loadOpportunities() : Promise.resolve(),
    };
    const results = await Promise.allSettled(Object.values(jobs));
    btns.forEach((b) => { b.disabled = false; b.classList.remove('spinning'); });
    const ok = results.every((r) => r.status === 'fulfilled') && state.stats;
    $('#sync-dot').classList.toggle('err', !ok);
    $('#last-sync').textContent = new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    if (manual) toast(ok ? 'Refreshed' : 'Some panels failed to load', ok ? 'success' : 'error');
  }

  // ── Navigation ──
  function show(view, opts = {}) {
    state.view = view;
    $$('[data-section]').forEach((s) => { s.hidden = s.dataset.section !== view; });
    if (view === 'opportunities') {
      if ('pipeline' in opts) { state.pipeline = opts.pipeline; state.flag = null; state.type = 'all'; }
      if ('flag' in opts) { state.flag = opts.flag; state.type = 'all'; if (opts.flag === 'starred') state.pipeline = 'all'; }
      if (opts.sort) { state.sort = opts.sort; $('#sort').value = opts.sort; }
      loadOpportunities();
    }
    $$('.nav-link, .tabbar button').forEach((b) => {
      let active = b.dataset.view === view;
      if (active && view === 'opportunities' && b.closest('.sidebar')) {
        active = state.flag === 'starred' ? b.dataset.flag === 'starred' : !b.dataset.flag && b.dataset.pipeline === state.pipeline && !state.flag;
      }
      b.classList.toggle('active', active);
    });
    const hash = view === 'opportunities' ? `#opportunities/${state.flag || state.pipeline}` : `#${view}`;
    if (location.hash !== hash) history.replaceState(null, '', hash);
    window.scrollTo(0, 0);
  }

  // ── Modals ──
  function openModal(id) { $(id).classList.add('open'); setTimeout(() => $(`${id} input:not([type=hidden])`).focus(), 30); }
  function closeModals() { $$('.modal-wrap.open').forEach((m) => m.classList.remove('open')); }

  function editOpp(opp) {
    const f = $('#opp-form');
    f.reset();
    f.id.value = opp?.id || '';
    $('#opp-modal-title').textContent = opp ? 'Edit opportunity' : 'Add opportunity';
    $('[data-action="delete-opp"]').hidden = !opp;
    const o = opp || { type: 'accelerator', category: state.pipeline === 'bd' ? 'bd' : 'startup', status: 'researching', fit_score: 50, priority: 3 };
    f.name.value = o.name || ''; f.type.value = o.type; f.category.value = o.category; f.status.value = o.status;
    f.deadline.value = dateOnly(o.deadline); f.fit_score.value = o.fit_score; f.priority.value = o.priority;
    f.funding_amount.value = o.funding_amount || ''; f.url.value = o.url || ''; f.needs_check.checked = !!o.needs_check;
    f.requirements.value = o.requirements || ''; f.notes.value = o.notes || '';
    $('#f-source').textContent = opp ? `Source: ${opp.source || 'manual'} · added ${fmtDate(opp.created_at)}` : '';
    openModal('#opp-modal');
  }

  function editItem(item) {
    const f = $('#item-form');
    f.reset();
    f.id.value = item?.id || '';
    $('#item-modal-title').textContent = item ? 'Edit readiness item' : 'Add readiness item';
    $('[data-action="delete-item"]').hidden = !item;
    const i = item || { category: 'pitch', status: 'not_started', priority: 3, completion_percentage: 0 };
    f.name.value = i.name || ''; f.category.value = i.category; f.status.value = i.status; f.owner.value = i.owner || '';
    f.due_date.value = dateOnly(i.due_date); f.priority.value = i.priority; f.completion_percentage.value = i.completion_percentage;
    f.description.value = i.description || ''; f.dependencies.value = i.dependencies || '';
    openModal('#item-modal');
  }

  const findOpp = (id) => [...state.opps, ...(state.allLive || []), ...(state.stats?.upcoming_opportunities || [])].find((o) => o.id === id);

  $('#opp-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    const f = e.target;
    if (!f.name.value.trim()) { f.name.focus(); return; }
    const data = {
      name: f.name.value.trim(), type: f.type.value, category: f.category.value, status: f.status.value,
      deadline: f.deadline.value || null,            // YYYY-MM-DD: no timezone conversion, no drift
      fit_score: Number(f.fit_score.value || 0), priority: Number(f.priority.value),
      funding_amount: f.funding_amount.value.trim() || null, url: f.url.value.trim() || null,
      needs_check: f.needs_check.checked, requirements: f.requirements.value.trim() || null, notes: f.notes.value.trim() || null,
    };
    const id = f.id.value;
    const submit = f.querySelector('[type=submit]'); submit.disabled = true;
    try {
      const { headers } = await api(id ? `/opportunities/${id}` : '/opportunities', { method: id ? 'PUT' : 'POST', body: JSON.stringify(data) });
      const dup = headers.get('X-Duplicate-Of');
      toast(dup ? `Already tracked (#${dup}). Filled in any missing details.` : id ? 'Saved' : 'Opportunity added', 'success');
      closeModals(); loadAll();
    } catch (err) { if (!(err instanceof AuthError)) toast(`Couldn't save: ${err.message}`, 'error'); }
    finally { submit.disabled = false; }
  });

  $('#item-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    const f = e.target;
    if (!f.name.value.trim()) { f.name.focus(); return; }
    const data = {
      name: f.name.value.trim(), category: f.category.value, status: f.status.value, owner: f.owner.value.trim() || null,
      due_date: f.due_date.value || null, priority: Number(f.priority.value),
      completion_percentage: Number(f.completion_percentage.value || 0),
      description: f.description.value.trim() || null, dependencies: f.dependencies.value.trim() || null,
    };
    const id = f.id.value;
    if (id) {   // only send what changed, so status/% reconciliation follows the user's edit
      const orig = state.items.find((i) => i.id === Number(id));
      if (orig && orig.status === data.status) delete data.status;
      if (orig && orig.completion_percentage === data.completion_percentage) delete data.completion_percentage;
    }
    try {
      await api(id ? `/readiness-items/${id}` : '/readiness-items', { method: id ? 'PUT' : 'POST', body: JSON.stringify(data) });
      toast(id ? 'Saved' : 'Item added', 'success'); closeModals(); loadAll();
    } catch (err) { if (!(err instanceof AuthError)) toast(`Couldn't save: ${err.message}`, 'error'); }
  });

  async function deleteOpp(id) {
    const o = findOpp(id);
    if (!confirm(`Delete “${o ? o.name : `#${id}`}”?\n\nThis can't be undone.`)) return;
    try { await api(`/opportunities/${id}`, { method: 'DELETE' }); toast('Deleted', 'success'); closeModals(); loadAll(); }
    catch (err) { if (!(err instanceof AuthError)) toast(`Couldn't delete: ${err.message}`, 'error'); }
  }

  async function toggleStar(id, btn) {
    btn.disabled = true;
    try {
      const { body } = await api(`/opportunities/${id}/toggle-save`, { method: 'PUT' });
      [state.opps, state.allLive || []].forEach((list) => list.forEach((o) => { if (o.id === id) o.is_starred = o.is_saved = body.is_starred; }));
      $$(`[data-star="${id}"]`).forEach((b) => { b.classList.toggle('on', body.is_starred); b.textContent = body.is_starred ? '★' : '☆'; b.setAttribute('aria-pressed', body.is_starred); });
      if (state.stats) { state.stats.starred_count += body.is_starred ? 1 : -1; renderCounts(); }
      if (state.view === 'opportunities') loadOpportunities();
    } catch (err) { if (!(err instanceof AuthError)) toast(`Couldn't update star: ${err.message}`, 'error'); }
    finally { btn.disabled = false; }
  }

  // ── Event delegation ──
  document.addEventListener('click', (e) => {
    const t = e.target.closest('button, a, [data-opp], [data-item]');
    if (!t) return;
    if (t.matches('[data-stop]')) return;                                   // external link
    if (t.dataset.star) { e.stopPropagation(); toggleStar(Number(t.dataset.star), t); return; }
    if (t.dataset.delete) { e.stopPropagation(); deleteOpp(Number(t.dataset.delete)); return; }
    if (t.dataset.view) {
      const opts = {};
      if (t.dataset.pipeline) opts.pipeline = t.dataset.pipeline;
      if (t.dataset.flag) opts.flag = t.dataset.flag;
      if (t.dataset.sort) opts.sort = t.dataset.sort;
      if (t.dataset.view === 'opportunities' && !Object.keys(opts).length) opts.pipeline = state.pipeline;
      show(t.dataset.view, opts); return;
    }
    if (t.dataset.pipe) { state.pipeline = t.dataset.pipe; state.type = 'all'; if (state.flag !== 'expired') state.flag = null; show('opportunities'); return; }
    if (t.dataset.type) { state.type = t.dataset.type; state.flag = null; show('opportunities'); return; }
    if (t.dataset.flag && t.classList.contains('tab')) { state.flag = state.flag === t.dataset.flag ? null : t.dataset.flag; state.type = 'all'; show('opportunities'); return; }
    if (t.dataset.rfilter) { state.readinessFilter = t.dataset.rfilter; renderReadiness(); return; }
    if (t.dataset.close !== undefined) { closeModals(); return; }
    if (t.dataset.clear) { $(`#opp-form [name="${t.dataset.clear}"]`).value = ''; return; }
    switch (t.dataset.action) {
      case 'refresh': loadAll({ manual: true }); return;
      case 'add': state.view === 'readiness' ? editItem(null) : editOpp(null); return;
      case 'add-opp': editOpp(null); return;
      case 'add-item': editItem(null); return;
      case 'delete-opp': deleteOpp(Number($('#opp-form').id.value)); return;
      case 'delete-item': {
        const id = Number($('#item-form').id.value);
        if (!confirm('Delete this readiness item?')) return;
        api(`/readiness-items/${id}`, { method: 'DELETE' }).then(() => { toast('Deleted', 'success'); closeModals(); loadAll(); },
          (err) => { if (!(err instanceof AuthError)) toast(`Couldn't delete: ${err.message}`, 'error'); });
        return;
      }
      default: break;
    }
    if (t.dataset.opp) { const o = findOpp(Number(t.dataset.opp)); if (o) editOpp(o); return; }
    if (t.dataset.item) { editItem(state.items.find((i) => i.id === Number(t.dataset.item))); }
  });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') closeModals();
    if (e.key === 'Enter' && (e.target.matches('[data-opp]') || e.target.matches('[data-item]'))) e.target.click();
  });
  $$('.modal-wrap').forEach((m) => m.addEventListener('click', (e) => { if (e.target === m) closeModals(); }));
  $('#search').addEventListener('input', debounce((e) => { state.q = e.target.value.trim(); loadOpportunities(); }, 250));
  $('#sort').addEventListener('change', (e) => { state.sort = e.target.value; loadOpportunities(); });

  // ── boot ──
  $('#f-status').innerHTML = Object.entries(STATUS_LABELS).map(([v, l]) => `<option value="${v}">${l}</option>`).join('');
  $('#r-category').innerHTML = READINESS_CATEGORIES.map((c) => `<option value="${c}">${c[0].toUpperCase() + c.slice(1)}</option>`).join('');

  async function boot() {
    try {
      const status = await (await fetch(`${API}/auth/status`, { headers: store.get(TOKEN_KEY) ? { Authorization: `Bearer ${store.get(TOKEN_KEY)}` } : {} })).json();
      state.authMode = status.mode;
      $('#sign-out').hidden = !(status.authenticated && store.get(TOKEN_KEY));
      if (status.mode === 'full' && !status.authenticated) {
        store.set(TOKEN_KEY, null);
        await askPasscode('Enter the team passcode to continue.');
      }
    } catch { /* older backend or offline — continue */ }
    try { const h = await (await fetch(`${API}/health`)).json(); $('#app-version').textContent = `v${h.version}`; } catch { /* offline */ }

    const [view, sub] = location.hash.replace('#', '').split('/');
    if (view === 'opportunities') show('opportunities', ['starred', 'needs_check', 'expired'].includes(sub) ? { flag: sub } : { pipeline: ['startup', 'bd'].includes(sub) ? sub : 'all' });
    else show(['readiness', 'activity'].includes(view) ? view : 'dashboard');
    loadAll({ skipOpps: true });
    setInterval(() => { if (!document.hidden) loadAll(); }, 5 * 60 * 1000);
  }
  boot();

  if ('serviceWorker' in navigator) {
    window.addEventListener('load', () => navigator.serviceWorker.register('/sw.js').catch(() => {}));
  }
})();
