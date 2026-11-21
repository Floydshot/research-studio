(function () {
  'use strict';
  const LIMIT = 65536, TICKER = /^[A-Za-z0-9.^=-]{1,24}$/;
  const FORBIDDEN = new Set(['__proto__', 'prototype', 'constructor']);
  const FIELDS = new Set(['schemaVersion', 'allocationMode', 'initial', 'monthly', 'source', 'manual', 'years', 'extend', 'reinvest', 'excluded', 'removed', 'weights', 'shares', 'extraTickers', 'migrated']);
  const $ = id => document.getElementById(id);
  let config = null, pending = null, generation = 0, initialized = false, applying = false, status = 'idle';
  function invalid() { throw new Error('Bitte eine gültige Depot-Datei auswählen.'); }
  function plain(value) { return value !== null && typeof value === 'object' && !Array.isArray(value) && Object.getPrototypeOf(value) === Object.prototype; }
  function allowedNumber(value, min, max) { return value === null || typeof value === 'number' && Number.isFinite(value) && value >= min && value <= max; }
  function safeTree(value, depth = 0, count = { value: 0 }) {
    if (++count.value > 3000 || depth > 8) invalid();
    if (value && typeof value === 'object') {
      if (!Array.isArray(value) && !plain(value)) invalid();
      for (const [key, child] of Object.entries(value)) { if (FORBIDDEN.has(key)) invalid(); safeTree(child, depth + 1, count); }
    }
  }
  function scenario(value) {
    if (!plain(value) || value.schemaVersion !== 2 || Object.keys(value).some(key => !FIELDS.has(key))) invalid();
    const result = { schemaVersion: 2 };
    for (const [key, min, max] of [['initial', 0, 1e9], ['monthly', 0, 1e7], ['manual', -99, 1000]]) if (value[key] !== undefined) {
      if (!allowedNumber(value[key], min, max)) invalid(); result[key] = value[key];
    }
    for (const [key, allowed] of [['allocationMode', ['weights', 'shares']], ['source', ['manual', '5y', '10y', 'all']], ['years', [5, 10, 15, 20, 30]]]) if (value[key] !== undefined) {
      if (!allowed.includes(value[key])) invalid(); result[key] = value[key];
    }
    for (const key of ['extend', 'reinvest', 'migrated']) if (value[key] !== undefined) { if (typeof value[key] !== 'boolean') invalid(); result[key] = value[key]; }
    for (const key of ['excluded', 'removed', 'weights', 'shares']) if (value[key] !== undefined) {
      if (!plain(value[key]) || Object.keys(value[key]).length > 100) invalid();
      result[key] = {};
      for (const [ticker, amount] of Object.entries(value[key])) {
        if (!TICKER.test(ticker) || FORBIDDEN.has(ticker)) invalid();
        if (key === 'excluded' || key === 'removed') { if (typeof amount !== 'boolean') invalid(); if (amount) result[key][ticker] = true; }
        else { if (!allowedNumber(amount, 0, key === 'shares' ? 1e9 : 100)) invalid(); result[key][ticker] = amount; }
      }
    }
    if (value.extraTickers !== undefined) {
      if (!Array.isArray(value.extraTickers) || value.extraTickers.length > 100 || value.extraTickers.some(ticker => typeof ticker !== 'string' || !TICKER.test(ticker) || FORBIDDEN.has(ticker))) invalid();
      result.extraTickers = [...new Set(value.extraTickers)];
    }
    return result;
  }
  function parse(text) {
    if (typeof text !== 'string' || new TextEncoder().encode(text).byteLength > LIMIT) invalid();
    let value; try { value = JSON.parse(text.replace(/^\uFEFF/, '')); } catch (_) { invalid(); }
    safeTree(value);
    if (!plain(value) || value.app !== 'research-studio' || value.version !== 1 || Object.keys(value).some(key => !['app', 'version', 'settings'].includes(key)) || !plain(value.settings)) invalid();
    if (Object.keys(value.settings).some(key => !['tech', 'value', 'overview'].includes(key))) invalid();
    const settings = { tech: scenario(value.settings.tech), value: scenario(value.settings.value) };
    if (value.settings.overview !== undefined) {
      const overview = value.settings.overview;
      if (!plain(overview) || Object.keys(overview).some(key => key !== 'years') || ![5, 10, 15, 20, 30].includes(overview.years)) invalid();
      settings.overview = { years: overview.years };
    }
    return settings;
  }
  function canImport() {
    try {
      const id = config && typeof config.getProjectId === 'function' ? config.getProjectId() : window.ProjectWorkspaces?.id;
      const unlocked = config && typeof config.isUnlocked === 'function' ? config.isUnlocked() : window.ProjectWorkspaces?.unlocked;
      return id === 'floyd' && unlocked === true;
    } catch (_) { return false; }
  }
  function message(text) {
    const node = $('project-import-error'); if (!node) return;
    node.textContent = text || ''; node.hidden = !text;
  }
  function reset() {
    ++generation; pending = null; applying = false; status = 'idle';
    const file = $('project-import-file'); if (file) { file.value = ''; file.disabled = false; }
    const preview = $('project-import-preview'); if (preview) { preview.replaceChildren(); preview.hidden = true; }
    const name = $('project-import-filename'); if (name) name.textContent = '';
    const apply = $('project-import-apply'); if (apply) { apply.disabled = true; apply.textContent = 'Übernehmen'; }
    const cancel = $('project-import-cancel'); if (cancel) cancel.disabled = false;
    message('');
    const dialog = $('project-import-dialog'); if (dialog?.open) dialog.close();
    const button = $('project-import-legacy'); if (button) button.hidden = !canImport();
  }
  function refresh() {
    if (!canImport()) { reset(); return; }
    $('project-import-legacy').hidden = false;
  }
  function focusImportTrigger() {
    const visible = node => node && !node.disabled && node.getClientRects().length > 0 && getComputedStyle(node).visibility !== 'hidden';
    const action = $('project-import-legacy'), menu = $('project-menu-toggle');
    if (visible(action)) action.focus();
    else if (visible(menu)) menu.focus();
  }
  function money(value) { return value === undefined || value === null ? 'offen' : new Intl.NumberFormat('de-DE', { style: 'currency', currency: 'EUR', maximumFractionDigits: 2 }).format(value); }
  function sharesPreview(settings) {
    const entries = Object.entries(settings.shares || {}).filter(([ticker]) => settings.excluded?.[ticker] !== true && settings.removed?.[ticker] !== true);
    const sum = entries.reduce((total, [, count]) => total + (typeof count === 'number' ? count : 0), 0);
    return 'aus Shares · ' + new Intl.NumberFormat('de-DE', { maximumFractionDigits: 6 }).format(sum) + ' Shares' + (entries.some(([, count]) => count === null) ? ' · offen' : '');
  }
  function renderPreview(settings) {
    const root = $('project-import-preview'); root.replaceChildren();
    const table = document.createElement('table'), head = document.createElement('thead'), header = document.createElement('tr'), body = document.createElement('tbody');
    for (const label of ['Depot', 'Startkapital', 'Monatlich']) { const cell = document.createElement('th'); cell.scope = 'col'; cell.textContent = label; header.append(cell); }
    head.append(header);
    for (const [id, label] of [['tech', 'Tech Stonks'], ['value', 'Value Stonks']]) {
      const settingsRow = settings[id], row = document.createElement('tr'), title = document.createElement('th'); title.scope = 'row'; title.textContent = label; row.append(title);
      for (const value of [settingsRow.allocationMode === 'shares' ? sharesPreview(settingsRow) : money(settingsRow.initial), money(settingsRow.monthly)]) { const cell = document.createElement('td'); cell.textContent = value; row.append(cell); }
      body.append(row);
    }
    table.append(head, body); root.append(table); root.hidden = false;
  }
  function open() {
    if (!canImport()) { reset(); return; }
    reset();
    $('project-import-dialog').showModal();
    $('project-import-file').focus();
  }
  async function readFile() {
    const token = ++generation, input = $('project-import-file'), file = input.files?.[0];
    pending = null; $('project-import-apply').disabled = true; $('project-import-preview').replaceChildren(); $('project-import-preview').hidden = true; $('project-import-filename').textContent = ''; message('');
    input.value = '';
    if (!file || !canImport() || !$('project-import-dialog').open) { if (!canImport() || !$('project-import-dialog').open) reset(); return; }
    if (file.size > LIMIT) { status = 'error'; message('Die Datei ist zu groß.'); return; }
    status = 'reading';
    try {
      const text = await file.text();
      if (token !== generation || !canImport()) return;
      const settings = parse(text);
      if (token !== generation || !canImport()) return;
      pending = settings; status = 'preview'; renderPreview(settings);
      $('project-import-filename').textContent = file.name;
      $('project-import-apply').disabled = false;
    } catch (_) {
      if (token === generation && canImport()) { status = 'error'; message('Bitte eine gültige Depot-Datei auswählen.'); }
    }
  }
  async function apply() {
    if (!pending || applying || !canImport()) { if (!canImport()) reset(); return; }
    const token = generation, settings = JSON.parse(JSON.stringify(pending));
    applying = true; status = 'applying'; message('');
    $('project-import-apply').disabled = true; $('project-import-apply').textContent = 'Wird übernommen …'; $('project-import-file').disabled = true; $('project-import-cancel').disabled = true;
    try {
      await config.applySettings(settings, { projectId: 'floyd', isCurrent: () => token === generation && canImport() });
      if (token !== generation || !canImport()) return;
      reset(); focusImportTrigger();
    } catch (_) {
      if (token === generation && canImport()) { status = 'error'; message('Übernehmen gerade nicht möglich. Bitte erneut versuchen.'); }
    } finally {
      if (token === generation) { applying = false; $('project-import-apply').disabled = !pending; $('project-import-apply').textContent = 'Übernehmen'; $('project-import-file').disabled = false; $('project-import-cancel').disabled = false; }
    }
  }
  function init(options) {
    if (initialized) return api;
    if (!options || typeof options.applySettings !== 'function') throw new Error('Import ist nicht eingerichtet.');
    const ids = ['project-import-legacy', 'project-import-dialog', 'project-import-file', 'project-import-filename', 'project-import-preview', 'project-import-error', 'project-import-cancel', 'project-import-apply'];
    if (ids.some(id => !$(id))) throw new Error('Import ist nicht eingerichtet.');
    config = options; initialized = true;
    $('project-import-legacy').textContent = 'Depot-Eingaben übernehmen';
    $('project-import-legacy').addEventListener('click', open);
    $('project-import-file').addEventListener('change', readFile);
    $('project-import-cancel').addEventListener('click', () => { reset(); focusImportTrigger(); });
    $('project-import-apply').addEventListener('click', apply);
    $('project-import-dialog').addEventListener('cancel', event => { event.preventDefault(); if (!applying) { reset(); focusImportTrigger(); } });
    $('project-import-dialog').addEventListener('close', () => { if (!$('project-import-dialog').open) reset(); });
    reset(); return api;
  }
  const api = Object.freeze({ init, reset, refresh, parse, get status() { return status; } });
  window.ProjectImport = api;
})();
