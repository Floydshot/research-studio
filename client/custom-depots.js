(function () {
  'use strict';

  const $ = id => document.getElementById(id);
  const MAX_DEPOTS = 20;
  const CUSTOM_ID = /^depot-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
  const RESERVED = new Set(['tech stonks', 'value stonks', 'overview', 'a24']);
  let config = null, initialized = false, generation = 0, active = null, creating = false;

  function context() {
    try {
      const value = config?.getContext();
      if (!value || value.ready !== true || value.private !== true || value.unlocked !== true || !['floyd', 'ts'].includes(value.projectId)) return null;
      return value;
    } catch (_) { return null; }
  }
  function sameProject(job) {
    const value = context();
    return !!(value && job && value.projectId === job.projectId);
  }
  function current(job) { return active === job && job.token === generation && sameProject(job); }
  function customCount(value) { return (Array.isArray(value.depots) ? value.depots : []).filter(depot => depot && CUSTOM_ID.test(depot.id)).length; }
  function cleanName(value) { return value.normalize('NFKC').trim().replace(/\s+/gu, ' '); }
  function normalized(value) { return cleanName(String(value || '')).toLocaleLowerCase('de-DE'); }
  function nameValue(value, view) {
    if (typeof value !== 'string') return null;
    if (/[\p{Cc}\p{Cf}\p{Cs}<>]/u.test(value)) return null;
    const name = cleanName(value), key = normalized(name);
    if (!name || [...name].length > 48 || RESERVED.has(key)) return null;
    if ((Array.isArray(view.depots) ? view.depots : []).some(depot => depot && normalized(depot.name) === key)) return null;
    return name;
  }
  function message(text, invalidName = false) {
    $('custom-depot-error').textContent = text || '';
    $('custom-depot-error').hidden = !text;
    if (invalidName) {
      $('custom-depot-name').setAttribute('aria-invalid', 'true');
      $('custom-depot-name').setAttribute('aria-describedby', 'custom-depot-error');
    } else {
      $('custom-depot-name').removeAttribute('aria-invalid');
      $('custom-depot-name').removeAttribute('aria-describedby');
    }
  }
  function updateBusy() {
    $('custom-depot-name').disabled = creating;
    $('custom-depot-dividends-yes').disabled = creating;
    $('custom-depot-dividends-no').disabled = creating;
    $('custom-depot-cancel').disabled = creating;
    $('custom-depot-create').disabled = creating;
    $('custom-depot-create').textContent = creating ? 'Wird erstellt …' : 'Depot erstellen';
    $('custom-depot-form').setAttribute('aria-busy', String(creating));
  }
  function reset() {
    ++generation; active = null; creating = false;
    if (!initialized) return;
    $('custom-depot-name').value = '';
    $('custom-depot-dividends-yes').checked = false;
    $('custom-depot-dividends-no').checked = true;
    message(''); updateBusy();
    if ($('custom-depot-dialog').open) $('custom-depot-dialog').close();
  }
  function focusTrigger(job, created = false) {
    if (!sameProject(job)) return;
    const selected = created && document.querySelector('.tabs [role="tab"][aria-selected="true"]');
    const target = selected || $('custom-depot-open');
    if (target && !target.disabled && target.getClientRects().length) target.focus();
  }
  function cancel() {
    if (creating) return;
    const job = active; reset(); focusTrigger(job);
  }
  function sync() {
    if (!initialized) return;
    const value = context(), button = $('custom-depot-open');
    button.hidden = !value;
    button.disabled = !value || customCount(value) >= MAX_DEPOTS;
    button.title = value && customCount(value) >= MAX_DEPOTS ? 'Maximal 20 eigene Depots' : 'Neues Depot';
    if (active && (!value || value.projectId !== active.projectId)) reset();
  }
  function open() {
    const value = context();
    if (!value || customCount(value) >= MAX_DEPOTS) { sync(); return; }
    reset();
    active = { projectId: value.projectId, token: generation };
    $('custom-depot-dialog').showModal(); $('custom-depot-name').focus();
  }
  async function create(event) {
    event.preventDefault();
    const job = active, value = context();
    if (!job || creating) return;
    if (!current(job) || !value) { reset(); sync(); return; }
    const name = nameValue($('custom-depot-name').value, value);
    if (!name) { message('Bitte Depotnamen anpassen', true); $('custom-depot-name').focus(); return; }
    if (customCount(value) >= MAX_DEPOTS) { message('Maximal 20 eigene Depots möglich.'); return; }
    const dividends = $('custom-depot-dividends-yes').checked;
    creating = true; message(''); updateBusy();
    try {
      await config.onCreate({ name, dividends }, Object.freeze({ projectId: job.projectId, isCurrent: () => current(job) }));
      if (!current(job)) return;
      reset(); sync(); focusTrigger(job, true);
    } catch (error) {
      if (!current(job)) return;
      if (error?.code === 'name') message('Bitte Depotnamen anpassen', true);
      else message('Depot konnte nicht gespeichert werden. Bitte erneut versuchen.');
    } finally {
      if (current(job)) { creating = false; updateBusy(); }
    }
  }
  function build() {
    const nav = document.querySelector('.tabs');
    if (!nav || !nav.parentNode) throw new Error('Depotnavigation fehlt.');
    const style = document.createElement('style'); style.id = 'custom-depot-style';
    style.textContent = '.custom-depot-tabs{display:flex;align-items:flex-end;column-gap:18px;row-gap:10px;min-width:0;margin-bottom:19px}.custom-depot-tabs>.tabs{flex:1;min-width:0;margin:0}.custom-depot-tabs>[hidden]{display:none!important}.custom-depot-tabs>#future-overview-option{--outlook-value-blue:var(--accent-blue);flex:none;align-self:flex-end;white-space:nowrap;margin:0;padding:0 0 13px}.custom-depot-open{flex:none;border:0;background:transparent;color:var(--muted);font:inherit;font-size:12px;font-weight:500;line-height:1.5;white-space:nowrap;padding:0 0 13px;cursor:pointer}.custom-depot-open:hover{color:var(--text)}.custom-depot-open:focus-visible{outline:2px solid var(--sky);outline-offset:4px}.custom-depot-open:disabled{opacity:.45;cursor:default}.custom-depot-dialog{box-sizing:border-box;width:min(360px,calc(100vw - 32px));max-width:100%;padding:22px;border:1px solid var(--line);border-radius:10px;background:var(--surface);color:var(--text);font-family:var(--font)}.custom-depot-dialog::backdrop{background:#0007}.custom-depot-dialog h2{margin:0 0 18px;font-size:18px;font-weight:600;letter-spacing:-.25px}.custom-depot-name-label{display:block;margin-bottom:5px;color:var(--muted);font-size:11px}.custom-depot-name{display:block;box-sizing:border-box;width:100%;height:38px;padding:0 10px;border:1px solid var(--line);border-radius:6px;background:var(--bg);color:var(--text);font:inherit;font-size:14px}.custom-depot-name[aria-invalid=true]{border-color:#d3636d;outline-color:#d3636d}.custom-depot-type{border:0;padding:0;margin:17px 0 0}.custom-depot-type legend{padding:0;margin-bottom:8px;color:var(--muted);font-size:11px}.custom-depot-type-options{display:flex;gap:22px}.custom-depot-type-options label{display:inline-flex;align-items:center;gap:6px;color:var(--text);font-size:12px;cursor:pointer}.custom-depot-type-options input{accent-color:var(--outlook-value-blue,var(--sky));margin:0}.custom-depot-error{color:#d3636d;font-size:11px;margin:12px 0 0}.custom-depot-actions{display:flex;justify-content:flex-end;gap:8px;margin-top:22px}.custom-depot-actions button{min-height:33px;padding:5px 13px;border:1px solid var(--line);border-radius:6px;background:var(--surface-2);color:var(--text);font:inherit;font-size:12px;font-weight:400;cursor:pointer}.custom-depot-actions #custom-depot-create{background:var(--text);color:var(--bg);border-color:transparent}.custom-depot-actions button:disabled{opacity:.55;cursor:default}html[data-theme="light"] .custom-depot-error{color:#b73542}@media(max-width:700px){.custom-depot-tabs{flex-wrap:wrap;column-gap:16px;row-gap:8px;margin-bottom:14px}.custom-depot-tabs>.tabs{flex:1 0 100%;max-width:100%}.custom-depot-open{font-size:11px;padding-bottom:12px}.custom-depot-tabs>#future-overview-option{padding-bottom:12px}.custom-depot-dialog{padding:18px}}';
    document.head.append(style);
    const wrapper = document.createElement('div'); wrapper.className = 'custom-depot-tabs';
    nav.parentNode.insertBefore(wrapper, nav); wrapper.append(nav);
    const trigger = document.createElement('button'); trigger.id = 'custom-depot-open'; trigger.className = 'custom-depot-open'; trigger.type = 'button'; trigger.textContent = '+ New Depot'; trigger.hidden = true; trigger.setAttribute('aria-haspopup', 'dialog'); trigger.setAttribute('aria-controls', 'custom-depot-dialog'); trigger.addEventListener('click', open); wrapper.append(trigger);
    const overviewOption = $('future-overview-option'); if (overviewOption) wrapper.append(overviewOption);
    const dialog = document.createElement('dialog'); dialog.id = 'custom-depot-dialog'; dialog.className = 'custom-depot-dialog'; dialog.setAttribute('aria-labelledby', 'custom-depot-title');
    const form = document.createElement('form'); form.id = 'custom-depot-form'; form.noValidate = true;
    const title = document.createElement('h2'); title.id = 'custom-depot-title'; title.textContent = 'New Depot';
    const nameLabel = document.createElement('label'); nameLabel.className = 'custom-depot-name-label'; nameLabel.htmlFor = 'custom-depot-name'; nameLabel.textContent = 'Name';
    const name = document.createElement('input'); name.id = 'custom-depot-name'; name.className = 'custom-depot-name'; name.type = 'text'; name.maxLength = 96; name.autocomplete = 'off'; name.required = true;
    const type = document.createElement('fieldset'); type.className = 'custom-depot-type';
    const legend = document.createElement('legend'); legend.textContent = 'Dividendendepot';
    const options = document.createElement('div'); options.className = 'custom-depot-type-options';
    for (const [id, caption, checked] of [['yes', 'Ja', false], ['no', 'Nein', true]]) {
      const label = document.createElement('label'), radio = document.createElement('input');
      radio.type = 'radio'; radio.name = 'custom-depot-dividends'; radio.id = 'custom-depot-dividends-' + id; radio.value = id; radio.checked = checked; radio.setAttribute('aria-label', 'Dividendendepot ' + caption);
      label.append(radio, document.createTextNode(caption)); options.append(label);
    }
    type.append(legend, options);
    const error = document.createElement('p'); error.id = 'custom-depot-error'; error.className = 'custom-depot-error'; error.hidden = true; error.setAttribute('role', 'alert');
    const actions = document.createElement('div'); actions.className = 'custom-depot-actions';
    const cancelButton = document.createElement('button'); cancelButton.id = 'custom-depot-cancel'; cancelButton.type = 'button'; cancelButton.textContent = 'Abbrechen'; cancelButton.addEventListener('click', cancel);
    const submit = document.createElement('button'); submit.id = 'custom-depot-create'; submit.type = 'submit'; submit.textContent = 'Depot erstellen';
    actions.append(cancelButton, submit); form.append(title, nameLabel, name, type, error, actions); form.addEventListener('submit', event => { create(event).catch(() => {}); }); dialog.append(form); document.body.append(dialog);
    name.addEventListener('input', () => message(''));
    dialog.addEventListener('cancel', event => { event.preventDefault(); cancel(); });
    dialog.addEventListener('close', () => { if (!dialog.open && active && !creating) { const job = active; reset(); focusTrigger(job); } });
  }
  function init(options) {
    if (initialized) return api;
    if (!options || typeof options.getContext !== 'function' || typeof options.onCreate !== 'function') throw new Error('Depotfunktion ist nicht eingerichtet.');
    config = options; build(); initialized = true; updateBusy(); sync(); return api;
  }
  const api = Object.freeze({ init, reset, sync });
  window.CustomDepots = api;
})();
