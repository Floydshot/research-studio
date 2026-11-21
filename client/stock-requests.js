(function () {
  'use strict';

  const $ = id => document.getElementById(id);
  const enabled=document.querySelector('meta[name="stock-requests-enabled"]')?.content!=='false';
  let config = null, initialized = false, pending = null, generation = 0, status = 'idle';

  function context() {
    try {
      const value = config?.getContext();
      return value?.ready === true && ['open', 'floyd', 'ts'].includes(value.projectId) ? { projectId: value.projectId, era: value.era } : null;
    } catch (_) { return null; }
  }
  function isCurrent(job) {
    const current = context();
    return pending === job && job.token === generation && current?.projectId === job.context.projectId && current.era === job.context.era;
  }
  function message(copy = '', kind = '') {
    const node = $('stock-request-message'); node.textContent = copy; node.hidden = !copy; node.className = 'stock-request-message' + (kind ? ' ' + kind : '');
  }
  function controls(busy = false, sent = false) {
    $('stock-request-dialog').setAttribute('aria-busy', String(busy));
    $('stock-request-name').disabled = busy || sent;
    $('stock-request-submit').disabled = busy; $('stock-request-submit').hidden = sent;
    $('stock-request-submit').textContent = busy ? 'Wird gesendet …' : 'Senden';
    $('stock-request-cancel').disabled = busy; $('stock-request-cancel').textContent = sent ? 'Schließen' : 'Abbrechen';
  }
  function reset() {
    ++generation;
    if (pending?.controller) pending.controller.abort();
    pending = null; status = 'idle';
    if (!$('stock-request-dialog')) return;
    $('stock-request-name').value = ''; $('stock-request-name').removeAttribute('aria-invalid');
    message(); controls();
    if ($('stock-request-dialog').open) $('stock-request-dialog').close();
  }
  function cancel() {
    if (status === 'sending') return;
    const job = pending, restore = job && isCurrent(job), trigger = job?.trigger;
    reset();
    if (restore && trigger?.isConnected && trigger.getClientRects().length) trigger.focus({ preventScroll: true });
  }
  function errorCopy(data, fallback) { return typeof data?.error === 'string' && data.error.trim() ? data.error.slice(0, 240) : fallback; }
  async function submit(event) {
    event?.preventDefault();
    const job = pending;
    if (!job || status === 'sending' || status === 'sent') return;
    if (!isCurrent(job)) { reset(); return; }
    const raw = $('stock-request-name').value, name = raw.trim().replace(/\s+/g, ' ');
    if (!name || raw.length > 160 || /[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]/.test(name)) {
      $('stock-request-name').setAttribute('aria-invalid', 'true'); message('Bitte Aktie anpassen.', 'error'); $('stock-request-name').focus(); return;
    }
    $('stock-request-name').removeAttribute('aria-invalid');
    if (job.requestName !== name) { job.requestName = name; job.requestId = crypto.randomUUID(); }
    job.controller = new AbortController(); status = 'sending'; message(); controls(true);
    const timeout = setTimeout(() => job.controller?.abort(), 20000);
    try {
      const response = await fetch('/api/projects/' + job.context.projectId + '/stock-requests', {
        method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
        body: JSON.stringify({ name, requestId: job.requestId, honey: '' }), signal: job.controller.signal
      });
      let data = null; try { data = await response.json(); } catch (_) { /* A missing receipt does not confirm delivery. */ }
      if (!isCurrent(job)) { if (pending === job) reset(); return; }
      if (!response.ok) {
        status = 'error'; message(errorCopy(data, response.status === 401 ? 'Bitte erneut anmelden.' : 'Senden gerade nicht möglich. Bitte erneut versuchen.'), 'error'); return;
      }
      const receipt = typeof data?.requestId === 'string' && data.requestId === job.requestId && /^[\da-f]{8}-[\da-f]{4}-[\da-f]{4}-[\da-f]{4}-[\da-f]{12}$/i.test(data.requestId);
      if (data?.status === 'sent' && receipt) {
        status = 'sent'; message(data.duplicate === true ? 'Diese Aktie wurde bereits angefragt.' : 'Anfrage per E-Mail gesendet.', 'success'); controls(false, true); $('stock-request-cancel').focus();
      } else if (data?.status === 'pending' && receipt) {
        status = 'pending'; message('Anfrage gespeichert. E-Mail-Versand steht noch aus.', 'pending');
      } else {
        status = 'error'; message('Versand noch nicht bestätigt. Bitte erneut versuchen.', 'error');
      }
    } catch (error) {
      if (!isCurrent(job)) { if (pending === job) reset(); return; }
      status = 'error'; message(error.name === 'AbortError' ? 'Versandstatus unklar. Bitte erneut versuchen.' : 'Senden gerade nicht möglich. Bitte erneut versuchen.', 'error');
    } finally {
      clearTimeout(timeout); job.controller = null;
      if (isCurrent(job) && status !== 'sent') controls();
    }
  }
  function build() {
    const style = document.createElement('style'); style.id = 'stock-request-style';
    style.textContent = '.stock-request-dialog{box-sizing:border-box;width:min(380px,calc(100vw - 32px));max-width:100%;padding:22px;border:1px solid var(--line);border-radius:10px;background:var(--surface);color:var(--text)}.stock-request-dialog::backdrop{background:#0007}.stock-request-dialog h2{margin:0 0 18px;font-family:var(--font);font-size:18px;font-weight:600;letter-spacing:-.25px}.stock-request-label{display:flex;flex-direction:column;gap:6px;font-size:11px;color:var(--muted)}.stock-request-label input{width:100%;min-width:0;height:36px;padding:0 10px;border:1px solid var(--line);border-radius:6px;background:var(--bg);color:var(--text);font-size:12px}.stock-request-label input[aria-invalid=true]{border-color:#d3636d;box-shadow:0 0 0 1px #d3636d}.stock-request-actions{display:flex;justify-content:flex-end;gap:8px;margin-top:20px}.stock-request-actions button{min-height:33px;padding:5px 13px;border:1px solid var(--line);border-radius:6px;background:var(--surface-2);color:var(--text);font-size:12px;font-weight:400}.stock-request-actions #stock-request-submit{background:var(--text);color:var(--bg);border-color:transparent}.stock-request-message{margin:12px 0 0;font-size:11px;line-height:1.5;color:var(--muted)}.stock-request-message.error{color:#d3636d}html[data-theme=light] .stock-request-message.error{color:#b83243}@media(max-width:700px){.stock-request-dialog{padding:18px}}';
    style.textContent += '.future-request-button{border:0;font-size:11px;font-weight:400}.stock-request-dialog{font-family:var(--font)}.stock-request-label input{box-sizing:border-box}';
    document.head.append(style);
    const dialog = document.createElement('dialog'); dialog.id = 'stock-request-dialog'; dialog.className = 'stock-request-dialog'; dialog.setAttribute('aria-labelledby', 'stock-request-title'); dialog.setAttribute('aria-describedby', 'stock-request-message');
    const form = document.createElement('form'); form.noValidate = true; form.addEventListener('submit', submit);
    const title = document.createElement('h2'); title.id = 'stock-request-title'; title.textContent = 'Stonk request';
    const label = document.createElement('label'); label.className = 'stock-request-label'; label.htmlFor = 'stock-request-name'; label.textContent = 'Aktie oder Unternehmen';
    const input = document.createElement('input'); input.id = 'stock-request-name'; input.type = 'text'; input.maxLength = 160; input.required = true; input.autocomplete = 'off'; input.spellcheck = false; input.setAttribute('aria-describedby', 'stock-request-message'); input.addEventListener('input', () => { input.removeAttribute('aria-invalid'); if (status !== 'sending' && status !== 'sent') message(); }); label.append(input);
    const copy = document.createElement('p'); copy.id = 'stock-request-message'; copy.className = 'stock-request-message'; copy.hidden = true; copy.setAttribute('role', 'status'); copy.setAttribute('aria-live', 'polite');
    const actions = document.createElement('div'); actions.className = 'stock-request-actions';
    const back = document.createElement('button'); back.id = 'stock-request-cancel'; back.type = 'button'; back.textContent = 'Abbrechen'; back.addEventListener('click', cancel);
    const send = document.createElement('button'); send.id = 'stock-request-submit'; send.type = 'submit'; send.textContent = 'Senden';
    actions.append(back, send); form.append(title, label, copy, actions); dialog.append(form); document.body.append(dialog);
    dialog.addEventListener('cancel', event => { event.preventDefault(); cancel(); });
    dialog.addEventListener('close', () => { if (!dialog.open && pending) cancel(); });
  }
  function open(name, trigger) {
    if (!enabled || !initialized || typeof name !== 'string') return false;
    const current = context(); if (!current) return false;
    reset(); pending = { context: current, trigger, token: generation, requestName: null, requestId: null, controller: null }; status = 'editing';
    $('stock-request-name').value = name.trim().slice(0, 160); $('stock-request-dialog').showModal(); $('stock-request-name').focus(); $('stock-request-name').select(); return true;
  }
  function init(options) {
    if (initialized) return api;
    if (typeof options?.getContext !== 'function') throw new Error('Aktienanfragen sind nicht eingerichtet.');
    config = options; build(); initialized = true; return api;
  }
  const api = Object.freeze({ init, open, reset, enabled, get status() { return status; } });
  window.StockRequests = api;
})();
