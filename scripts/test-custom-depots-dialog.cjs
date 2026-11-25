const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const root = path.resolve(__dirname, '..');
const customSource = fs.readFileSync(path.join(root, 'client/custom-depots.js'), 'utf8');
const removalSource = fs.readFileSync(path.join(root, 'client/project-removal.js'), 'utf8');
let checks = 0;
function eq(actual, expected, message) { checks++; assert.deepEqual(actual, expected, message); }
const tick = () => new Promise(resolve => setImmediate(resolve));
const id = index => 'depot-00000000-0000-4000-8000-' + String(index).padStart(12, '0');

function dom({overviewToggle = false} = {}) {
  const ids = new Map();
  const document = { activeElement: null };
  class Element {
    constructor(tag) {
      this.tagName = tag.toUpperCase(); this.children = []; this.parentNode = null;
      this.attributes = new Map(); this.listeners = new Map(); this.hidden = false;
      this.disabled = false; this.open = false; this.value = ''; this.checked = false;
      this.className = ''; this.textContent = ''; this._id = '';
    }
    set id(value) { if (this._id) ids.delete(this._id); this._id = value; if (value) ids.set(value, this); }
    get id() { return this._id; }
    append(...nodes) { for (const node of nodes) { if (node.parentNode) node.parentNode.children.splice(node.parentNode.children.indexOf(node), 1); node.parentNode = this; this.children.push(node); } }
    insertBefore(node, reference) { node.parentNode = this; this.children.splice(this.children.indexOf(reference), 0, node); }
    setAttribute(key, value) { this.attributes.set(key, String(value)); }
    getAttribute(key) { return this.attributes.has(key) ? this.attributes.get(key) : null; }
    removeAttribute(key) { this.attributes.delete(key); }
    addEventListener(name, listener) { if (!this.listeners.has(name)) this.listeners.set(name, []); this.listeners.get(name).push(listener); }
    dispatch(name, extras = {}) { const event = { target: this, defaultPrevented: false, preventDefault() { this.defaultPrevented = true; }, ...extras }; for (const listener of this.listeners.get(name) || []) listener(event); return event; }
    click() { if (!this.disabled) this.dispatch('click'); }
    focus() { document.activeElement = this; }
    showModal() { this.open = true; }
    close() { if (!this.open) return; this.open = false; this.dispatch('close'); }
    getClientRects() { let node = this; while (node) { if (node.hidden) return []; node = node.parentNode; } return [{}]; }
    closest() { return null; }
    querySelector() { return null; }
  }
  document.createElement = tag => new Element(tag);
  document.createTextNode = text => ({ textContent: text, parentNode: null });
  document.getElementById = key => ids.get(key) || null;
  document.head = new Element('head'); document.body = new Element('body');
  const nav = new Element('nav'); nav.className = 'tabs'; nav.setAttribute('role', 'tablist');
  const selected = new Element('button'); selected.setAttribute('role', 'tab'); selected.setAttribute('aria-selected', 'true'); nav.append(selected);
  document.body.append(nav);
  let toggleChanges = 0, overviewOption = null, overviewInput = null;
  if (overviewToggle) {
    const inputs = new Element('div'); inputs.className = 'future-depot-options';
    overviewOption = new Element('label'); overviewOption.id = 'future-overview-option'; overviewOption.hidden = true;
    overviewOption.className = 'future-check future-reinvest-label';
    overviewInput = new Element('input'); overviewInput.id = 'future-add-overview'; overviewInput.checked = true;
    overviewInput.addEventListener('change', () => { toggleChanges++; });
    overviewOption.append(overviewInput); inputs.append(overviewOption); document.body.append(inputs);
  }
  document.querySelector = selector => selector === '.tabs' ? nav : selector === '.tabs [role="tab"][aria-selected="true"]' ? selected : null;
  return { document, nav, selected, overviewOption, overviewInput, toggleChanges:() => toggleChanges, get: key => ids.get(key) };
}

function fixture(start = {}, options = {}) {
  const html = dom(options);
  let current = { ready: true, private: true, unlocked: true, projectId: 'floyd', depots: [{ id:'tech', name:'Tech Stonks' }, { id:'value', name:'Value Stonks' }], ...start };
  let onCreate = async () => {};
  const calls = [];
  const window = {};
  const context = vm.createContext({ window, document:html.document, console });
  vm.runInContext(customSource, context);
  window.CustomDepots.init({ getContext:() => current, onCreate:async (entry, scope) => { calls.push({ entry:JSON.parse(JSON.stringify(entry)), scope }); return onCreate(entry, scope); } });
  return { ...html, context, window, calls, setContext:value => { current=value; }, getContext:() => current,
           setCreate:handler => { onCreate=handler; }, loadRemoval:() => vm.runInContext(removalSource, context) };
}
function type(f, name) { f.get('custom-depot-name').value = name; f.get('custom-depot-name').dispatch('input'); }
async function submit(f) { f.get('custom-depot-form').dispatch('submit'); await tick(); }

(async () => {
  const f = fixture();
  eq(Object.isFrozen(f.window.CustomDepots), true, 'API frozen');
  eq(f.get('custom-depot-open').hidden, false, 'private ready workspace exposes trigger');
  eq(f.nav.parentNode.className, 'custom-depot-tabs', 'navigation wrapped');
  eq(f.get('custom-depot-open').parentNode, f.nav.parentNode, 'trigger is a sibling of tablist');
  eq(f.nav.children.includes(f.get('custom-depot-open')), false, 'action is outside role=tablist');
  f.get('custom-depot-open').click();
  eq(f.get('custom-depot-dialog').open, true, 'native dialog opens');
  eq(f.document.activeElement, f.get('custom-depot-name'), 'name receives focus');
  eq(f.get('custom-depot-dividends-no').checked, true, 'growth default');
  await submit(f);
  eq(f.calls.length, 0, 'empty name never submitted');
  eq(f.get('custom-depot-name').getAttribute('aria-invalid'), 'true', 'invalid field marked');
  eq(f.get('custom-depot-error').textContent, 'Bitte Depotnamen anpassen');
  type(f, '  Mein   Wachstum  ');
  eq(f.get('custom-depot-name').getAttribute('aria-invalid'), null, 'editing clears invalid state');
  await submit(f);
  eq(f.calls[0].entry, { name:'Mein Wachstum', dividends:false }, 'canonical growth payload');
  eq(f.get('custom-depot-dialog').open, false, 'success closes dialog');
  eq(f.document.activeElement, f.selected, 'new selected tab receives focus');

  f.get('custom-depot-open').click(); type(f, 'Income');
  f.get('custom-depot-dividends-yes').checked = true;
  f.get('custom-depot-dividends-no').checked = false;
  await submit(f);
  eq(f.calls.at(-1).entry, {name:'Income', dividends:true}, 'dividend payload');

  for (const name of ['', '  ', 'Tech Stonks', 'ｔｅｃｈ　Ｓｔｏｎｋｓ', 'VALUE STONKS', 'Overview', 'A24', '<b>Test</b>', 'Test\n', '\tTest', 'Hidden\u200bName', 'x'.repeat(49)]) {
    f.get('custom-depot-open').click(); type(f, name); const before=f.calls.length; await submit(f);
    eq(f.calls.length, before, 'reject unsafe/reserved name ' + JSON.stringify(name));
    eq(f.get('custom-depot-name').getAttribute('aria-invalid'), 'true');
    f.get('custom-depot-cancel').click();
  }
  f.setContext({...f.getContext(), depots:[...f.getContext().depots, {id:id(1),name:'Café'}]});
  f.get('custom-depot-open').click(); type(f, 'Cafe\u0301'); const beforeDuplicate=f.calls.length; await submit(f);
  eq(f.calls.length, beforeDuplicate, 'unicode-equivalent duplicate rejected');
  f.get('custom-depot-cancel').click();
  f.get('custom-depot-open').click(); type(f, '🚀'.repeat(48)); await submit(f);
  eq(f.calls.at(-1).entry.name, '🚀'.repeat(48), '48 unicode code points accepted');

  const limited = fixture({depots:Array.from({length:20},(_,i)=>({id:id(i),name:'Depot '+i}))});
  eq(limited.get('custom-depot-open').disabled, true, 'bounded depot limit');
  const publicView = fixture({projectId:'open',private:false});
  eq(publicView.get('custom-depot-open').hidden, true, 'public view cannot create shared depots');
  const locked = fixture({unlocked:false});
  eq(locked.get('custom-depot-open').hidden, true, 'locked view cannot create');

  const moved = fixture({}, {overviewToggle:true});
  const wrapper = moved.nav.parentNode;
  eq(moved.overviewOption.parentNode, wrapper, 'existing overview label moved beside action');
  eq(wrapper.children.indexOf(moved.overviewOption), wrapper.children.indexOf(moved.get('custom-depot-open')) + 1, 'toggle directly follows New Depot');
  eq(moved.get('future-overview-option'), moved.overviewOption, 'existing label identity retained');
  eq(moved.get('future-add-overview'), moved.overviewInput, 'existing checkbox identity retained');
  eq(moved.overviewInput.checked, true, 'moving does not change allocation setting');
  moved.overviewInput.dispatch('change'); eq(moved.toggleChanges(), 1, 'existing event handler survives the move');
  moved.overviewOption.hidden = false; moved.overviewInput.checked = false;
  moved.window.CustomDepots.sync();
  eq(moved.overviewOption.hidden, false, 'root retains visibility ownership');
  eq(moved.overviewInput.checked, false, 'root retains selected-depot setting ownership');

  const pending = fixture(); let finish;
  pending.setCreate(() => new Promise(resolve=>{finish=resolve;}));
  pending.get('custom-depot-open').click(); type(pending, 'Pending'); await submit(pending);
  eq(pending.get('custom-depot-create').disabled, true, 'creation is busy');
  eq(pending.get('custom-depot-name').disabled, true);
  await submit(pending); eq(pending.calls.length, 1, 'double submit ignored');
  pending.get('custom-depot-cancel').click();
  eq(pending.get('custom-depot-dialog').open, true, 'cannot interrupt a write with cancel');
  eq(pending.get('custom-depot-dialog').dispatch('cancel').defaultPrevented, true, 'escape is controlled during save');
  eq(pending.calls[0].scope.isCurrent(), true);
  pending.setContext({ready:true,projectId:'ts',private:true,unlocked:true,depots:[]}); pending.window.CustomDepots.sync();
  eq(pending.get('custom-depot-dialog').open, false, 'project switch closes dialog');
  eq(pending.calls[0].scope.isCurrent(), false, 'stale completion cannot affect another workspace');
  finish(); await tick();
  eq(pending.get('custom-depot-name').value, '', 'stale input cleared');
  eq(pending.get('custom-depot-create').disabled, false, 'new workspace is not left busy');

  const failing=fixture(); failing.setCreate(async()=>{throw new Error('unavailable');});
  failing.get('custom-depot-open').click(); type(failing,'Retry'); await submit(failing);
  eq(failing.get('custom-depot-dialog').open,true,'service failure remains reviewable');
  eq(failing.get('custom-depot-create').disabled,false,'failure can be retried');
  eq(failing.get('custom-depot-name').value,'Retry','failure preserves input');
  eq(failing.get('custom-depot-error').textContent,'Depot konnte nicht gespeichert werden. Bitte erneut versuchen.');

  const removal=fixture(); let removalContext={ready:true,projectId:'floyd',portfolioId:id(1)};
  removal.loadRemoval(); removal.window.ProjectRemoval.init({getContext:()=>removalContext,onRemove:async()=>true});
  eq(removal.window.ProjectRemoval.open('MSFT',null),true,'custom private stock removal works'); removal.window.ProjectRemoval.reset();
  removalContext={...removalContext,projectId:'open'};
  eq(removal.window.ProjectRemoval.open('MSFT',null),false,'no custom removal context in public workspace');
  removalContext={...removalContext,projectId:'ts',portfolioId:'depot-unsafe/../tech'};
  eq(removal.window.ProjectRemoval.open('MSFT',null),false,'invalid custom ID rejected');
  removalContext={...removalContext,portfolioId:'tech'};
  eq(removal.window.ProjectRemoval.open('MSFT',null),true,'existing tech removal unchanged');
  console.log('PASS',checks,'custom depot dialog, name validation, scope guards, async failure and removal checks');
})().catch(error=>{console.error(error);process.exit(1);});
