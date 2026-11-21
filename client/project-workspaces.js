(function () {
  'use strict';

  const PROJECTS = Object.freeze({
    open: Object.freeze({ label: 'Open Projects', emoji: '🌐' }),
    floyd: Object.freeze({ label: 'Floyd Projects', emoji: '🏛️' }),
    ts: Object.freeze({ label: 'TS Projects', emoji: '🧭' })
  });
  const $ = id => document.getElementById(id);
  let config = null, project = 'open', unlocked = true, revision = null;
  let phase = 'idle', epoch = 0, selection = 0, controller = null;
  let timer = null, pending = null, writing = null, savedJson = null;
  let dialogProject = null, submitting = false, initialized = false, loggingOut = false;
  const loginRequired = new Set();

  function workspaceError(code) {
    const error = new Error('Projektaktion nicht abgeschlossen.');
    error.code = code;
    return error;
  }
  function isPrivate(id = project) { return id === 'floyd' || id === 'ts'; }
  function isCurrent(token, id) { return token === epoch && id === project; }
  function clearTimer() { if (timer !== null) clearTimeout(timer); timer = null; }
  function discardQueue() { clearTimer(); pending = null; savedJson = null; }
  function abortRead() { if (controller) controller.abort(); controller = null; }
  function clearPassword(resetInvalid = true) {
    const input = $('project-login-password');
    if (input) { input.value = ''; if (resetInvalid) input.removeAttribute('aria-invalid'); }
  }
  function setLoginError(message) {
    $('project-login-error').textContent = message || '';
    $('project-login-error').hidden = !message;
  }
  function hideLogin() {
    dialogProject = null;
    clearPassword(); setLoginError('');
    const dialog = $('project-login-dialog');
    if (dialog.open) dialog.close();
  }
  function updateIdentity() {
    const item = PROJECTS[project];
    $('project-menu-label').textContent = item.label;
    $('project-menu-emoji').textContent = item.emoji;
    $('project-menu-toggle').setAttribute('aria-label', item.label + ' auswählen');
    $('project-menu-toggle').disabled = loggingOut;
    for (const button of $('project-menu').querySelectorAll('[role="menuitem"]:not([hidden])')) {
      if (button.dataset.project === project) button.setAttribute('aria-current', 'true');
      else button.removeAttribute('aria-current');
    }
    $('project-cloud-controls').hidden = !isPrivate();
    $('project-logout').hidden = !isPrivate() || !unlocked;
    $('project-logout').disabled = loggingOut;
  }
  function setPhase(value) {
    phase = value;
    const labels = { idle: '', loading: 'Wird geöffnet …', locked: 'Geschützt', saved: 'Gespeichert', saving: 'Wird gespeichert …', dirty: 'Nicht gespeichert', unsaved: 'Nicht gespeichert', conflict: 'Nicht gespeichert' };
    $('project-sync-status').textContent = labels[value] || 'Nicht gespeichert';
    $('project-sync-status').dataset.status = value;
    const conflict = value === 'conflict', failed = value === 'unsaved';
    const banner = $('project-sync-banner');
    banner.hidden = !isPrivate() || !unlocked || !(conflict || failed);
    $('project-sync-message').textContent = conflict
      ? 'Anderswo geändert; „Neu laden“ verwirft deine ungespeicherten Änderungen.'
      : failed ? 'Änderungen konnten nicht gespeichert werden.' : '';
    $('project-sync-reload').hidden = !conflict;
    $('project-sync-retry').hidden = !failed;
    updateIdentity();
  }
  async function request(id, route, options = {}) {
    const headers = { Accept: 'application/json', ...options.headers };
    if (options.body !== undefined) headers['Content-Type'] = 'application/json';
    return fetch('/api/projects/' + id + '/' + route, {
      ...options, headers, credentials: 'same-origin', mode: 'same-origin',
      cache: 'no-store', redirect: 'error', referrerPolicy: 'same-origin'
    });
  }
  async function json(response) {
    try { return await response.json(); }
    catch (_) { throw workspaceError('response'); }
  }
  function validState(value, id) {
    return value && value.project === id && Number.isSafeInteger(value.revision) && value.revision >= 0
      && value.settings && typeof value.settings === 'object' && !Array.isArray(value.settings)
      && ['tech', 'value'].every(key => value.settings[key] && typeof value.settings[key] === 'object' && !Array.isArray(value.settings[key]));
  }
  function copySettings(value) {
    if (!value || typeof value !== 'object' || Array.isArray(value)) throw workspaceError('settings');
    return JSON.parse(JSON.stringify(value));
  }
  async function lockPrivate(message) {
    if (!isPrivate()) return;
    const id = project;
    ++epoch; ++selection; abortRead(); discardQueue(); revision = null; unlocked = false;
    setPhase('locked');
    await config.onLock(id);
    if (project === id && !unlocked) showLogin(id, message || 'Bitte erneut anmelden.');
  }
  async function activatePrivate(id, token, signal) {
    const response = await request(id, 'state', { signal });
    if (!isCurrent(token, id)) return false;
    if (response.status === 401) { await lockPrivate('Bitte erneut anmelden.'); return false; }
    if (!response.ok) throw workspaceError('load');
    const data = await json(response);
    if (!isCurrent(token, id)) return false;
    if (!validState(data, id)) throw workspaceError('response');
    const settings = copySettings(data.settings);
    revision = data.revision; savedJson = JSON.stringify(settings);
    unlocked = true;
    try { await config.onActivate(id, settings); }
    catch (error) { if (isCurrent(token, id)) { unlocked = false; await config.onLock(id); } throw error; }
    if (!isCurrent(token, id)) return false;
    unlocked = true; hideLogin(); setPhase('saved');
    return true;
  }
  function closeMenu(returnFocus = false) {
    $('project-menu').hidden = true;
    $('project-menu-toggle').setAttribute('aria-expanded', 'false');
    if (returnFocus) $('project-menu-toggle').focus();
  }
  function openMenu(last = false) {
    $('project-menu').hidden = false;
    $('project-menu-toggle').setAttribute('aria-expanded', 'true');
    const items = [...$('project-menu').querySelectorAll('[role="menuitem"]:not([hidden])')];
    (last ? items.at(-1) : items.find(item => item.dataset.project === project) || items[0]).focus();
  }
  function showLogin(id, message = '') {
    if (!isPrivate(id) || project !== id || unlocked) return;
    dialogProject = id; clearPassword(); setLoginError(message);
    $('project-login-title').textContent = PROJECTS[id].label;
    $('project-login-submit').disabled = false;
    $('project-login-submit').textContent = 'Öffnen';
    const dialog = $('project-login-dialog');
    if (!dialog.open) dialog.showModal();
    $('project-login-password').focus();
  }
  async function select(id) {
    if (!Object.prototype.hasOwnProperty.call(PROJECTS, id)) throw workspaceError('project');
    if (loggingOut) return false;
    closeMenu();
    if (id === project && unlocked) return true;
    const ticket = ++selection;
    if (isPrivate() && unlocked) {
      try { await flush(); }
      catch (_) { return false; }
    }
    if (ticket !== selection) return false;
    const token = ++epoch;
    abortRead(); discardQueue(); hideLogin();
    project = id; unlocked = id === 'open'; revision = null;
    setPhase(id === 'open' ? 'idle' : 'loading');
    await config.onLock(id);
    if (!isCurrent(token, id)) return false;
    if (id === 'open') {
      await config.onActivate('open', null);
      if (isCurrent(token, id)) { setPhase('idle'); $('project-menu-toggle').focus(); }
      return true;
    }
    if (loginRequired.has(id)) { setPhase('locked'); showLogin(id); return false; }
    controller = new AbortController();
    try {
      const response = await request(id, 'session', { signal: controller.signal });
      if (!isCurrent(token, id)) return false;
      if (response.status === 401) { setPhase('locked'); showLogin(id); return false; }
      if (!response.ok) throw workspaceError('session');
      const session = await json(response);
      if (!isCurrent(token, id)) return false;
      if (session.authenticated !== true || session.project !== id) throw workspaceError('session');
      return await activatePrivate(id, token, controller.signal);
    } catch (error) {
      if (isCurrent(token, id) && error.name !== 'AbortError') {
        setPhase('locked'); showLogin(id, 'Verbindung nicht möglich. Bitte erneut versuchen.');
      }
      return false;
    }
  }
  async function login(event) {
    event.preventDefault();
    if (submitting || !dialogProject || !isPrivate() || unlocked) return;
    const id = dialogProject, token = epoch, input = $('project-login-password');
    let password = input.value;
    clearPassword();
    if (!password) { setLoginError('Bitte Passwort eingeben.'); input.setAttribute('aria-invalid', 'true'); input.focus(); return; }
    submitting = true; setLoginError('');
    $('project-login-submit').disabled = true;
    $('project-login-submit').textContent = 'Wird geöffnet …';
    setPhase('loading'); abortRead(); controller = new AbortController();
    try {
      const body = JSON.stringify({ password }); password = '';
      const response = await request(id, 'login', { method: 'POST', body, signal: controller.signal });
      if (!isCurrent(token, id)) return;
      if (!response.ok) {
        const message = response.status === 429 ? 'Zu viele Versuche. Bitte später erneut versuchen.' : response.status === 400 || response.status === 401 ? 'Passwort nicht korrekt.' : 'Anmeldung gerade nicht möglich. Bitte erneut versuchen.';
        setPhase('locked'); setLoginError(message); input.setAttribute('aria-invalid', 'true'); input.focus(); return;
      }
      const session = await json(response);
      if (!isCurrent(token, id)) return;
      if (session.authenticated !== true || session.project !== id) throw workspaceError('response');
      if (await activatePrivate(id, token, controller.signal)) loginRequired.delete(id);
    } catch (error) {
      if (isCurrent(token, id) && error.name !== 'AbortError') {
        setPhase('locked'); setLoginError('Anmeldung gerade nicht möglich. Bitte erneut versuchen.'); input.focus();
      }
    } finally {
      password = ''; clearPassword(false); submitting = false;
      if (isCurrent(token, id)) { $('project-login-submit').disabled = false; $('project-login-submit').textContent = 'Öffnen'; }
    }
  }
  function save(settings) {
    if (!initialized || !isPrivate() || !unlocked) return false;
    let serialized;
    try { serialized = JSON.stringify(copySettings(settings === undefined ? config.getSettings() : settings)); }
    catch (_) { setPhase('unsaved'); return false; }
    if (!writing && serialized === savedJson) { pending = null; clearTimer(); if (phase !== 'conflict') setPhase('saved'); return true; }
    pending = { id: project, token: epoch, serialized };
    clearTimer();
    if (phase === 'conflict') return false;
    setPhase(writing ? 'saving' : 'dirty');
    timer = setTimeout(() => { timer = null; drain().catch(() => {}); }, 500);
    return true;
  }
  function drain() {
    if (writing) return writing;
    if (phase === 'conflict') return Promise.reject(workspaceError('conflict'));
    const run = (async () => {
      while (pending && isPrivate() && unlocked) {
        const job = pending; pending = null;
        if (!isCurrent(job.token, job.id)) continue;
        if (job.serialized === savedJson) continue;
        setPhase('saving');
        try {
          const response = await request(job.id, 'state', { method: 'PUT', headers: { 'If-Match': String(revision) }, body: JSON.stringify({ settings: JSON.parse(job.serialized) }) });
          if (!isCurrent(job.token, job.id)) return;
          if (response.status === 401) { await lockPrivate('Sitzung abgelaufen. Bitte erneut anmelden.'); throw workspaceError('unauthorized'); }
          if (response.status === 409) { pending = pending || job; setPhase('conflict'); throw workspaceError('conflict'); }
          if (!response.ok) throw workspaceError('save');
          const data = await json(response);
          if (!isCurrent(job.token, job.id)) return;
          if (!validState(data, job.id) || data.revision <= revision) throw workspaceError('response');
          revision = data.revision; savedJson = job.serialized;
        } catch (error) {
          if (isCurrent(job.token, job.id) && unlocked && phase !== 'conflict') { pending = pending || job; setPhase('unsaved'); }
          throw error;
        }
      }
      if (isPrivate() && unlocked && !pending && phase !== 'conflict') setPhase('saved');
    })();
    writing = run;
    run.finally(() => { if (writing === run) writing = null; }).catch(() => {});
    return run;
  }
  async function flush() {
    clearTimer();
    if (!isPrivate() || !unlocked) return;
    if (phase === 'conflict') throw workspaceError('conflict');
    await drain();
  }
  async function reloadState() {
    if (!isPrivate() || !unlocked) return;
    const id = project, token = ++epoch;
    ++selection; abortRead(); discardQueue(); unlocked = false; revision = null;
    setPhase('loading'); await config.onLock(id);
    if (!isCurrent(token, id)) return;
    controller = new AbortController();
    try { await activatePrivate(id, token, controller.signal); }
    catch (error) { if (isCurrent(token, id) && error.name !== 'AbortError') { setPhase('locked'); showLogin(id, 'Laden gerade nicht möglich. Bitte erneut versuchen.'); } }
  }
  async function logout() {
    if (!isPrivate() || loggingOut) return;
    const id = project;
    loggingOut = true;
    loginRequired.add('floyd'); loginRequired.add('ts');
    updateIdentity();
    let token = epoch;
    try {
      try { await flush(); } catch (_) { /* Logout still revokes the sessions when a save failed. */ }
      token = ++epoch; ++selection; abortRead(); discardQueue(); unlocked = false; revision = null;
      hideLogin(); setPhase('loading'); await config.onLock(id);
      if (!isCurrent(token, id)) return;
      const results = await Promise.allSettled(['floyd', 'ts'].map(target => request(target, 'logout', { method: 'POST', body: '{}' })));
      if (!isCurrent(token, id)) return;
      if (results.some(result => result.status !== 'fulfilled' || (!result.value.ok && result.value.status !== 401))) throw workspaceError('logout');
      loggingOut = false;
      await select('open');
    } catch (_) {
      if (isCurrent(token, id)) { setPhase('locked'); showLogin(id, 'Abmelden gerade nicht möglich. Die Ansicht bleibt gesperrt.'); }
    } finally {
      loggingOut = false; updateIdentity();
    }
  }
  function cancelLogin() { hideLogin(); select('open').catch(() => {}); }
  async function init(options) {
    if (initialized) return api;
    if (!options || typeof options.getSettings !== 'function' || typeof options.onActivate !== 'function' || typeof options.onLock !== 'function') throw workspaceError('configuration');
    const required = ['project-menu-toggle', 'project-menu-label', 'project-menu-emoji', 'project-menu', 'project-cloud-controls', 'project-sync-status', 'project-logout', 'project-sync-banner', 'project-sync-message', 'project-sync-reload', 'project-sync-retry', 'project-login-dialog', 'project-login-form', 'project-login-title', 'project-login-password', 'project-login-error', 'project-login-submit', 'project-login-cancel'];
    if (required.some(id => !$(id))) throw workspaceError('markup');
    config = options; initialized = true;
    $('project-menu-toggle').addEventListener('click', () => $('project-menu').hidden ? openMenu() : closeMenu());
    $('project-menu-toggle').addEventListener('keydown', event => { if (['ArrowDown', 'ArrowUp'].includes(event.key)) { event.preventDefault(); openMenu(event.key === 'ArrowUp'); } });
    $('project-menu').addEventListener('click', event => { const item = event.target.closest('[data-project]'); if (item) select(item.dataset.project).catch(() => {}); });
    $('project-menu').addEventListener('keydown', event => {
      const items = [...$('project-menu').querySelectorAll('[role="menuitem"]:not([hidden])')], index = items.indexOf(document.activeElement);
      if (event.key === 'Escape') { event.preventDefault(); closeMenu(true); }
      else if (event.key === 'Tab') closeMenu();
      else if (['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) { event.preventDefault(); const target = event.key === 'Home' ? 0 : event.key === 'End' ? items.length - 1 : (index + (event.key === 'ArrowDown' ? 1 : -1) + items.length) % items.length; items[target].focus(); }
      else if (event.key.length === 1 && !event.ctrlKey && !event.metaKey && !event.altKey) { const item = items.find(button => (PROJECTS[button.dataset.project]?.label||button.textContent.trim()).toLocaleLowerCase().startsWith(event.key.toLocaleLowerCase())); if (item) item.focus(); }
    });
    document.addEventListener('pointerdown', event => { if (!event.target.closest('.project-picker')) closeMenu(); });
    $('project-login-form').addEventListener('submit', login);
    $('project-login-cancel').addEventListener('click', cancelLogin);
    $('project-login-dialog').addEventListener('cancel', event => { event.preventDefault(); cancelLogin(); });
    $('project-login-dialog').addEventListener('close', () => { if ($('project-login-dialog').open) return; clearPassword(); if (dialogProject !== null) cancelLogin(); });
    $('project-login-password').addEventListener('input', () => { setLoginError(''); $('project-login-password').removeAttribute('aria-invalid'); });
    $('project-logout').addEventListener('click', () => logout().catch(() => {}));
    $('project-sync-reload').addEventListener('click', () => reloadState().catch(() => {}));
    $('project-sync-retry').addEventListener('click', () => flush().catch(() => {}));
    window.addEventListener('beforeunload', event => { if (isPrivate() && unlocked && (pending || writing || phase === 'conflict' || phase === 'unsaved')) { event.preventDefault(); event.returnValue = ''; } });
    project = 'open'; unlocked = true; revision = null; setPhase('idle');
    await config.onActivate('open', null);
    return api;
  }
  const api = Object.freeze({ init, save, flush, select, logout,
    get id() { return project; }, get private() { return isPrivate(); },
    get unlocked() { return unlocked; }, get status() { return phase; }
  });
  window.ProjectWorkspaces = api;
})();
