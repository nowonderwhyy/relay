'use strict';

const $ = (selector) => document.querySelector(selector);
const state = {
  data: null,
  meta: { instance_name: 'Relay', host_label: 'Local', vpn_mode: 'none' },
  expanded: new Set(),
  timer: null,
  offline: false,
  needsPassword: false,
  seen: new Map(),
  locationNames: new Map(),
};

// Front-panel LED states for jobs.
const LEDS = { downloading: 'busy', queued: 'busy', complete: 'on', error: 'fault' };

const LABELS = {
  queued: 'Waiting', downloading: 'Downloading', paused: 'Paused', complete: 'Ready',
  error: 'Problem', cancelled: 'Cancelled',
};
const ACTIVE = new Set(['queued', 'downloading', 'paused']);
const HISTORY = new Set(['complete', 'cancelled', 'error']);

const ICON = (body) => `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round">${body}</svg>`;
const ICONS = {
  link: ICON('<path d="M10 14a5 5 0 0 0 7 0l3-3a5 5 0 0 0-7-7l-1 1"/><path d="M14 10a5 5 0 0 0-7 0l-3 3a5 5 0 0 0 7 7l1-1"/>'),
  magnet: ICON('<path d="M5 3h5v8a2 2 0 0 0 4 0V3h5v8a7 7 0 0 1-14 0z"/><path d="M5 7h5M14 7h5"/>'),
  torrent: ICON('<path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/><path d="M14 3v5h5"/><path d="M12 11v6m0 0-3-3m3 3 3-3"/>'),
  save: ICON('<path d="M12 4v11m0 0-4-4m4 4 4-4"/><path d="M5 20h14"/>'),
  files: ICON('<path d="M8 6h13M8 12h13M8 18h13"/><path d="M3.5 6h.01M3.5 12h.01M3.5 18h.01"/>'),
  pause: ICON('<path d="M9 5v14M15 5v14"/>'),
  play: ICON('<path d="M7 4.5v15l12-7.5z"/>'),
  retry: ICON('<path d="M4 12a8 8 0 0 1 14-5.3L20 9"/><path d="M20 4v5h-5"/><path d="M20 12a8 8 0 0 1-14 5.3L4 15"/><path d="M4 20v-5h5"/>'),
};

// ---------------------------------------------------------------- helpers

function remember(key, value) {
  try {
    if (value === undefined) return localStorage.getItem(key);
    localStorage.setItem(key, value);
  } catch { /* private window or blocked storage */ }
  return null;
}

function esc(text) {
  return String(text ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

function bytes(value) {
  let n = Number(value) || 0;
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let i = 0;
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i += 1; }
  return i === 0 ? `${n} B` : `${n.toFixed(n >= 100 ? 0 : 1)} ${units[i]}`;
}

function duration(seconds) {
  if (seconds == null) return '';
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes} min`;
  const hours = Math.floor(minutes / 60);
  if (hours < 48) return `${hours}h ${minutes % 60}m`;
  return `${Math.round(hours / 24)} days`;
}

function when(timestamp) {
  const date = new Date(timestamp * 1000);
  const today = new Date();
  const time = date.toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' });
  if (date.toDateString() === today.toDateString()) return `today ${time}`;
  return `${date.toLocaleDateString(undefined, { weekday: 'short', month: 'short', day: 'numeric' })}, ${time}`;
}

function day(timestamp) {
  return new Date(timestamp * 1000).toLocaleDateString(undefined, { weekday: 'long', month: 'short', day: 'numeric' });
}

function percent(job) {
  if (job.status === 'complete') return 100;
  return job.total ? Math.min(100, Math.floor((100 * job.completed) / job.total)) : 0;
}

function fileURL(job, path) {
  return `/files/${job.id}/${path.split('/').map(encodeURIComponent).join('/')}`;
}

function baseTitle() {
  const { instance_name: name, host_label: host } = state.meta;
  return host && host.toLowerCase() !== name.toLowerCase() ? `${name} · ${host}` : name;
}

function applyBranding(info) {
  if (!info) return;
  if (info.instance_name) state.meta.instance_name = info.instance_name;
  if (info.host_label) state.meta.host_label = info.host_label;
  if (info.vpn_mode) state.meta.vpn_mode = info.vpn_mode;

  const { instance_name: name, host_label: host, vpn_mode: vpnMode } = state.meta;
  $('#brand-name').textContent = name;
  $('#brand-host').textContent = host;
  $('#brand-host').hidden = !host || host.toLowerCase() === name.toLowerCase();

  const target = host || name;
  $('#headline').textContent = `Send a download to ${target}.`;
  if (vpnMode !== 'none') {
    $('#lede').textContent = 'It downloads privately through the VPN and keeps going after you close this page. When it’s done, it comes straight to this device.';
    $('#how-step-2').innerHTML = `<span>02</span>${esc(target)} downloads it through the VPN.`;
  } else {
    $('#lede').textContent = 'It downloads in the background and keeps going after you close this page. When it’s done, it comes straight to this device.';
    $('#how-step-2').innerHTML = `<span>02</span>${esc(target)} downloads it in the background.`;
  }
}

let toastTimer;
function toast(message, bad = false) {
  const element = $('#toast');
  $('#toast-text').textContent = message;
  element.classList.toggle('bad', bad);
  element.hidden = true;
  void element.offsetWidth;
  element.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { element.hidden = true; }, bad ? 6500 : 4000);
}

async function api(path, options = {}) {
  const init = { method: 'GET', headers: { 'X-Relay': '1' }, credentials: 'same-origin' };
  if (options.json !== undefined) {
    init.method = 'POST';
    init.headers['Content-Type'] = 'application/json';
    init.body = JSON.stringify(options.json);
  } else if (options.form) {
    init.method = 'POST';
    init.body = options.form;
  } else if (options.post) {
    init.method = 'POST';
  }
  const response = await fetch(path, init);
  let data = {};
  try { data = await response.json(); } catch { /* not JSON */ }
  if (response.status === 401 && !path.startsWith('/api/login')) {
    await checkPassword();
    showSignIn();
    throw new Error(data.error || 'Sign in first.');
  }
  if (!response.ok) throw new Error(data.error || `Something went wrong (${response.status}).`);
  return data;
}

// ------------------------------------------------------------- job cards

function button(action, label, kind = '', icon = '') {
  return `<button type="button" class="btn ${kind}" data-action="${action}">${icon ? ICONS[icon] : ''}<span>${label}</span></button>`;
}

function signature(job) {
  return [job.status, job.file_count, job.error, state.expanded.has(job.id), job.kind, Boolean(job.total)].join('/');
}

function cardHTML(job) {
  const status = job.status;
  const single = job.file_count === 1 ? job.files[0] : null;
  let actions = '';
  let extra = '';
  let note = '';
  if (status === 'downloading' || status === 'queued') actions = button('pause', 'Pause', '', 'pause') + button('cancel', 'Cancel', 'plain danger');
  if (status === 'paused') actions = button('resume', 'Resume', '', 'play') + button('cancel', 'Cancel', 'plain danger');
  if (status === 'error' || status === 'cancelled') {
    actions = button('retry', 'Try again', '', 'retry') + button('remove', 'Remove', 'plain danger');
    note = `<div class="note ${status === 'error' ? 'error' : ''}">${esc(job.error)}</div>`;
  }
  if (status === 'complete') {
    const save = single
      ? `<a class="btn primary" href="${fileURL(job, single.path)}" download>${ICONS.save}<span>Save to this device</span></a>`
      : `<a class="btn primary" href="/zip/${job.id}" download>${ICONS.save}<span>Save all (.zip)</span></a>`
        + button('files', state.expanded.has(job.id) ? 'Hide files' : `Choose files (${job.file_count})`, '', 'files');
    actions = save + button('remove', 'Delete', 'plain danger');
    note = job.expires ? `<div class="note">Kept until ${day(job.expires)}, then erased.</div>` : '';
    if (!single && state.expanded.has(job.id)) {
      const more = job.file_count > job.files.length ? `<li><span class="path">…and ${job.file_count - job.files.length} more in the .zip</span></li>` : '';
      extra = `<ul class="files">${job.files.map((file) => `<li><span class="path">${esc(file.path)}<span class="size">${bytes(file.size)}</span></span>`
        + `<a class="btn" href="${fileURL(job, file.path)}" download>Save</a></li>`).join('')}${more}</ul>`;
    }
  }
  const progress = ACTIVE.has(status) || ((status === 'error' || status === 'cancelled') && job.total)
    ? '<div class="progress"><div class="meter"><span></span></div><span class="pct"></span></div>' : '';
  return `<div class="row"><span class="glyph">${ICONS[job.kind] || ICONS.link}</span>`
    + '<div class="title"><div class="name"></div><div class="sub"></div></div>'
    + `<span class="state"><i class="led ${LEDS[status] || ''}"></i>${LABELS[status] || status}</span></div>`
    + `<div class="body">${progress}<div class="meta"></div>${note}${extra}<div class="actions">${actions}</div></div>`;
}

function metaText(job) {
  const pct = percent(job);
  const sizes = job.total ? `${bytes(job.completed)} of ${bytes(job.total)}` : '';
  switch (job.status) {
    case 'downloading':
      if (!job.total) return job.phase || 'Starting…';
      return [sizes, job.speed ? `↓ ${bytes(job.speed)}/s` : '', job.eta != null ? `${duration(job.eta)} left` : '', job.phase]
        .filter(Boolean).join('  ·  ');
    case 'queued':
      if (state.data.vpn_mode !== 'none' && !state.data.vpn) return 'Waiting for the VPN';
      return state.data.engine ? 'Waiting for a free download slot' : 'Waiting for the download engine';
    case 'paused':
      return [sizes, 'paused'].filter(Boolean).join('  ·  ');
    case 'complete':
      return `${bytes(job.total)}  ·  ${job.file_count} file${job.file_count === 1 ? '' : 's'}  ·  finished ${when(job.finished)}`;
    default:
      return job.total ? `${pct}%  ·  ${sizes}` : '';
  }
}

function fill(card, job) {
  card.querySelector('.name').textContent = job.name || 'Download';
  card.querySelector('.name').title = job.name || '';
  card.querySelector('.sub').textContent = `Added ${when(job.created)}`;
  const meta = metaText(job);
  card.querySelector('.meta').textContent = meta;
  card.querySelector('.meta').hidden = !meta;
  const bar = card.querySelector('.meter span');
  if (bar) {
    bar.style.width = `${percent(job)}%`;
    card.querySelector('.pct').textContent = job.total ? `${percent(job)}%` : '…';
  }
}

function renderReadout(data) {
  const downloading = data.jobs.filter((job) => job.status === 'downloading');
  const speed = downloading.reduce((sum, job) => sum + (job.speed || 0), 0);
  const parts = [];
  if (downloading.length) parts.push(`<b>${downloading.length}</b> active`, `<b>${bytes(speed)}/s</b>`);
  parts.push(`<b>${bytes(data.disk.free)}</b> free`);
  $('#readout').innerHTML = parts.join(' · ');
  document.title = downloading.length
    ? `${Math.floor(downloading.reduce((sum, job) => sum + percent(job), 0) / downloading.length)}% · ${state.meta.instance_name}`
    : baseTitle();
}

function renderSwitch(data) {
  const group = $('#speed-limit');
  if (!group.children.length) {
    const limits = Object.entries(data.speed_limits)
      .sort(([a], [b]) => (a === '0') - (b === '0') || parseFloat(a) - parseFloat(b))
      .reverse();
    for (const [value, label] of limits) {
      group.insertAdjacentHTML('beforeend', `<button type="button" role="radio" data-limit="${value}">${value === '0' ? 'Off' : esc(label)}</button>`);
    }
  }
  for (const option of group.children) option.setAttribute('aria-checked', String(option.dataset.limit === data.speed_limit));
}

// ------------------------------------------------------- VPN exit picker

async function loadLocation() {
  const row = $('#location-row');
  const select = $('#location');
  if (!row || !select) return;
  try {
    const data = await api('/api/location');
    if (!data.available || !data.locations.length) {
      row.hidden = true;
      return;
    }
    state.locationNames.clear();
    select.innerHTML = '';
    for (const loc of data.locations) {
      state.locationNames.set(loc.slug, loc.label || loc.slug);
      const option = document.createElement('option');
      option.value = loc.slug;
      option.textContent = loc.label || loc.slug;
      option.selected = loc.slug === data.active;
      select.append(option);
    }
    select.disabled = false;
    row.hidden = false;
  } catch {
    row.hidden = true;
  }
}

function announce(mine) {
  for (const job of mine) {
    const before = state.seen.get(job.id);
    if (before && ACTIVE.has(before) && job.status === 'complete') toast(`“${job.name}” is ready to save.`);
    if (before && ACTIVE.has(before) && job.status === 'error') toast(`“${job.name}” hit a problem: ${job.error}`, true);
    state.seen.set(job.id, job.status);
  }
}

// Auto-save finished downloads on desktops by default, off on touch-only devices.
const TOUCH_ONLY = window.matchMedia('(hover: none) and (pointer: coarse)').matches;

function autosaveOn() {
  const choice = remember('relay-autosave') || state.autosaveChoice;
  return choice ? choice === 'on' : !TOUCH_ONLY;
}

function startAutosave() {
  state.autosaveSince = state.data?.now ?? Date.now() / 1000;
  remember('relay-autosave-since', String(state.autosaveSince));
}

function autosaveFinished(jobs) {
  if (!autosaveOn()) return;
  if (!(Number(remember('relay-autosave-since')) || state.autosaveSince)) startAutosave();
  const since = Number(remember('relay-autosave-since')) || state.autosaveSince;
  let saved;
  try { saved = JSON.parse(remember('relay-autosaved') || '[]'); } catch { saved = []; }
  for (const job of jobs) {
    if (job.status !== 'complete' || !job.file_count || job.finished < since || saved.includes(job.id)) continue;
    saved.push(job.id);
    remember('relay-autosaved', JSON.stringify(saved.slice(-100)));
    const link = document.createElement('a');
    link.href = job.file_count === 1 ? fileURL(job, job.files[0].path) : `/zip/${job.id}`;
    link.download = '';
    document.body.append(link);
    link.click();
    link.remove();
    toast(`Saving “${job.name}” to this device.`);
  }
}

function renderStatusBadge(data) {
  const led = $('#vpn .led');
  const text = $('#vpn-text');
  const badge = $('#vpn');
  if (data.vpn_mode === 'none') {
    led.className = `led ${data.engine ? 'direct' : 'fault'}`;
    text.textContent = data.engine ? 'Direct' : 'Engine offline';
    badge.title = data.engine
      ? 'Standalone direct mode — downloads use this host’s normal connection (no built-in VPN kill-switch).'
      : (data.message || 'The download engine is not running.');
    return;
  }
  led.className = `led ${data.vpn ? 'on' : 'fault'}`;
  text.textContent = data.vpn ? 'VPN on' : 'VPN off, downloads paused';
  const bits = [];
  if (data.vpn_interface) bits.push(`interface ${data.vpn_interface}`);
  if (data.vpn_guarded) bits.push('nftables kill-switch active');
  if (data.forward_port) bits.push(`forwarded port ${data.forward_port}`);
  badge.title = data.vpn
    ? `Every download routes through the VPN${bits.length ? ' (' + bits.join(', ') + ')' : ''}.`
    : 'VPN is disconnected; downloads stay paused until it reconnects.';
}

function render() {
  const data = state.data;
  if (!data) return;
  applyBranding(data);
  renderStatusBadge(data);
  $('#me').textContent = data.me;
  $('#keep-days').textContent = data.keep_days;

  const note = $('#engine-note');
  const target = state.meta.host_label || state.meta.instance_name;
  let problem = '';
  if (state.offline) {
    problem = `Can't reach ${target} right now. Retrying…`;
  } else if (data.vpn_mode !== 'none' && !data.vpn) {
    problem = data.message || 'The VPN is disconnected, so downloads are paused until it reconnects.';
  } else if (!data.engine) {
    problem = data.message || 'The download engine is starting…';
  }
  note.textContent = problem;
  note.hidden = !problem;
  renderSwitch(data);

  const { jobs } = data;
  renderReadout(data);
  announce(jobs);
  autosaveFinished(jobs);
  $('#count-mine').textContent = jobs.length || '';
  $('#clear-history').hidden = !jobs.some((job) => HISTORY.has(job.status));

  const list = $('#jobs');
  const seen = new Set();
  jobs.forEach((job, index) => {
    let card = list.querySelector(`li[data-id="${job.id}"]`);
    if (!card) {
      card = document.createElement('li');
      card.className = 'job';
      card.dataset.id = job.id;
    }
    const sig = signature(job);
    if (card.dataset.sig !== sig) {
      card.innerHTML = cardHTML(job);
      card.dataset.sig = sig;
      card.dataset.status = job.status;
    }
    fill(card, job);
    if (list.children[index] !== card) list.insertBefore(card, list.children[index] || null);
    seen.add(job.id);
  });
  [...list.children].forEach((card) => { if (!seen.has(card.dataset.id)) card.remove(); });
  $('#empty').hidden = jobs.length > 0;
}

// ------------------------------------------------------------ data flow

async function refresh() {
  try {
    state.data = await api('/api/status');
    state.offline = false;
  } catch {
    if (!$('#sign-in').hidden) return;
    state.offline = true;
  }
  render();
}

function schedule() {
  clearTimeout(state.timer);
  state.timer = setTimeout(async () => {
    await refresh();
    if ($('#sign-in').hidden) schedule();
  }, document.hidden ? 8000 : 1500);
}

function showSignIn(message = '') {
  clearTimeout(state.timer);
  $('#app').hidden = true;
  $('#status').hidden = true;
  $('#sign-in').hidden = false;
  $('#password-field').hidden = !state.needsPassword;
  $('#login-password').required = state.needsPassword;
  $('#login-name').value = $('#login-name').value || remember('relay-name') || '';
  const note = $('#login-note');
  note.textContent = message;
  note.hidden = !message;
  ($('#login-name').value && state.needsPassword ? $('#login-password') : $('#login-name')).focus();
}

async function checkPassword() {
  try {
    const me = await api('/api/me');
    state.needsPassword = me.needs_password;
    applyBranding(me);
  } catch { /* keep cached values */ }
}

async function signIn(name, password = '') {
  const result = await api('/api/login', { json: { name, password } });
  remember('relay-name', result.name);
  $('#login-password').value = '';
}

async function showApp() {
  $('#sign-in').hidden = true;
  $('#app').hidden = false;
  $('#status').hidden = false;
  state.seen.clear();
  await refresh();
  await loadLocation();
  schedule();
}

async function act(id, action, control) {
  const job = state.data.jobs.find((item) => item.id === id);
  if (!job) return;
  if (action === 'files') {
    if (state.expanded.has(id)) state.expanded.delete(id); else state.expanded.add(id);
    render();
    return;
  }
  if (action === 'remove' && job.status === 'complete'
      && !confirm(`Delete "${job.name}"? Save it first if you still need it.`)) return;
  control.disabled = true;
  try {
    await api(`/api/jobs/${id}/${action}`, { post: true });
    await refresh();
  } catch (error) {
    toast(error.message, true);
  } finally {
    control.disabled = false;
  }
}

async function addLink(link) {
  const buttonElement = $('#add-button');
  buttonElement.disabled = true;
  try {
    await api('/api/jobs', { json: { link } });
    $('#link').value = '';
    toast('Added. It keeps going even if you close this page.');
    await refresh();
  } catch (error) {
    toast(error.message, true);
  } finally {
    buttonElement.disabled = false;
  }
}

async function addTorrent(file) {
  if (!/\.torrent$/i.test(file.name)) {
    toast("That isn't a .torrent file.", true);
    return;
  }
  const form = new FormData();
  form.append('torrent', file);
  try {
    await api('/api/jobs', { form });
    toast(`Added ${file.name}.`);
    await refresh();
  } catch (error) {
    toast(error.message, true);
  }
}

// --------------------------------------------------------------- events

$('#login-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  try {
    await signIn($('#login-name').value.trim(), $('#login-password').value);
    await showApp();
  } catch (error) {
    await checkPassword();
    showSignIn(error.message);
  }
});

$('#logout').addEventListener('click', async () => {
  try { await api('/api/logout', { post: true }); } catch { /* signing out anyway */ }
  state.data = null;
  remember('relay-name', '');
  $('#login-name').value = '';
  showSignIn();
});

$('#add-form').addEventListener('submit', (event) => {
  event.preventDefault();
  addLink($('#link').value);
});

$('#torrent').addEventListener('change', (event) => {
  const [file] = event.target.files;
  if (file) addTorrent(file);
  event.target.value = '';
});

$('#jobs').addEventListener('click', (event) => {
  const target = event.target.closest('button[data-action]');
  if (target) act(target.closest('li').dataset.id, target.dataset.action, target);
});

$('#autosave').checked = autosaveOn();
$('#autosave').addEventListener('change', (event) => {
  const on = event.target.checked;
  state.autosaveChoice = on ? 'on' : 'off';
  remember('relay-autosave', state.autosaveChoice);
  if (on) startAutosave();
  toast(on ? 'Downloads that finish from now on will save to this device.' : 'Auto-save is off on this device. Use “Save to this device” on each download.');
});

$('#clear-history').addEventListener('click', async (event) => {
  const finished = state.data.jobs.filter((job) => HISTORY.has(job.status)).length;
  if (!confirm(`Clear ${finished} download${finished === 1 ? '' : 's'}? They're erased right away, `
      + "including finished files you haven't saved yet. Anything still downloading stays.")) return;
  event.target.disabled = true;
  try {
    const result = await api('/api/clear', { post: true });
    toast(`Cleared ${result.cleared} item${result.cleared === 1 ? '' : 's'}.`);
    await refresh();
  } catch (error) {
    toast(error.message, true);
  } finally {
    event.target.disabled = false;
  }
});

$('#speed-limit').addEventListener('click', async (event) => {
  const option = event.target.closest('button[data-limit]');
  if (!option || option.getAttribute('aria-checked') === 'true') return;
  try {
    await api('/api/settings', { json: { speed_limit: option.dataset.limit } });
    toast(option.dataset.limit === '0' ? 'Speed limit off.' : `Downloads limited to ${state.data.speed_limits[option.dataset.limit]}.`);
    await refresh();
  } catch (error) {
    toast(error.message, true);
  }
});

$('#location').addEventListener('change', async (event) => {
  const select = event.target;
  const label = state.locationNames.get(select.value) || select.value;
  select.disabled = true;
  toast(`Switching the VPN exit to ${label}. Downloads resume in a few seconds.`);
  try {
    const result = await api('/api/location', { json: { slug: select.value } });
    toast(result.vpn_ready === false
      ? `VPN exit set to ${label}, but the tunnel is not ready. Check your WireGuard config.`
      : `VPN exit is now ${label}.`, result.vpn_ready === false);
    await refresh();
  } catch (error) {
    toast(error.message, true);
    await loadLocation();
  } finally {
    select.disabled = false;
  }
});

let dragDepth = 0;
window.addEventListener('dragenter', () => {
  if ($('#app').hidden) return;
  dragDepth += 1;
  $('#drop').hidden = false;
});
window.addEventListener('dragleave', () => {
  dragDepth = Math.max(0, dragDepth - 1);
  if (!dragDepth) $('#drop').hidden = true;
});
window.addEventListener('dragover', (event) => event.preventDefault());
window.addEventListener('drop', (event) => {
  event.preventDefault();
  dragDepth = 0;
  $('#drop').hidden = true;
  if ($('#app').hidden) return;
  const [file] = event.dataTransfer.files;
  if (file) return addTorrent(file);
  const text = event.dataTransfer.getData('text/uri-list') || event.dataTransfer.getData('text/plain');
  if (text) addLink(text.split('\n')[0]);
});

document.addEventListener('visibilitychange', () => {
  if (!document.hidden && $('#sign-in').hidden && !$('#app').hidden) {
    refresh();
    schedule();
  }
});

(async () => {
  try {
    const me = await api('/api/me');
    state.needsPassword = me.needs_password;
    applyBranding(me);
    document.title = baseTitle();
    if (!me.signed_in && !me.needs_password && remember('relay-name')) {
      try { await signIn(remember('relay-name')); me.signed_in = true; } catch { /* ask instead */ }
    }
    if (me.signed_in) await showApp();
    else showSignIn();
  } catch {
    showSignIn("Can't reach Relay right now.");
  }
})();
