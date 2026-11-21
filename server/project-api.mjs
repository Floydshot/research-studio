import { handleStockRequests } from './stock-requests.mjs';

/**
 * Private project API for the Research Studio Cloudflare Worker.
 *
 * Bind a D1 database as DB and ProjectPasswordVerifier as PASSWORD_VERIFIER.
 * Password stretching runs in the private Durable Object so the front Worker
 * does not rely on CPU-limit spike allowances. Configure FLOYD_PASSWORD_HASH,
 * TS_PASSWORD_HASH and SESSION_SECRET as runtime secrets, never static assets.
 * Password format: pbkdf2-sha256$100000$<salt hex, 16+ bytes>$<32-byte hash hex>.
 * Regional Sites secrets use SECRET_<NAME>.get(); their presence takes
 * precedence and an unavailable regional binding always fails closed.
 *
 * PUT /api/projects/:project/state accepts {settings:{tech,value,overview?}} and requires
 * If-Match: <revision>. Numeric nulls are allowed as unfinished input drafts.
 * Public market data and the public Open Projects scenario are not stored here.
 */

const ENCODER = new TextEncoder();
const MAX_BODY_BYTES = 65536;
const SESSION_MS = 12 * 60 * 60 * 1000;
const ATTEMPT_WINDOW_MS = 15 * 60 * 1000;
const MAX_ATTEMPTS = 5;
const TICKER = /^[A-Za-z0-9.^=-]{1,24}$/;
const FORBIDDEN_KEYS = new Set(['__proto__', 'constructor', 'prototype']);
const PRIVATE_PROJECTS = new Set(['floyd', 'ts']);
const INITIAL_SETTINGS = '{"tech":{},"value":{}}';
const schemaPromises = new WeakMap();
const MEMBERSHIP_MIGRATION = 'project-membership-2026-11-21-v1';
const FLOYD_VALUE_MEMBERSHIP = Object.freeze(['ALV.DE', 'CS.PA', 'MUV2.DE', 'HNR1.DE', 'D05.SI']);
const MEMBERSHIP_REMOVALS = Object.freeze({
  floyd: Object.freeze({
    tech: Object.freeze({ AVGO: true, TSM: true }),
    value: Object.freeze({ '8630.T': true, 'ZURN.SW': true, 'SREN.SW': true, '8725.T': true, '8766.T': true, 'SLHN.SW': true, GS: true, JPM: true, MS: true, AFL: true, JEPQ: true })
  }),
  ts: Object.freeze({ tech: Object.freeze({ NET: true, NOW: true }) })
});

export const PROJECT_CATALOG = Object.freeze([
  Object.freeze({ id: 'open', name: 'Open Projects', emoji: '🌐', protected: false }),
  Object.freeze({ id: 'floyd', name: 'Floyd Projects', emoji: '🏛️', protected: true }),
  Object.freeze({ id: 'ts', name: 'TS Projects', emoji: '🏛️', protected: true })
]);

class ApiError extends Error {
  constructor(status, message, headers = {}) { super(message); this.status = status; this.headers = headers; }
}

function json(value, status = 200, extraHeaders = {}) {
  return new Response(JSON.stringify(value), {
    status,
    headers: {
      'Content-Type': 'application/json; charset=utf-8',
      'Cache-Control': 'private, no-store',
      'Pragma': 'no-cache',
      'X-Content-Type-Options': 'nosniff',
      ...extraHeaders
    }
  });
}

async function readSecret(env, name) {
  const regional = env['SECRET_' + name];
  let value;
  if (regional !== undefined) {
    if (!regional || typeof regional.get !== 'function') throw new ApiError(503, 'Projektzugang ist noch nicht eingerichtet.');
    value = await regional.get();
  } else {
    value = env[name];
  }
  if (typeof value !== 'string' || !value.trim()) throw new ApiError(503, 'Projektzugang ist noch nicht eingerichtet.');
  return value;
}

function bytesToHex(bytes) { return [...bytes].map(value => value.toString(16).padStart(2, '0')).join(''); }
function hexToBytes(value) {
  if (typeof value !== 'string' || !/^(?:[a-f0-9]{2})+$/.test(value)) throw new ApiError(503, 'Projektzugang ist noch nicht eingerichtet.');
  return Uint8Array.from(value.match(/../g), pair => parseInt(pair, 16));
}
async function digest(value) { return bytesToHex(new Uint8Array(await crypto.subtle.digest('SHA-256', ENCODER.encode(value)))); }

function parsePasswordHash(value) {
  const parts = value.split('$');
  if (parts.length !== 4 || parts[0] !== 'pbkdf2-sha256' || parts[1] !== '100000' || !/^[a-f0-9]{32,128}$/.test(parts[2]) || parts[2].length % 2 || !/^[a-f0-9]{64}$/.test(parts[3])) {
    throw new ApiError(503, 'Projektzugang ist noch nicht eingerichtet.');
  }
  return { salt: hexToBytes(parts[2]), expected: hexToBytes(parts[3]), iterations: 100000 };
}

export async function verifyPassword(password, encoded) {
  if (typeof password !== 'string' || password.length < 1 || password.length > 256) return false;
  if (typeof encoded !== 'string' || encoded.length > 256) throw new ApiError(503, 'Projektzugang ist noch nicht eingerichtet.');
  const { salt, expected, iterations } = parsePasswordHash(encoded);
  const key = await crypto.subtle.importKey('raw', ENCODER.encode(password), 'PBKDF2', false, ['deriveBits']);
  const actual = new Uint8Array(await crypto.subtle.deriveBits({ name: 'PBKDF2', hash: 'SHA-256', salt, iterations }, key, 256));
  // Workers exposes a native timing-resistant comparison. The standards-only
  // local adapter uses native HMAC verification rather than a JavaScript loop.
  if (typeof crypto.subtle.timingSafeEqual === 'function') return crypto.subtle.timingSafeEqual(actual, expected);
  const comparisonKey = await crypto.subtle.generateKey({ name: 'HMAC', hash: 'SHA-256' }, false, ['sign', 'verify']);
  const signature = await crypto.subtle.sign('HMAC', comparisonKey, expected);
  return await crypto.subtle.verify('HMAC', comparisonKey, signature, actual);
}

async function verifyViaDurableObject(env, project, password, encoded) {
  const namespace = env.PASSWORD_VERIFIER;
  if (!namespace || typeof namespace.idFromName !== 'function' || typeof namespace.get !== 'function') throw new ApiError(503, 'Projektzugang ist noch nicht eingerichtet.');
  const stub = namespace.get(namespace.idFromName('project:' + project));
  if (!stub || typeof stub.verify !== 'function') throw new ApiError(503, 'Projektzugang ist noch nicht eingerichtet.');
  // No local fallback: an unavailable private verifier must fail closed.
  const result = await stub.verify(password, encoded);
  if (typeof result !== 'boolean') throw new ApiError(503, 'Projektzugang ist gerade nicht verfügbar.');
  return result;
}

function assertSameOrigin(request) {
  const origin = request.headers.get('Origin');
  if (!origin || origin !== new URL(request.url).origin || request.headers.get('Sec-Fetch-Site') === 'cross-site') {
    throw new ApiError(403, 'Diese Anfrage ist nicht erlaubt.');
  }
}

async function readJson(request) {
  if (!(request.headers.get('Content-Type') || '').toLowerCase().split(';')[0].trim().endsWith('/json')) throw new ApiError(415, 'Bitte JSON-Daten senden.');
  const declared = request.headers.get('Content-Length');
  if (declared !== null && (!/^\d+$/.test(declared) || Number(declared) > MAX_BODY_BYTES)) throw new ApiError(413, 'Die Eingabe ist zu groß.');
  const chunks = []; let length = 0;
  if (request.body) {
    const reader = request.body.getReader();
    try {
      while (true) {
        const { done, value } = await reader.read(); if (done) break;
        length += value.byteLength;
        if (length > MAX_BODY_BYTES) { await reader.cancel(); throw new ApiError(413, 'Die Eingabe ist zu groß.'); }
        chunks.push(value);
      }
    } finally { reader.releaseLock(); }
  }
  const bytes = new Uint8Array(length); let offset = 0;
  for (const chunk of chunks) { bytes.set(chunk, offset); offset += chunk.byteLength; }
  try { return JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(bytes)); }
  catch (_) { throw new ApiError(400, 'Bitte Eingabe anpassen.'); }
}

function plainObject(value) { return value !== null && typeof value === 'object' && !Array.isArray(value) && Object.getPrototypeOf(value) === Object.prototype; }
function inputError() { throw new ApiError(400, 'Bitte Portfolio-Eingaben anpassen.'); }
function numeric(value, minimum, maximum) { return value === null || typeof value === 'number' && Number.isFinite(value) && value >= minimum && value <= maximum; }

function sanitizeScenario(input) {
  if (!plainObject(input)) inputError();
  const result = {};
  for (const key of Object.keys(input)) if (FORBIDDEN_KEYS.has(key)) inputError();
  if (input.schemaVersion !== undefined && input.schemaVersion !== 2) inputError();
  result.schemaVersion = 2;
  for (const [key, minimum, maximum] of [['initial', 0, 1e9], ['monthly', 0, 1e7], ['manual', -99, 1000]]) {
    if (input[key] !== undefined) { if (!numeric(input[key], minimum, maximum)) inputError(); result[key] = input[key]; }
  }
  for (const [key, allowed] of [['allocationMode', ['weights', 'shares']], ['source', ['manual', '5y', '10y', 'all']], ['years', [5, 10, 15, 20, 30]]]) {
    if (input[key] !== undefined) { if (!allowed.includes(input[key])) inputError(); result[key] = input[key]; }
  }
  for (const key of ['extend', 'reinvest', 'migrated']) if (input[key] !== undefined) {
    if (typeof input[key] !== 'boolean') inputError(); result[key] = input[key];
  }
  for (const key of ['excluded', 'removed', 'weights', 'shares']) if (input[key] !== undefined) {
    if (!plainObject(input[key]) || Object.keys(input[key]).length > 100) inputError();
    result[key] = {};
    for (const [ticker, value] of Object.entries(input[key])) {
      if (!TICKER.test(ticker) || FORBIDDEN_KEYS.has(ticker)) inputError();
      if (key === 'excluded' || key === 'removed') { if (typeof value !== 'boolean') inputError(); if (value || key === 'removed') result[key][ticker] = value; }
      else { if (!numeric(value, 0, key === 'shares' ? 1e9 : 100)) inputError(); result[key][ticker] = value; }
    }
  }
  if (input.extraTickers !== undefined) {
    if (!Array.isArray(input.extraTickers) || input.extraTickers.length > 100 || input.extraTickers.some(ticker => typeof ticker !== 'string' || !TICKER.test(ticker) || FORBIDDEN_KEYS.has(ticker))) inputError();
    result.extraTickers = [...new Set(input.extraTickers)];
  }
  return result;
}

export function sanitizeProjectSettings(input) {
  if (!plainObject(input)) inputError();
  for (const key of Object.keys(input)) if (FORBIDDEN_KEYS.has(key)) inputError();
  // An explicit allowlist prevents settings from another project or A24 being
  // persisted accidentally; unrelated ordinary properties are discarded.
  const result = { tech: sanitizeScenario(input.tech === undefined ? {} : input.tech), value: sanitizeScenario(input.value === undefined ? {} : input.value) };
  if (input.overview !== undefined) {
    if (!plainObject(input.overview) || ![5, 10, 15, 20, 30].includes(input.overview.years)) inputError();
    result.overview = { years: input.overview.years };
  }
  return result;
}

async function ensureSchema(db) {
  if (!db || typeof db.prepare !== 'function' || typeof db.batch !== 'function') throw new ApiError(503, 'Projektspeicher ist noch nicht eingerichtet.');
  if (!schemaPromises.has(db)) {
    const promise = db.batch([
      db.prepare("CREATE TABLE IF NOT EXISTS project_states (project TEXT PRIMARY KEY CHECK(project IN ('floyd','ts')), revision INTEGER NOT NULL DEFAULT 0, settings TEXT NOT NULL, updated_at INTEGER NOT NULL)"),
      db.prepare("CREATE TABLE IF NOT EXISTS project_sessions (session_hash TEXT PRIMARY KEY, project TEXT NOT NULL CHECK(project IN ('floyd','ts')), credential_version TEXT NOT NULL, expires_at INTEGER NOT NULL)"),
      db.prepare('CREATE INDEX IF NOT EXISTS project_sessions_expiry ON project_sessions(expires_at)'),
      db.prepare("CREATE TABLE IF NOT EXISTS project_login_attempts (project TEXT NOT NULL CHECK(project IN ('floyd','ts')), ip_hash TEXT NOT NULL, failures INTEGER NOT NULL, window_start INTEGER NOT NULL, PRIMARY KEY(project,ip_hash))"),
      db.prepare("CREATE TABLE IF NOT EXISTS project_state_migrations (project TEXT NOT NULL CHECK(project IN ('floyd','ts')), migration TEXT NOT NULL, applied_at INTEGER NOT NULL, PRIMARY KEY(project,migration))"),
      db.prepare('INSERT OR IGNORE INTO project_states(project,revision,settings,updated_at) VALUES(?,0,?,?)').bind('floyd', INITIAL_SETTINGS, Date.now()),
      db.prepare('INSERT OR IGNORE INTO project_states(project,revision,settings,updated_at) VALUES(?,0,?,?)').bind('ts', INITIAL_SETTINGS, Date.now())
    ]).catch(error => { schemaPromises.delete(db); throw error; });
    schemaPromises.set(db, promise);
  }
  await schemaPromises.get(db);
}

async function ensureProfileMembershipMigration(db, project) {
  const applied = await db.prepare('SELECT 1 AS applied FROM project_state_migrations WHERE project=? AND migration=?').bind(project, MEMBERSHIP_MIGRATION).first();
  if (applied) return;
  const entries = Object.entries(MEMBERSHIP_REMOVALS[project]), now = Date.now();
  // Depot names are fixed literals from MEMBERSHIP_REMOVALS, never client input.
  // The conditional UPDATE and marker INSERT share one D1 transaction. A
  // concurrent request either observes the marker or skips the UPDATE, so a
  // later catalog restoration is never overwritten by a second migration.
  const parameters = [];
  const paths = entries.map(([depot, removed]) => {
    parameters.push(JSON.stringify(removed));
    let removals = 'json(?)';
    if (project === 'floyd' && depot === 'value') {
      // Existing catalog additions also leave this depot during this one-time
      // reset. Read them inside the UPDATE so a concurrent write cannot be lost.
      // Keep their saved shares/weights and list intact for a later restoration.
      removals = `json_patch(json(?),(SELECT json_group_object(value,json('true')) FROM json_each(CASE WHEN json_type(settings,'$.value.extraTickers')='array' THEN json_extract(settings,'$.value.extraTickers') ELSE '[]' END) WHERE type='text' AND value NOT IN (${FLOYD_VALUE_MEMBERSHIP.map(() => '?').join(',')})))`;
      parameters.push(...FLOYD_VALUE_MEMBERSHIP);
    }
    return ` '$.${depot}.removed', json_patch(CASE WHEN json_type(settings,'$.${depot}.removed')='object' THEN json_extract(settings,'$.${depot}.removed') ELSE '{}' END,${removals})`;
  }).join(',');
  await db.batch([
    db.prepare(`UPDATE project_states SET settings=json_set(settings,${paths}),revision=revision+1,updated_at=? WHERE project=? AND NOT EXISTS(SELECT 1 FROM project_state_migrations WHERE project=? AND migration=?)`).bind(...parameters, now, project, project, MEMBERSHIP_MIGRATION),
    db.prepare('INSERT OR IGNORE INTO project_state_migrations(project,migration,applied_at) SELECT project,?,? FROM project_states WHERE project=?').bind(MEMBERSHIP_MIGRATION, now, project)
  ]);
}

function cookieName(project) { return '__Secure-research-' + project; }
function sessionCookie(project, token, maxAge) { return cookieName(project) + '=' + token + '; Path=/api/projects/' + project + '; Max-Age=' + maxAge + '; HttpOnly; Secure; SameSite=Strict'; }
function readCookie(request, project) {
  const prefix = cookieName(project) + '=';
  const values = (request.headers.get('Cookie') || '').split(';').map(part => part.trim()).filter(part => part.startsWith(prefix)).map(part => part.slice(prefix.length));
  return values.length === 1 && /^[a-f0-9]{64}$/.test(values[0]) ? values[0] : null;
}
function passwordKey(project) { return project === 'floyd' ? 'FLOYD_PASSWORD_HASH' : 'TS_PASSWORD_HASH'; }

async function requireSession(request, env, project) {
  if (request.headers.get('Sec-Fetch-Site') === 'cross-site') throw new ApiError(403, 'Diese Anfrage ist nicht erlaubt.');
  const token = readCookie(request, project);
  if (!token) throw new ApiError(401, 'Bitte anmelden.');
  const sessionHash = await digest(token);
  const session = await env.DB.prepare('SELECT project,credential_version,expires_at FROM project_sessions WHERE session_hash=? AND project=? AND expires_at>?').bind(sessionHash, project, Date.now()).first();
  if (!session) throw new ApiError(401, 'Bitte anmelden.');
  const encoded = await readSecret(env, passwordKey(project)); parsePasswordHash(encoded);
  if (session.credential_version !== await digest(encoded)) {
    await env.DB.prepare('DELETE FROM project_sessions WHERE session_hash=?').bind(sessionHash).run();
    throw new ApiError(401, 'Bitte erneut anmelden.');
  }
  return { sessionHash, expiresAt: session.expires_at };
}

async function attemptKey(request, env, project) {
  const secret = await readSecret(env, 'SESSION_SECRET');
  if (secret.length < 32) throw new ApiError(503, 'Projektzugang ist noch nicht eingerichtet.');
  const key = await crypto.subtle.importKey('raw', ENCODER.encode(secret), { name: 'HMAC', hash: 'SHA-256' }, false, ['sign']);
  return bytesToHex(new Uint8Array(await crypto.subtle.sign('HMAC', key, ENCODER.encode(project + ':' + (request.headers.get('CF-Connecting-IP') || 'unknown')))));
}

async function login(request, env, project) {
  const body = await readJson(request);
  if (!plainObject(body) || typeof body.password !== 'string' || body.password.length < 1 || body.password.length > 256) throw new ApiError(400, 'Bitte Passwort anpassen.');
  const encoded = await readSecret(env, passwordKey(project)); parsePasswordHash(encoded);
  const ipHash = await attemptKey(request, env, project), now = Date.now(), threshold = now - ATTEMPT_WINDOW_MS;
  // The increment is atomic, so parallel attempts cannot bypass the limit.
  const attempt = await env.DB.prepare('INSERT INTO project_login_attempts(project,ip_hash,failures,window_start) VALUES(?,?,1,?) ON CONFLICT(project,ip_hash) DO UPDATE SET failures=CASE WHEN window_start<=? THEN 1 ELSE MIN(failures+1,6) END,window_start=CASE WHEN window_start<=? THEN excluded.window_start ELSE window_start END RETURNING failures,window_start').bind(project, ipHash, now, threshold, threshold).first();
  if (!attempt || attempt.failures > MAX_ATTEMPTS) {
    const wait = Math.max(1, Math.ceil(((attempt?.window_start || now) + ATTEMPT_WINDOW_MS - now) / 1000));
    throw new ApiError(429, 'Zu viele Versuche. Bitte später erneut versuchen.', { 'Retry-After': String(wait) });
  }
  if (!await verifyViaDurableObject(env, project, body.password, encoded)) throw new ApiError(401, 'Passwort stimmt nicht.');
  const token = bytesToHex(crypto.getRandomValues(new Uint8Array(32))), expiresAt = now + SESSION_MS;
  const oldToken = readCookie(request, project);
  const statements = [
    env.DB.prepare('DELETE FROM project_login_attempts WHERE project=? AND ip_hash=?').bind(project, ipHash),
    env.DB.prepare('DELETE FROM project_sessions WHERE expires_at<=?').bind(now),
    env.DB.prepare('DELETE FROM project_login_attempts WHERE window_start<?').bind(threshold),
    env.DB.prepare('INSERT INTO project_sessions(session_hash,project,credential_version,expires_at) VALUES(?,?,?,?)').bind(await digest(token), project, await digest(encoded), expiresAt)
  ];
  if (oldToken) statements.push(env.DB.prepare('DELETE FROM project_sessions WHERE session_hash=? AND project=?').bind(await digest(oldToken), project));
  await env.DB.batch(statements);
  return json({ authenticated: true, project, expiresAt }, 200, { 'Set-Cookie': sessionCookie(project, token, SESSION_MS / 1000) });
}

async function stateResponse(env, project) {
  const row = await env.DB.prepare('SELECT revision,settings FROM project_states WHERE project=?').bind(project).first();
  if (!row) throw new ApiError(503, 'Projektspeicher ist nicht verfügbar.');
  return json({ project, revision: row.revision, settings: JSON.parse(row.settings) }, 200, { ETag: '"' + row.revision + '"' });
}

/** Return null for routes outside this API so the caller can serve assets. */
export async function handleProjectApi(request, env) {
  const pathname = new URL(request.url).pathname;
  if (pathname !== '/api/projects' && !pathname.startsWith('/api/projects/')) return null;
  try {
    if (pathname === '/api/projects' || pathname === '/api/projects/') {
      if (request.method !== 'GET') throw new ApiError(405, 'Diese Methode ist nicht erlaubt.', { Allow: 'GET' });
      return json({ projects: PROJECT_CATALOG });
    }
    const stockRequestRoute = /^\/api\/projects\/(open|floyd|ts)\/stock-requests$/.exec(pathname);
    if (stockRequestRoute) {
      const project = stockRequestRoute[1];
      if (request.method !== 'POST') throw new ApiError(405, 'Diese Methode ist nicht erlaubt.', { Allow: 'POST' });
      assertSameOrigin(request);
      if (project !== 'open') { await ensureSchema(env.DB); await requireSession(request, env, project); }
      return await handleStockRequests(request, env, { project });
    }
    const route = /^\/api\/projects\/(floyd|ts)\/(login|session|state|logout)$/.exec(pathname);
    if (!route || !PRIVATE_PROJECTS.has(route[1])) throw new ApiError(404, 'Projekt nicht gefunden.');
    const [, project, action] = route;
    const allowed = action === 'state' ? ['GET', 'PUT'] : action === 'session' ? ['GET'] : ['POST'];
    if (!allowed.includes(request.method)) throw new ApiError(405, 'Diese Methode ist nicht erlaubt.', { Allow: allowed.join(', ') });
    if (request.method !== 'GET') assertSameOrigin(request);
    await ensureSchema(env.DB);
    if (action === 'login') return await login(request, env, project);
    const session = await requireSession(request, env, project);
    if (action === 'session') return json({ authenticated: true, project, expiresAt: session.expiresAt });
    if (action === 'logout') {
      await env.DB.prepare('DELETE FROM project_sessions WHERE session_hash=? AND project=?').bind(session.sessionHash, project).run();
      return json({ authenticated: false, project }, 200, { 'Set-Cookie': sessionCookie(project, '', 0) });
    }
    await ensureProfileMembershipMigration(env.DB, project);
    if (request.method === 'GET') return await stateResponse(env, project);
    const match = /^(?:"(\d+)"|(\d+))$/.exec(request.headers.get('If-Match') || '');
    const expected = match ? Number(match[1] ?? match[2]) : NaN;
    if (!Number.isSafeInteger(expected) || expected < 0) throw new ApiError(428, 'Bitte Projekt neu laden.');
    const body = await readJson(request);
    if (!plainObject(body)) inputError();
    const settings = sanitizeProjectSettings(body.settings);
    // Missing removed maps identify an older client. Preserve the current maps
    // inside the same CAS UPDATE rather than reading then merging in JavaScript.
    // An explicitly supplied map (including {}) is a deliberate membership edit.
    const techRemovedProvided = plainObject(body.settings.tech) && Object.prototype.hasOwnProperty.call(body.settings.tech, 'removed');
    const valueRemovedProvided = plainObject(body.settings.value) && Object.prototype.hasOwnProperty.call(body.settings.value, 'removed');
    const update = await env.DB.prepare("UPDATE project_states SET settings=json_set(?, '$.tech.removed',json(CASE WHEN ? THEN ? ELSE COALESCE(json_extract(settings,'$.tech.removed'),'{}') END), '$.value.removed',json(CASE WHEN ? THEN ? ELSE COALESCE(json_extract(settings,'$.value.removed'),'{}') END)),revision=revision+1,updated_at=? WHERE project=? AND revision=? RETURNING revision,settings").bind(JSON.stringify(settings), techRemovedProvided ? 1 : 0, JSON.stringify(settings.tech.removed || {}), valueRemovedProvided ? 1 : 0, JSON.stringify(settings.value.removed || {}), Date.now(), project, expected).first();
    if (!update) throw new ApiError(409, 'Projekt wurde inzwischen geändert. Bitte neu laden.');
    return json({ project, revision: update.revision, settings: JSON.parse(update.settings) }, 200, { ETag: '"' + update.revision + '"' });
  } catch (error) {
    if (error instanceof ApiError) return json({ error: error.message }, error.status, error.headers);
    // Do not send database errors, runtime secrets, passwords or session tokens.
    return json({ error: 'Projekt ist gerade nicht verfügbar. Bitte erneut versuchen.' }, 503);
  }
}
