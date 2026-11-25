import assert from 'node:assert/strict';
import { DatabaseSync } from 'node:sqlite';
import { randomBytes, webcrypto } from 'node:crypto';
import { readFile } from 'node:fs/promises';
import vm from 'node:vm';
import { handleProjectApi, sanitizeProjectSettings, verifyPassword } from '../server/project-api.mjs';

if (!globalThis.crypto) globalThis.crypto = webcrypto;
let checks = 0;
const eq = (actual, expected, message) => { assert.deepEqual(actual, expected, message); checks++; };
const reject = (value, message) => { assert.throws(() => sanitizeProjectSettings(value), error => error.status === 400, message); checks++; };
const depotId = number => 'depot-' + number.toString(16).padStart(8, '0') + '-1234-4567-89ab-0123456789ab';
const custom = (number = 1, overrides = {}) => ({
  id: depotId(number), name: 'Depot ' + number, dividends: true,
  settings: { schemaVersion: 2, includeInOverview: false, initial: 12000, years: 15,
    allocationMode: 'shares', shares: { MSFT: 12.5 }, extraTickers: ['MSFT'],
    weights: { MSFT: 100 }, returnOverrides: { MSFT: 11 }, removed: {}, excluded: {} },
  ...overrides
});

const original = {
  tech: { schemaVersion: 2, allocationMode: 'shares', shares: { MSFT: 21, AAPL: 7 },
    weights: { MSFT: 60, AAPL: 40 }, removed: { TSM: false },
    returnOverrides: { MSFT: 8 }, includeInOverview: false, years: 15 },
  value: { schemaVersion: 2, initial: 19000, monthly: 250, years: 15,
    includeInOverview: true, removed: {}, returnOverrides: {} },
  overview: { years: 15 }, customDepots: [custom()]
};

const clean = sanitizeProjectSettings(original);
eq(clean, original, 'complete private scenario round-trips unchanged');
eq(sanitizeProjectSettings({}).tech.includeInOverview, true, 'old Tech defaults to Overview');
eq(sanitizeProjectSettings({}).value.includeInOverview, true, 'old Value defaults to Overview');
eq(sanitizeProjectSettings({ customDepots: [custom(2, { settings: {} })] }).customDepots[0].settings.includeInOverview, true);
eq(sanitizeProjectSettings({ customDepots: [custom(2, { name: '  Mein   Dividenden-Depot  ' })] }).customDepots[0].name, 'Mein Dividenden-Depot');
eq(sanitizeProjectSettings({ customDepots: [custom(2, { name: '🏛️'.repeat(20) })] }).customDepots.length, 1, 'bounded Unicode names supported');
eq(sanitizeProjectSettings({ customDepots: [custom(2, { dividends: false })] }).customDepots[0].dividends, false, 'Growth type remains explicit');
eq(sanitizeProjectSettings({ customDepots: Array.from({ length: 20 }, (_, i) => custom(i + 1)) }).customDepots.length, 20);

for (const invalid of [null, {}, 'depot-one', 'tech', 'value', '__proto__', depotId(1).toUpperCase(), depotId(1) + '/state']) {
  reject({ customDepots: [custom(1, { id: invalid })] }, 'reject unsafe/ambiguous depot IDs');
}
for (const name of ['', '  ', 'x'.repeat(49), '<script>', 'test\nname', 'test\u200bname', 'Overview', 'OVERVIEW', 'Ｔｅｃｈ Ｓｔｏｎｋｓ', 'Value Stonks', 'A24']) {
  reject({ customDepots: [custom(1, { name })] }, 'reject invalid/reserved name');
}
for (const dividends of [undefined, null, 0, 1, 'true']) reject({ customDepots: [custom(1, { dividends })] });
for (const includeInOverview of [null, 0, 1, 'false']) {
  reject({ tech: { includeInOverview } });
  reject({ customDepots: [custom(1, { settings: { includeInOverview } })] });
}
reject({ customDepots: [custom(1), custom(1, { name: 'Different name' })] }, 'duplicate ID');
reject({ customDepots: [custom(1), custom(2, { name: 'DEPOT 1' })] }, 'case-insensitive duplicate name');
reject({ customDepots: Array.from({ length: 21 }, (_, i) => custom(i + 1)) });
reject({ customDepots: [custom(1, { settings: null })] });
reject({ customDepots: [custom(1, { settings: { shares: { MSFT: -1 } } })] });
reject({ customDepots: [custom(1, { settings: { weights: { MSFT: 101 } } })] });
reject({ customDepots: [custom(1, { settings: { returnOverrides: { MSFT: Infinity } } })] });
reject({ customDepots: [custom(1, { settings: { extraTickers: ['<script>'] } })] });
reject({ customDepots: [custom(1, { owner: 'ts' })] }, 'depot cannot inject another project owner');
reject(JSON.parse('{"customDepots":[{"id":"' + depotId(1) + '","name":"Safe","dividends":true,"settings":{"__proto__":{"admin":true}}}]}'));
reject(JSON.parse('{"customDepots":[{"id":"' + depotId(1) + '","name":"Safe","dividends":true,"settings":{"shares":{"constructor":12}}}]}'));
eq({}.admin, undefined, 'no prototype pollution');

class LocalD1 {
  constructor() { this.sqlite = new DatabaseSync(':memory:'); }
  prepare(sql) { return new Statement(this, sql, []); }
  async batch(statements) {
    this.sqlite.exec('BEGIN IMMEDIATE');
    try { const result = statements.map(statement => statement.execute()); this.sqlite.exec('COMMIT'); return result; }
    catch (error) { this.sqlite.exec('ROLLBACK'); throw error; }
  }
  close() { this.sqlite.close(); }
}
class Statement {
  constructor(db, sql, parameters) { Object.assign(this, { db, sql, parameters }); }
  bind(...parameters) { return new Statement(this.db, this.sql, parameters); }
  execute() { return { success: true, results: this.db.sqlite.prepare(this.sql).all(...this.parameters).map(row => ({ ...row })) }; }
  async first(column) { const row = this.execute().results[0] || null; return column && row ? row[column] : row; }
  async run() { return this.execute(); }
}
async function hash(password) {
  const salt = randomBytes(16), key = await crypto.subtle.importKey('raw', new TextEncoder().encode(password), 'PBKDF2', false, ['deriveBits']);
  const value = await crypto.subtle.deriveBits({ name: 'PBKDF2', hash: 'SHA-256', salt, iterations: 100000 }, key, 256);
  return 'pbkdf2-sha256$100000$' + salt.toString('hex') + '$' + Buffer.from(value).toString('hex');
}
const db = new LocalD1(), origin = 'https://depot-tests.example';
const env = { DB: db, FLOYD_PASSWORD_HASH: await hash('local-test-only-floyd'), TS_PASSWORD_HASH: await hash('local-test-only-ts'),
  SESSION_SECRET: randomBytes(32).toString('hex'),
  PASSWORD_VERIFIER: { idFromName: name => name, get: () => ({ verify: verifyPassword }) } };
async function call(project, action, method = 'GET', body, headers = {}) {
  return handleProjectApi(new Request(`${origin}/api/projects/${project}/${action}`, { method,
    headers: { ...(method !== 'GET' ? { Origin: origin, 'Content-Type': 'application/json' } : {}), ...headers },
    body: body === undefined ? undefined : JSON.stringify(body) }), env);
}
const cookie = response => response.headers.get('set-cookie').split(';')[0];
const save = (project, token, revision, settings) => call(project, 'state', 'PUT', { settings }, { Cookie: token, 'If-Match': String(revision) });

try {
  const floydLogin = await call('floyd', 'login', 'POST', { password: 'local-test-only-floyd' });
  const tsLogin = await call('ts', 'login', 'POST', { password: 'local-test-only-ts' });
  eq(floydLogin.status, 200); eq(tsLogin.status, 200);
  const floydCookie = cookie(floydLogin), tsCookie = cookie(tsLogin);
  const floydInitial = await (await call('floyd', 'state', 'GET', undefined, { Cookie: floydCookie })).json();
  const tsInitial = await (await call('ts', 'state', 'GET', undefined, { Cookie: tsCookie })).json();
  let response = await save('floyd', floydCookie, floydInitial.revision, original);
  eq(response.status, 200);
  let state = await response.json();
  eq(state.settings, original, 'API does not lose holdings/maps when adding custom depot');
  eq(state.settings.customDepots[0].settings.includeInOverview, false, 'custom disabled flag preserved');
  const reread = await (await call('floyd', 'state', 'GET', undefined, { Cookie: floydCookie })).json();
  eq(reread, state, 'separate device read returns complete custom depots');
  eq((await call('ts', 'state', 'GET', undefined, { Cookie: floydCookie })).status, 401, 'Floyd session cannot read TS');
  eq((await save('ts', floydCookie, tsInitial.revision, original)).status, 401, 'Floyd session cannot write TS');
  eq(await (await call('ts', 'state', 'GET', undefined, { Cookie: tsCookie })).json(), tsInitial, 'TS settings are unchanged');
  eq((await save('floyd', floydCookie, floydInitial.revision, original)).status, 409, 'stale device CAS rejected');
  eq((await save('floyd', floydCookie, state.revision, { ...original, customDepots: [custom(1), custom(1)] })).status, 400);
  eq((await (await call('floyd', 'state', 'GET', undefined, { Cookie: floydCookie })).json()).revision, state.revision, 'invalid collection never writes');

  // A still-open old UI has no new collection/toggles. Its CAS update must not
  // erase another device's new feature data or reset a false flag to true.
  const legacy = structuredClone(original);
  delete legacy.customDepots;
  delete legacy.tech.includeInOverview; delete legacy.value.includeInOverview;
  delete legacy.tech.removed; delete legacy.value.removed;
  delete legacy.tech.returnOverrides; delete legacy.value.returnOverrides;
  legacy.tech.monthly = 75;
  response = await save('floyd', floydCookie, state.revision, legacy);
  eq(response.status, 200); state = await response.json();
  eq(state.settings.customDepots, original.customDepots, 'old-client save preserves complete collection atomically');
  eq(state.settings.tech.includeInOverview, false, 'old client preserves false include flag');
  eq(state.settings.value.includeInOverview, true);
  eq(state.settings.tech.removed, original.tech.removed);
  eq(state.settings.tech.returnOverrides, original.tech.returnOverrides);
  eq(state.settings.tech.shares, original.tech.shares);
  eq(state.settings.tech.monthly, 75, 'ordinary old-client change is still applied');

  const toggled = structuredClone(state.settings);
  toggled.tech.includeInOverview = true; toggled.value.includeInOverview = false;
  toggled.customDepots[0].settings.includeInOverview = true;
  response = await save('floyd', floydCookie, state.revision, toggled);
  eq(response.status, 200); state = await response.json();
  eq(state.settings.tech.includeInOverview, true);
  eq(state.settings.value.includeInOverview, false);
  eq(state.settings.customDepots[0].settings.includeInOverview, true);
  eq(state.settings.tech.shares, original.tech.shares, 'toggle does not affect shares');

  const concurrent = await Promise.all([1, 2].map(number => save('floyd', floydCookie, state.revision, { ...state.settings, customDepots: [custom(number)] })));
  eq(concurrent.map(result => result.status).sort(), [200, 409], 'concurrent custom-depot writes stay isolated by revision');
  state = await (await call('floyd', 'state', 'GET', undefined, { Cookie: floydCookie })).json();
  response = await save('floyd', floydCookie, state.revision, { ...state.settings, customDepots: [] });
  eq(response.status, 200); state = await response.json();
  eq(state.settings.customDepots, [], 'explicit empty collection deliberately clears');
  eq(state.settings.value.includeInOverview, false);

  const populated = Array.from({ length: 20 }, (_, n) => custom(n + 1, { settings: {
    schemaVersion: 2, includeInOverview: n % 2 === 0, extraTickers: Array.from({ length: 100 }, (_, i) => 'TEST' + i),
    shares: Object.fromEntries(Array.from({ length: 100 }, (_, i) => ['TEST' + i, 123.5])),
    weights: Object.fromEntries(Array.from({ length: 100 }, (_, i) => ['TEST' + i, 1])),
    returnOverrides: Object.fromEntries(Array.from({ length: 100 }, (_, i) => ['TEST' + i, 11]))
  } }));
  const large = { ...original, customDepots: populated };
  assert.ok(new TextEncoder().encode(JSON.stringify({ settings: large })).length > 65536); checks++;
  response = await save('floyd', floydCookie, state.revision, large);
  eq(response.status, 200, 'bounded many-depot state can exceed legacy 64KiB');
  eq((await response.json()).settings.customDepots.length, 20);
  eq((await call('floyd', 'login', 'POST', { password: 'x'.repeat(70000) })).status, 413, 'login keeps smaller existing budget');
  eq((await save('floyd', floydCookie, state.revision + 1, { ...large, ignored: 'x'.repeat(524288) })).status, 413, 'state hard byte cap enforced');
  eq((await call('floyd', 'state', 'PUT', { settings: original }, { Cookie: floydCookie, 'If-Match': String(state.revision + 1), Origin: 'https://evil.example' })).status, 403);

  const legacyCode = await readFile(new URL('../client/project-import.js', import.meta.url), 'utf8');
  const sandbox = { window: {}, TextEncoder, Intl }; vm.createContext(sandbox); vm.runInContext(legacyCode, sandbox);
  const importText = JSON.stringify({ app: 'research-studio', version: 1, settings: { tech: original.tech, value: original.value, overview: original.overview } });
  sandbox.importText = importText;
  const imported = vm.runInContext('ProjectImportResult = window.ProjectImport.parse(importText); JSON.stringify(ProjectImportResult)', sandbox);
  eq(JSON.parse(imported).tech.includeInOverview, false, 'legacy importer accepts new optional flag');
  eq(JSON.parse(imported).tech.shares, original.tech.shares);
  console.log('PASS', checks, 'custom-depot schema, persistence, backwards compatibility, CAS, input budgets and project isolation checks');
} finally { db.close(); }
