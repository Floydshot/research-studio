/** Cloudflare Cron orchestration only; market-data computation stays in Actions. */
const MINUTE = 60_000;
const MAX_ATTEMPTS = 3;
const LEASE_MS = 2 * MINUTE;
const DISPATCH_GRACE_MS = 10 * MINUTE;
const PUBLICATION_GRACE_MS = 5 * MINUTE;
const CLOCK_SKEW_MS = 5 * MINUTE;
const REPOSITORY = 'Floydshot/research-studio';
const WORKFLOW = 'daily_update.yml';
const PAGES = 'https://floydshot.github.io/research-studio';
const API = `https://api.github.com/repos/${REPOSITORY}/actions/workflows/${WORKFLOW}`;
const schemaPromises = new WeakMap();
const berlinFormatter = new Intl.DateTimeFormat('en-CA', {
  timeZone: 'Europe/Berlin', year: 'numeric', month: '2-digit', day: '2-digit',
  hour: '2-digit', minute: '2-digit', hourCycle: 'h23'
});

function localParts(timestamp) {
  return Object.fromEntries(berlinFormatter.formatToParts(timestamp)
    .filter(part => part.type !== 'literal').map(part => [part.type, part.value]));
}

/** Includes the 08:25 check, in both CET and CEST; never starts before 07:30. */
export function berlinRefreshWindow(timestamp) {
  if (!Number.isFinite(timestamp)) throw new TypeError('Invalid scheduler timestamp');
  const parts = localParts(timestamp), day = `${parts.year}-${parts.month}-${parts.day}`;
  const base = Date.UTC(Number(parts.year), Number(parts.month) - 1, Number(parts.day));
  const summerCandidate = base + (5 * 60 + 30) * MINUTE;
  const dueAt = localParts(summerCandidate).hour === '07' ? summerCandidate : base + (6 * 60 + 30) * MINUTE;
  const windowEndsAt = dueAt + 56 * MINUTE;
  return { day, dueAt, windowEndsAt, inWindow: timestamp >= dueAt && timestamp < windowEndsAt };
}

/** A partial stock batch is valid when market quotes, CNN and ECB were fetched. */
export function refreshBatchIsCurrent(payload, window, now) {
  const batch = payload?.refresh_batch;
  if (!batch || batch.version !== 1 || !['live', 'partial'].includes(batch.status) || batch.id !== batch.started_at) return false;
  const started = Date.parse(batch.started_at), completed = Date.parse(batch.completed_at);
  if (!Number.isFinite(started) || !Number.isFinite(completed) || started < window.dueAt || completed < started || completed > now + CLOCK_SKEW_MS) return false;
  const { yahoo, cnn, ecb } = batch.providers || {};
  if (!yahoo || !['live', 'partial'].includes(yahoo.status) || !Number.isSafeInteger(yahoo.complete_stock_count) || yahoo.complete_stock_count < 1) return false;
  return [cnn, ecb].every(provider => {
    const retrieved = Date.parse(provider?.retrieved_at);
    return provider?.status === 'live' && Number.isFinite(retrieved) && retrieved >= started && retrieved <= completed;
  });
}

function configurationError(code) {
  const error = new Error(code);
  error.name = 'RefreshSchedulerConfigurationError';
  error.code = code;
  return error;
}

async function tokenFrom(env) {
  let token;
  try {
    const binding = env.SECRET_GITHUB_REFRESH_TOKEN;
    token = binding !== undefined ? await binding?.get?.() : env.GITHUB_REFRESH_TOKEN;
  } catch (_) { throw configurationError('refresh_scheduler_github_token_unavailable'); }
  if (typeof token !== 'string' || token.trim().length < 20 || token.length > 2000 || /[\r\n]/.test(token)) throw configurationError('refresh_scheduler_missing_github_token');
  return token.trim();
}

async function ensureSchema(db) {
  if (!db || typeof db.prepare !== 'function') throw configurationError('refresh_scheduler_storage_unavailable');
  if (!schemaPromises.has(db)) {
    const promise = db.prepare(`CREATE TABLE IF NOT EXISTS refresh_scheduler_days (
      day TEXT PRIMARY KEY, attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts BETWEEN 0 AND 3),
      lease_token TEXT, lease_until INTEGER NOT NULL DEFAULT 0, request_id TEXT, request_at INTEGER,
      run_id INTEGER, outcome TEXT NOT NULL DEFAULT 'new', updated_at INTEGER NOT NULL, last_error TEXT
    )`).run().catch(error => { schemaPromises.delete(db); throw error; });
    schemaPromises.set(db, promise);
  }
  await schemaPromises.get(db);
}

async function requestJson(fetchFn, url, options = {}, limit = 1_000_000) {
  const controller = new AbortController(), timer = setTimeout(() => controller.abort(), 8_000);
  try {
    const response = await fetchFn(url, { ...options, signal: controller.signal });
    if (!response.ok || response.status === 204) return { status: response.status, next: false, data: null };
    const text = await response.text();
    if (text.length > limit) throw new Error('response_too_large');
    let data;
    try { data = text ? JSON.parse(text) : null; }
    catch (_) { throw new Error('invalid_json'); }
    return { status: response.status, next: /rel="next"/.test(response.headers?.get?.('Link') || ''), data };
  } catch (error) {
    const safe = new Error(controller.signal.aborted ? 'upstream_timeout' : ['response_too_large', 'invalid_json'].includes(error?.message) ? error.message : 'upstream_network_error');
    safe.code = safe.message;
    throw safe;
  } finally { clearTimeout(timer); }
}

async function readPublishedBatch(fetchFn, now) {
  const options = { headers: { Accept: 'application/json', 'Cache-Control': 'no-cache', 'User-Agent': 'Research-Studio-Refresh-Scheduler' }, cache: 'no-store', cf: { cacheTtl: 0 } };
  let result = await requestJson(fetchFn, `${PAGES}/refresh-status.json?_refresh=${now}`, options, 64_000);
  if (result.status === 404) result = await requestJson(fetchFn, `${PAGES}/data.json?_refresh=${now}`, options, 4_000_000);
  return result.status === 200 ? result.data : null;
}

function githubHeaders(token) {
  return { Accept: 'application/vnd.github+json', Authorization: `Bearer ${token}`, 'User-Agent': 'Research-Studio-Refresh-Scheduler', 'X-GitHub-Api-Version': '2026-03-10' };
}

async function listRuns(fetchFn, token, day) {
  const all = [];
  for (let page = 1; page <= 3; page++) {
    const result = await requestJson(fetchFn, `${API}/runs?per_page=100&created=${day}&page=${page}`, { headers: githubHeaders(token), cache: 'no-store' });
    if (result.status === 401 || result.status === 403) throw configurationError('refresh_scheduler_github_permission_denied');
    if (result.status !== 200 || !Array.isArray(result.data?.workflow_runs)) throw new Error('github_runs_unavailable');
    all.push(...result.data.workflow_runs);
    if (!result.next) return all;
  }
  throw new Error('github_run_list_truncated');
}

function safeRunId(value) { return Number.isSafeInteger(value) && value > 0 ? value : null; }
function timestamp(value) { const parsed = Date.parse(value); return Number.isFinite(parsed) ? parsed : null; }
function result(window, outcome, extra = {}) { return { day: window.day, due_at: new Date(window.dueAt).toISOString(), outcome, ...extra }; }

/**
 * No HTTP endpoint is exposed here. Call only from the Worker's scheduled handler.
 * fetchFn/now/randomUUID are injectable for tests without credentials or network.
 */
export async function runRefreshScheduler(controller, env, dependencies = {}) {
  const now = (dependencies.now || Date.now)(), window = berlinRefreshWindow(now);
  if (!window.inWindow) return result(window, 'outside_window');
  if (Number.isFinite(controller?.scheduledTime) && berlinRefreshWindow(controller.scheduledTime).day !== window.day) return result(window, 'old_scheduled_event');
  const token = await tokenFrom(env), db = env.DB, fetchFn = dependencies.fetchFn || globalThis.fetch;
  const randomUUID = dependencies.randomUUID || (() => crypto.randomUUID());
  await ensureSchema(db);
  const previous = await db.prepare('SELECT * FROM refresh_scheduler_days WHERE day=?').bind(window.day).first();
  if (previous?.outcome === 'complete') return result(window, 'already_complete', { attempts: previous.attempts });

  let published = null;
  try { published = await readPublishedBatch(fetchFn, now); }
  catch (_) { /* A missing feed may be repaired by the next bounded refresh. */ }
  if (refreshBatchIsCurrent(published, window, now)) {
    await db.prepare(`INSERT INTO refresh_scheduler_days(day,outcome,updated_at) VALUES(?,'complete',?)
      ON CONFLICT(day) DO UPDATE SET outcome='complete',lease_token=NULL,lease_until=0,updated_at=excluded.updated_at,last_error=NULL`).bind(window.day, now).run();
    return result(window, 'complete', { attempts: previous?.attempts || 0 });
  }

  await db.prepare("INSERT OR IGNORE INTO refresh_scheduler_days(day,updated_at) VALUES(?,?)").bind(window.day, now).run();
  const lease = randomUUID();
  let row = await db.prepare(`UPDATE refresh_scheduler_days SET lease_token=?,lease_until=?,updated_at=?
    WHERE day=? AND lease_until<=? AND outcome<>'complete' RETURNING *`).bind(lease, now + LEASE_MS, now, window.day, now).first();
  if (!row) {
    const current = await db.prepare('SELECT outcome,attempts FROM refresh_scheduler_days WHERE day=?').bind(window.day).first();
    return result(window, current?.outcome === 'complete' ? 'already_complete' : 'lease_busy', { attempts: current?.attempts || 0 });
  }

  const release = async (outcome, lastError = null, runId = row.run_id) => {
    await db.prepare(`UPDATE refresh_scheduler_days SET outcome=?,last_error=?,run_id=?,lease_token=NULL,lease_until=0,updated_at=?
      WHERE day=? AND lease_token=?`).bind(outcome, lastError, runId, now, window.day, lease).run();
  };
  try {
    const runs = await listRuns(fetchFn, token, window.day);
    const active = runs.find(run => run.event !== 'push' && run.status !== 'completed' && timestamp(run.created_at) !== null && timestamp(run.created_at) >= window.dueAt - 60 * MINUTE);
    if (active) {
      await release('running', null, safeRunId(active.id));
      return result(window, 'running', { attempts: row.attempts, run_id: safeRunId(active.id) });
    }

    const marker = row.request_id ? `refresh-${window.day}-${row.request_id}` : null;
    const ours = marker ? runs.filter(run => run.display_title === marker && timestamp(run.created_at) >= (row.request_at || 0) - MINUTE) : [];
    ours.sort((a, b) => (timestamp(b.created_at) || 0) - (timestamp(a.created_at) || 0));
    const latest = ours[0], failed = latest?.status === 'completed' && latest.conclusion !== 'success';
    if (latest?.status === 'completed' && latest.conclusion === 'success' && timestamp(latest.updated_at) !== null && now - timestamp(latest.updated_at) < PUBLICATION_GRACE_MS) {
      await release('awaiting_publication', null, safeRunId(latest.id));
      return result(window, 'awaiting_publication', { attempts: row.attempts, run_id: safeRunId(latest.id) });
    }
    if (row.request_at !== null && !failed && row.outcome !== 'dispatch_rejected' && now - row.request_at < DISPATCH_GRACE_MS) {
      await release('awaiting_registration');
      return result(window, 'awaiting_registration', { attempts: row.attempts });
    }
    if (row.attempts >= MAX_ATTEMPTS) {
      await release('attempts_exhausted', 'refresh_not_published');
      return result(window, 'attempts_exhausted', { attempts: row.attempts });
    }

    const requestId = randomUUID();
    row = await db.prepare(`UPDATE refresh_scheduler_days SET attempts=attempts+1,request_id=?,request_at=?,run_id=NULL,outcome='dispatching',updated_at=?
      WHERE day=? AND lease_token=? AND outcome<>'complete' AND attempts<? RETURNING *`).bind(requestId, now, now, window.day, lease, MAX_ATTEMPTS).first();
    if (!row) return result(window, 'lease_lost');
    let dispatch;
    try {
      dispatch = await requestJson(fetchFn, `${API}/dispatches`, {
        method: 'POST', headers: { ...githubHeaders(token), 'Content-Type': 'application/json' },
        body: JSON.stringify({ ref: 'main', inputs: { scheduler_day: window.day, request_id: requestId } })
      }, 32_000);
    } catch (_) {
      await release('dispatch_uncertain', 'dispatch_response_unconfirmed', null);
      return result(window, 'dispatch_uncertain', { attempts: row.attempts, request_id: requestId });
    }
    if (![200, 204].includes(dispatch.status)) {
      await release('dispatch_rejected', `github_http_${dispatch.status}`, null);
      if ([401, 403].includes(dispatch.status)) throw configurationError('refresh_scheduler_github_permission_denied');
      return result(window, 'dispatch_rejected', { attempts: row.attempts, http_status: dispatch.status });
    }
    const runId = safeRunId(dispatch.data?.workflow_run_id);
    await release('dispatched', null, runId);
    return result(window, 'dispatched', { attempts: row.attempts, request_id: requestId, run_id: runId });
  } catch (error) {
    const code = error?.name === 'RefreshSchedulerConfigurationError' ? error.code : ['github_run_list_truncated'].includes(error?.message) ? error.message : 'github_runs_unavailable';
    await release('check_failed', code);
    if (error?.name === 'RefreshSchedulerConfigurationError') throw error;
    return result(window, 'check_failed', { attempts: row.attempts, code });
  } finally {
    await db.prepare('UPDATE refresh_scheduler_days SET lease_token=NULL,lease_until=0 WHERE day=? AND lease_token=?').bind(window.day, lease).run();
  }
}
