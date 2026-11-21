(function () {
  'use strict';

  const TICKER = /^[A-Za-z0-9.^=-]{1,24}$/;
  const $ = id => document.getElementById(id);
  let config = null, initialized = false, pending = null, generation = 0, status = 'idle';

  function currentContext() {
    try {
      const value = config?.getContext();
      if (!value || value.ready !== true || !['open', 'floyd', 'ts'].includes(value.projectId) || !['tech', 'value'].includes(value.portfolioId)) return null;
      return { projectId: value.projectId, portfolioId: value.portfolioId, era: value.era };
    } catch (_) { return null; }
  }
  function sameContext(snapshot) {
    const current = currentContext();
    return current !== null && current.projectId === snapshot.projectId && current.portfolioId === snapshot.portfolioId && current.era === snapshot.era;
  }
  function isCurrent(job) { return pending === job && job.token === generation && sameContext(job.context); }
  function visible(node) { return node?.isConnected && node.getClientRects().length > 0; }
  function restoreFocus(job, removed = false) {
    if (!job || !sameContext(job.context)) return;
    const preferred = removed ? job.nextFocus : job.trigger;
    const target = visible(preferred) ? preferred : visible(job.trigger) ? job.trigger : $('future-catalog-search');
    if (visible(target)) target.focus({ preventScroll: true });
  }
  function reset() {
    ++generation; pending = null; status = 'idle';
    const stock = $('project-removal-stock'), error = $('project-removal-error'), yes = $('project-removal-yes'), no = $('project-removal-no'), dialog = $('project-removal-dialog');
    if (stock) stock.textContent = '';
    if (error) { error.textContent = ''; error.hidden = true; }
    if (yes) { yes.disabled = false; yes.textContent = 'yes'; }
    if (no) no.disabled = false;
    if (dialog?.open) dialog.close();
  }
  function cancel() {
    if (status === 'removing') return;
    const job = pending;
    reset(); restoreFocus(job);
  }
  async function confirm() {
    const job = pending;
    if (!job || status === 'removing') return;
    if (!isCurrent(job)) { reset(); return; }
    status = 'removing'; $('project-removal-yes').disabled = true; $('project-removal-no').disabled = true;
    const error = $('project-removal-error'); error.textContent = ''; error.hidden = true;
    const context = Object.freeze({ projectId: job.context.projectId, portfolioId: job.context.portfolioId, isCurrent: () => isCurrent(job) });
    try {
      const removed = await config.onRemove(job.ticker, context);
      if (!isCurrent(job)) { if (pending === job) reset(); return; }
      reset(); restoreFocus(job, removed !== false);
    } catch (_) {
      if (!isCurrent(job)) { if (pending === job) reset(); return; }
      status = 'error'; error.textContent = 'Entfernen gerade nicht möglich. Bitte erneut versuchen.'; error.hidden = false;
    } finally {
      if (isCurrent(job)) { $('project-removal-yes').disabled = false; $('project-removal-no').disabled = false; }
    }
  }
  function buildDialog() {
    const style = document.createElement('style'); style.id = 'project-removal-style';
    style.textContent = '.future-card-removal{display:flex;justify-content:flex-end;margin-top:8px}.future-card-remove{border:0;background:transparent;color:var(--muted);font-size:10px;font-weight:400;line-height:1.4;padding:4px 0 0;cursor:pointer}.future-card-remove:hover{color:var(--text)}.project-removal-dialog{width:min(330px,calc(100vw - 32px));max-width:100%;padding:22px;border:1px solid var(--line);border-radius:10px;background:var(--surface);color:var(--text)}.project-removal-dialog::backdrop{background:#0007}.project-removal-dialog h2{margin:0;font-family:var(--font);font-size:18px;font-weight:600;letter-spacing:-.25px}.project-removal-actions{display:flex;justify-content:flex-end;gap:8px;margin-top:20px}.project-removal-actions button{min-height:33px;padding:5px 13px;border:1px solid var(--line);border-radius:6px;background:var(--surface-2);color:var(--text);font-size:12px;font-weight:400}.project-removal-actions #project-removal-yes{background:var(--text);color:var(--bg);border-color:transparent}.project-removal-error{color:#d3636d;font-size:11px;margin:12px 0 0}@media(max-width:700px){.project-removal-dialog{padding:18px}}';
    style.textContent += '.future-card{display:flex;flex-direction:column}.future-card.excluded{opacity:1}.future-card.excluded>:not(.future-card-removal){opacity:.58}.future-card-removal{margin-top:auto;padding-top:8px}.project-removal-dialog{box-sizing:border-box}';
    document.head.append(style);
    const dialog = document.createElement('dialog'); dialog.id = 'project-removal-dialog'; dialog.className = 'project-removal-dialog';
    dialog.setAttribute('aria-labelledby', 'project-removal-title'); dialog.setAttribute('aria-describedby', 'project-removal-stock project-removal-error');
    const title = document.createElement('h2'); title.id = 'project-removal-title'; title.textContent = 'Kick this stonk out?';
    const stock = document.createElement('p'); stock.id = 'project-removal-stock'; stock.className = 'sr-only';
    const error = document.createElement('p'); error.id = 'project-removal-error'; error.className = 'project-removal-error'; error.hidden = true; error.setAttribute('role', 'alert');
    const actions = document.createElement('div'); actions.className = 'project-removal-actions';
    const no = document.createElement('button'); no.id = 'project-removal-no'; no.type = 'button'; no.textContent = 'no'; no.addEventListener('click', cancel);
    const yes = document.createElement('button'); yes.id = 'project-removal-yes'; yes.type = 'button'; yes.textContent = 'yes'; yes.addEventListener('click', () => { confirm().catch(() => {}); });
    actions.append(no, yes); dialog.append(title, stock, error, actions); document.body.append(dialog);
    dialog.addEventListener('cancel', event => { event.preventDefault(); cancel(); });
    dialog.addEventListener('close', () => { if (!dialog.open && pending) cancel(); });
  }
  function open(ticker, trigger) {
    if (!initialized || typeof ticker !== 'string' || !TICKER.test(ticker)) return false;
    const context = currentContext(); if (!context) return false;
    const card = trigger?.closest?.('.future-card'), nextFocus = card?.nextElementSibling?.querySelector('.future-card-toggle');
    reset(); pending = { ticker, trigger, nextFocus, context, token: generation }; status = 'confirming';
    $('project-removal-stock').textContent = ticker;
    $('project-removal-dialog').showModal(); $('project-removal-no').focus(); return true;
  }
  function init(options) {
    if (initialized) return api;
    if (!options || typeof options.onRemove !== 'function' || typeof options.getContext !== 'function') throw new Error('Entfernen ist nicht eingerichtet.');
    config = options; buildDialog(); initialized = true; return api;
  }
  const api = Object.freeze({ init, open, reset, get status() { return status; } });
  window.ProjectRemoval = api;
})();
