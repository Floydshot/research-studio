/**
 * Append-only stock-name requests. This module cannot change the market
 * catalog, application code or portfolio settings.
 *
 * The caller authenticates private project routes before calling this module.
 * Open Projects uses the same origin/body checks and persistent request limits.
 * Bind DB and SESSION_SECRET, plus server-only STOCK_REQUEST_FROM/TO. Optional
 * regional SECRET_<NAME>.get() bindings take precedence and fail closed.
 *
 * STOCK_REQUEST_EMAIL.send({from,to,subject,text,idempotencyKey}) must reject
 * when the provider has not accepted the email. A resolved promise means
 * acceptance, not inbox delivery. Never retry an uncertain send automatically.
 */

const MAX_BODY_BYTES = 2048;
const MAX_PER_IP_DAY = 5;
const MAX_GLOBAL_DAY = 30;
const DAY_MS = 86400000;
const SEND_TIMEOUT_MS = 8000;
const PROJECTS = Object.freeze({ open: 'Open Projects', floyd: 'Floyd Projects', ts: 'TS Projects' });
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
const BODY_KEYS = new Set(['name', 'ticker', 'requestId', 'honey']);
const schemaPromises = new WeakMap();

class RequestError extends Error {
  constructor(status, message, headers = {}) { super(message); this.status = status; this.headers = headers; }
}

function response(value, status = 200, headers = {}) {
  return new Response(JSON.stringify(value), { status, headers: {
    'Content-Type': 'application/json; charset=utf-8', 'Cache-Control': 'private, no-store',
    'Pragma': 'no-cache', 'X-Content-Type-Options': 'nosniff', ...headers
  } });
}

async function secret(env, name) {
  const regional = env['SECRET_' + name];
  const value = regional !== undefined
    ? regional && typeof regional.get === 'function' ? await regional.get() : null
    : env[name];
  if (typeof value !== 'string' || !value.trim()) throw new Error('configuration_unavailable');
  return value;
}

async function readBody(request) {
  if ((request.headers.get('Content-Type') || '').toLowerCase().split(';')[0].trim() !== 'application/json') throw new RequestError(415, 'Bitte JSON-Daten senden.');
  const declared = request.headers.get('Content-Length');
  if (declared !== null && (!/^\d+$/.test(declared) || Number(declared) > MAX_BODY_BYTES)) throw new RequestError(413, 'Die Eingabe ist zu groß.');
  const chunks = []; let length = 0;
  if (request.body) {
    const reader = request.body.getReader();
    try {
      while (true) {
        const { value, done } = await reader.read(); if (done) break;
        length += value.byteLength;
        if (length > MAX_BODY_BYTES) { await reader.cancel(); throw new RequestError(413, 'Die Eingabe ist zu groß.'); }
        chunks.push(value);
      }
    } finally { reader.releaseLock(); }
  }
  const bytes = new Uint8Array(length); let offset = 0;
  for (const chunk of chunks) { bytes.set(chunk, offset); offset += chunk.byteLength; }
  let body;
  try { body = JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(bytes)); }
  catch (_) { throw new RequestError(400, 'Bitte Aktiennamen anpassen.'); }
  if (!body || typeof body !== 'object' || Array.isArray(body) || Object.getPrototypeOf(body) !== Object.prototype || Object.keys(body).some(key => !BODY_KEYS.has(key))) throw new RequestError(400, 'Bitte Eingabe anpassen.');
  if (typeof body.honey !== 'string' || body.honey !== '') throw new RequestError(400, 'Bitte Eingabe anpassen.');
  if (typeof body.requestId !== 'string' || !UUID.test(body.requestId)) throw new RequestError(400, 'Bitte Anfrage erneut öffnen.');
  function text(value, maximum, optional = false) {
    if (optional && value === undefined) return null;
    if (typeof value !== 'string') throw new RequestError(400, 'Bitte Aktiennamen anpassen.');
    const cleaned = value.normalize('NFKC').trim();
    if ((!cleaned && !optional) || [...cleaned].length > maximum || /[\p{Cc}\p{Cf}\p{Cs}<>]/u.test(cleaned)) throw new RequestError(400, 'Bitte Aktiennamen anpassen.');
    return cleaned.replace(/\s+/gu, ' ') || null;
  }
  const name = text(body.name, 160), ticker = text(body.ticker, 24, true);
  return { name, ticker, requestId: body.requestId.toLowerCase(), normalizedName: name.toLocaleLowerCase('en-US') };
}

async function ensureSchema(db) {
  if (!db || typeof db.batch !== 'function' || typeof db.prepare !== 'function') throw new Error('storage_unavailable');
  if (!schemaPromises.has(db)) {
    const promise = db.batch([
      db.prepare("CREATE TABLE IF NOT EXISTS stock_requests (id TEXT PRIMARY KEY, project TEXT NOT NULL CHECK(project IN ('open','floyd','ts')), name TEXT NOT NULL, normalized_name TEXT NOT NULL, ticker TEXT, ip_hash TEXT NOT NULL, created_at INTEGER NOT NULL, admission_token TEXT NOT NULL UNIQUE, duplicate_of TEXT REFERENCES stock_requests(id), status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','sending','sent')), email_attempted_at INTEGER, email_sent_at INTEGER, last_error TEXT)"),
      db.prepare('CREATE INDEX IF NOT EXISTS stock_requests_name_time ON stock_requests(normalized_name,created_at)'),
      db.prepare("CREATE TABLE IF NOT EXISTS stock_request_daily_limits (kind TEXT NOT NULL CHECK(kind IN ('ip','global')), limit_key TEXT NOT NULL, day TEXT NOT NULL, requests INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(kind,limit_key,day))")
    ]).catch(error => { schemaPromises.delete(db); throw error; });
    schemaPromises.set(db, promise);
  }
  await schemaPromises.get(db);
}

async function ipHash(request, env) {
  const keyMaterial = await secret(env, 'SESSION_SECRET');
  if (keyMaterial.length < 32) throw new Error('configuration_unavailable');
  const encoder = new TextEncoder();
  const key = await crypto.subtle.importKey('raw', encoder.encode(keyMaterial), { name: 'HMAC', hash: 'SHA-256' }, false, ['sign']);
  const signed = await crypto.subtle.sign('HMAC', key, encoder.encode('stock-request-ip:' + (request.headers.get('CF-Connecting-IP') || 'unknown')));
  return [...new Uint8Array(signed)].map(byte => byte.toString(16).padStart(2, '0')).join('');
}

function sameRequest(row, project, input) {
  return row.project === project && row.normalized_name === input.normalizedName && row.ticker === input.ticker;
}

async function resultForRow(db, row, input, isNew) {
  const target = row.duplicate_of
    ? await db.prepare('SELECT status FROM stock_requests WHERE id=?').bind(row.duplicate_of).first()
    : row;
  return response({ requestId: input.requestId, status: target?.status === 'sent' ? 'sent' : 'pending', duplicate: !!row.duplicate_of || !isNew }, isNew ? 201 : 200);
}

async function sendOnce(db, env, row) {
  const claim = await db.prepare("UPDATE stock_requests SET status='sending',email_attempted_at=? WHERE id=? AND status='pending' AND email_attempted_at IS NULL AND duplicate_of IS NULL RETURNING id").bind(Date.now(), row.id).first();
  if (!claim) return;
  let timer, timedOut = false;
  try {
    const accepted = await Promise.race([
      (async () => {
        const from = await secret(env, 'STOCK_REQUEST_FROM'), to = await secret(env, 'STOCK_REQUEST_TO');
        if (from.length > 320 || /[\r\n]/.test(from) || !/^[^\s@,;<>]+@[^\s@,;<>]+\.[^\s@,;<>]+$/.test(to)) throw new Error('configuration_unavailable');
        if (!env.STOCK_REQUEST_EMAIL || typeof env.STOCK_REQUEST_EMAIL.send !== 'function') throw new Error('configuration_unavailable');
        // A slow regional configuration lookup must not dispatch mail after
        // this request has already reported an uncertain timeout.
        if (timedOut) throw new Error('provider_timeout');
        return await env.STOCK_REQUEST_EMAIL.send({
          from, to, subject: 'Research Studio · Stonk request',
          text: ['Neue Aktienanfrage', '', 'Position: ' + row.name, row.ticker ? 'Symbol: ' + row.ticker : '', 'Projekt: ' + PROJECTS[row.project], 'Angefragt: ' + new Date(row.created_at).toISOString(), 'Anfrage-ID: ' + row.id, '', 'Bitte Gesellschaft und Börsennotierung prüfen. Diese Anfrage ändert weder Website noch Katalog oder Depot.'].filter(Boolean).join('\n'),
          idempotencyKey: 'stock-request-' + row.id
        });
      })(),
      new Promise((_, reject) => { timer = setTimeout(() => { timedOut = true; reject(new Error('provider_timeout')); }, SEND_TIMEOUT_MS); })
    ]);
    if (accepted === false || accepted?.accepted === false || accepted?.success === false || accepted?.ok === false || accepted?.error) throw new Error('provider_rejected');
    await db.prepare("UPDATE stock_requests SET status='sent',email_sent_at=?,last_error=NULL WHERE id=? AND status='sending'").bind(Date.now(), row.id).run();
  } catch (error) {
    const code = ['configuration_unavailable', 'provider_timeout', 'provider_rejected'].includes(error?.message) ? error.message : 'provider_error';
    await db.prepare("UPDATE stock_requests SET status='pending',last_error=? WHERE id=? AND status='sending'").bind(code, row.id).run();
  } finally { if (timer !== undefined) clearTimeout(timer); }
}

/** Private-route authentication is performed by project-api before this call. */
export async function handleStockRequests(request, env, { project }) {
  try {
    if (!Object.prototype.hasOwnProperty.call(PROJECTS, project)) throw new RequestError(404, 'Projekt nicht gefunden.');
    if (request.method !== 'POST') throw new RequestError(405, 'Diese Methode ist nicht erlaubt.', { Allow: 'POST' });
    if (request.headers.get('Origin') !== new URL(request.url).origin || request.headers.get('Sec-Fetch-Site') === 'cross-site') throw new RequestError(403, 'Diese Anfrage ist nicht erlaubt.');
    const input = await readBody(request);
    await ensureSchema(env.DB);
    let row = await env.DB.prepare('SELECT * FROM stock_requests WHERE id=?').bind(input.requestId).first();
    if (row) {
      if (!sameRequest(row, project, input)) throw new RequestError(409, 'Bitte eine neue Anfrage öffnen.');
      // Recover only a crash before the first dispatch claim. Once an attempt
      // was recorded, every uncertain/error result remains non-retryable here.
      if (!row.duplicate_of && row.status === 'pending' && row.email_attempted_at === null) {
        await sendOnce(env.DB, env, row);
        row = await env.DB.prepare('SELECT * FROM stock_requests WHERE id=?').bind(input.requestId).first();
      }
      return await resultForRow(env.DB, row, input, false);
    }
    const now = Date.now(), day = new Date(now).toISOString().slice(0, 10), hashedIp = await ipHash(request, env), admission = crypto.randomUUID();
    // The marker unique to this invocation ensures even simultaneous requests
    // with the same UUID can increment each persistent quota only once. The
    // duplicate lookup, insertion and quota increments share one transaction.
    const statements = [
      env.DB.prepare("INSERT OR IGNORE INTO stock_request_daily_limits(kind,limit_key,day,requests) VALUES('ip',?,?,0)").bind(hashedIp, day),
      env.DB.prepare("INSERT OR IGNORE INTO stock_request_daily_limits(kind,limit_key,day,requests) VALUES('global','all',?,0)").bind(day),
      env.DB.prepare("INSERT INTO stock_requests(id,project,name,normalized_name,ticker,ip_hash,created_at,admission_token,duplicate_of) SELECT ?,?,?,?,?,?,?,?,(SELECT id FROM stock_requests WHERE normalized_name=? AND created_at>? AND duplicate_of IS NULL ORDER BY created_at DESC LIMIT 1) WHERE (SELECT requests FROM stock_request_daily_limits WHERE kind='ip' AND limit_key=? AND day=?)<? AND (SELECT requests FROM stock_request_daily_limits WHERE kind='global' AND limit_key='all' AND day=?)<? ON CONFLICT(id) DO NOTHING").bind(input.requestId, project, input.name, input.normalizedName, input.ticker, hashedIp, now, admission, input.normalizedName, now - DAY_MS, hashedIp, day, MAX_PER_IP_DAY, day, MAX_GLOBAL_DAY),
      env.DB.prepare("UPDATE stock_request_daily_limits SET requests=requests+1 WHERE kind='ip' AND limit_key=? AND day=? AND EXISTS(SELECT 1 FROM stock_requests WHERE admission_token=?)").bind(hashedIp, day, admission),
      env.DB.prepare("UPDATE stock_request_daily_limits SET requests=requests+1 WHERE kind='global' AND limit_key='all' AND day=? AND EXISTS(SELECT 1 FROM stock_requests WHERE admission_token=?)").bind(day, admission)
    ];
    await env.DB.batch(statements);
    row = await env.DB.prepare('SELECT * FROM stock_requests WHERE id=?').bind(input.requestId).first();
    if (!row) throw new RequestError(429, 'Anfragelimit erreicht. Bitte morgen erneut versuchen.', { 'Retry-After': String(Math.max(1, Math.ceil((Date.parse(day + 'T00:00:00Z') + DAY_MS - now) / 1000))) });
    if (!sameRequest(row, project, input)) throw new RequestError(409, 'Bitte eine neue Anfrage öffnen.');
    const isNew = row.admission_token === admission;
    if (isNew && !row.duplicate_of) {
      await sendOnce(env.DB, env, row);
      row = await env.DB.prepare('SELECT * FROM stock_requests WHERE id=?').bind(input.requestId).first();
    }
    return await resultForRow(env.DB, row, input, isNew);
  } catch (error) {
    if (error instanceof RequestError) return response({ error: error.message }, error.status, error.headers);
    return response({ error: 'Anfrage ist gerade nicht verfügbar. Bitte erneut versuchen.' }, 503);
  }
}
