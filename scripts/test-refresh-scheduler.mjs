import assert from 'node:assert/strict';
import { DatabaseSync } from 'node:sqlite';
import { berlinRefreshWindow, refreshBatchIsCurrent, runRefreshScheduler } from '../server/refresh-scheduler.mjs';

let checks = 0;
const eq = (actual, expected, note) => { assert.deepEqual(actual, expected, note); checks++; };
const yes = (value, note) => { assert.ok(value, note); checks++; };
const TOKEN = 'test_scheduler_token_not_a_real_credential';
const winterDue = Date.parse('2026-11-24T06:30:00Z');
const minute = 60_000;

class D1 {
  constructor() { this.sqlite = new DatabaseSync(':memory:'); this.creates = 0; }
  prepare(sql) {
    if (sql.startsWith('CREATE TABLE')) this.creates++;
    const statement = this.sqlite.prepare(sql);
    return {
      bind: (...values) => ({
        first: async () => statement.get(...values) || null,
        run: async () => statement.run(...values)
      }),
      run: async () => statement.run()
    };
  }
  row() { return this.sqlite.prepare('SELECT * FROM refresh_scheduler_days').get(); }
}

function batch(due = winterDue) {
  const started = new Date(due + 5_000).toISOString();
  const retrieved = new Date(due + 30_000).toISOString();
  return { refresh_batch: {
    version: 1, id: started, started_at: started, completed_at: new Date(due + minute).toISOString(), status: 'live',
    stock_counts: { live: 54 },
    providers: {
      yahoo: { status: 'live', complete_stock_count: 54, stock_count: 54 },
      cnn: { status: 'live', retrieved_at: retrieved }, ecb: { status: 'live', retrieved_at: retrieved }
    }
  } };
}

function fixture(options = {}) {
  const clock = { value: options.now ?? winterDue }, db = options.db || new D1();
  const state = { payload: options.payload || batch(winterDue - 86400000), runs: [], posts: [], requests: [], dispatchStatus: 204, ...options };
  let uuidCounter = 0;
  const dependencies = {
    now: () => clock.value,
    randomUUID: () => `00000000-0000-4000-8000-${String(++uuidCounter).padStart(12, '0')}`,
    fetchFn: async (url, request) => {
      const target = new URL(url);
      state.requests.push({ url, method: request.method || 'GET' });
      if (target.origin === 'https://floydshot.github.io') {
        eq(request.headers.Authorization, undefined, 'GitHub token never sent to public data host');
        if (target.pathname.endsWith('refresh-status.json') && state.sidecarMissing) return new Response(null, { status: 404 });
        return new Response(JSON.stringify(state.payload), { status: state.feedStatus || 200 });
      }
      eq(target.origin, 'https://api.github.com', 'credential destination is fixed');
      eq(request.headers.Authorization, `Bearer ${TOKEN}`);
      if (target.pathname.endsWith('/runs')) {
        if (state.slowRuns) await new Promise(resolve => setTimeout(resolve, 10));
        const page = Number(target.searchParams.get('page'));
        return new Response(JSON.stringify(state.malformedRuns ? {} : { workflow_runs: state.runs }), {
          status: state.listStatus || 200,
          headers: state.morePages ? { Link: '<https://api.github.com/ignored>; rel="next"' } : {}
        });
      }
      yes(target.pathname.endsWith('/dispatches'), 'only workflow dispatch write allowed');
      state.posts.push(JSON.parse(request.body));
      if (state.dispatchThrows) throw new Error('sensitive upstream content must not be logged');
      return new Response(state.dispatchStatus === 204 ? null : JSON.stringify({ workflow_run_id: 12345 }), { status: state.dispatchStatus });
    }
  };
  return { clock, db, state, run: (controller = {}) => runRefreshScheduler(controller, { DB: db, GITHUB_REFRESH_TOKEN: TOKEN }, dependencies), dependencies };
}

// Both clock changes retain 07:30 local, including the exact transition Sundays.
for (const [date, due] of [
  ['2026-03-28', '06:30'], ['2026-03-29', '05:30'],
  ['2026-10-24', '05:30'], ['2026-10-25', '06:30'], ['2026-11-24', '06:30']
]) {
  const at = Date.parse(`${date}T${due}:00Z`), window = berlinRefreshWindow(at);
  eq(window.day, date); eq(window.dueAt, at); eq(window.inWindow, true);
  eq(berlinRefreshWindow(at - 1).inWindow, false, 'never early');
  eq(berlinRefreshWindow(at + 55 * minute + 59_999).inWindow, true, '08:25 check accepted');
  eq(berlinRefreshWindow(at + 56 * minute).inWindow, false, 'no late uncontrolled dispatch');
}

const fresh = batch(), window = berlinRefreshWindow(winterDue);
eq(refreshBatchIsCurrent(fresh, window, winterDue + 2 * minute), true, 'all securities and shared providers are current');
const partial = structuredClone(fresh);
partial.refresh_batch.status = 'partial';
partial.refresh_batch.stock_counts = { live: 34, partial: 20 };
Object.assign(partial.refresh_batch.providers.yahoo, { status: 'partial', complete_stock_count: 34 });
eq(refreshBatchIsCurrent(partial, window, winterDue + 2 * minute), false, 'honest partial stock batch must be repaired');
const completeWithZeroes = structuredClone(fresh);
completeWithZeroes.refresh_batch.stock_counts = { live: 54, partial: 0, stale: 0, initial: 0 };
eq(refreshBatchIsCurrent(completeWithZeroes, window, winterDue + 2 * minute), true, 'explicit zero gaps are complete');
for (const mutate of [
  value => { value.refresh_batch.status = 'partial'; },
  value => { value.refresh_batch.status = 'stale'; },
  value => { value.refresh_batch.id = 'wrong'; },
  value => { value.refresh_batch.started_at = new Date(winterDue - minute).toISOString(); value.refresh_batch.id = value.refresh_batch.started_at; },
  value => { value.refresh_batch.providers.yahoo.complete_stock_count = 0; },
  value => { value.refresh_batch.providers.yahoo.complete_stock_count = '52'; },
  value => { value.refresh_batch.providers.yahoo.complete_stock_count = true; },
  value => { value.refresh_batch.providers.yahoo.status = 'partial'; },
  value => { delete value.refresh_batch.providers.yahoo.stock_count; },
  value => { value.refresh_batch.providers.yahoo.stock_count = 0; value.refresh_batch.providers.yahoo.complete_stock_count = 0; },
  value => { value.refresh_batch.providers.yahoo.stock_count = '54'; },
  value => { value.refresh_batch.providers.yahoo.stock_count = true; },
  value => { value.refresh_batch.providers.yahoo.stock_count = 53; },
  value => { value.refresh_batch.providers.yahoo.stock_count = Number.MAX_SAFE_INTEGER + 1; value.refresh_batch.providers.yahoo.complete_stock_count = Number.MAX_SAFE_INTEGER + 1; },
  value => { delete value.refresh_batch.stock_counts; },
  value => { value.refresh_batch.stock_counts = null; },
  value => { value.refresh_batch.stock_counts = []; },
  value => { value.refresh_batch.stock_counts = {}; },
  value => { value.refresh_batch.stock_counts.live = '54'; },
  value => { value.refresh_batch.stock_counts.live = true; },
  value => { value.refresh_batch.stock_counts.live = 53; },
  value => { value.refresh_batch.stock_counts.partial = 1; },
  value => { value.refresh_batch.stock_counts.stale = 1; },
  value => { value.refresh_batch.stock_counts.initial = 1; },
  value => { value.refresh_batch.stock_counts.partial = -1; },
  value => { value.refresh_batch.stock_counts.partial = 0.5; },
  value => { value.refresh_batch.stock_counts.partial = false; },
  value => { value.refresh_batch.stock_counts.unknown = 0; },
  value => { value.refresh_batch.providers.cnn.status = 'previous'; },
  value => { value.refresh_batch.providers.ecb.retrieved_at = new Date(winterDue - minute).toISOString(); },
  value => { value.refresh_batch.completed_at = new Date(winterDue + 20 * minute).toISOString(); }
]) {
  const invalid = structuredClone(fresh); mutate(invalid);
  eq(refreshBatchIsCurrent(invalid, window, winterDue + 2 * minute), false);
}

let f = fixture({ now: winterDue - 1 });
eq((await f.run()).outcome, 'outside_window'); eq(f.db.creates, 0); eq(f.state.requests.length, 0);
f = fixture();
eq((await f.run({ scheduledTime: winterDue - 86400000 })).outcome, 'old_scheduled_event'); eq(f.state.requests.length, 0);
await assert.rejects(() => runRefreshScheduler({}, { DB: f.db }, f.dependencies), /missing_github_token/); checks++;
eq(f.state.requests.length, 0, 'missing credentials cause no network transmission');

f = fixture({ now: winterDue + 2 * minute, payload: fresh });
eq((await f.run()).outcome, 'complete'); eq(f.state.posts.length, 0); eq(f.db.row().outcome, 'complete');
const count = f.state.requests.length;
eq((await f.run()).outcome, 'already_complete'); eq(f.state.requests.length, count, 'complete day makes no further network calls'); eq(f.db.creates, 1, 'schema initialized once per binding');

f = fixture({ now: winterDue + 2 * minute, payload: fresh, sidecarMissing: true });
eq((await f.run()).outcome, 'complete'); eq(f.state.requests.length, 2, 'rollout supports old Pages without sidecar');

f = fixture({ now: winterDue + 2 * minute, payload: partial });
eq((await f.run()).outcome, 'dispatched', 'partial publication starts a bounded repair attempt');
eq(f.state.posts.length, 1); eq(f.db.row().outcome, 'dispatched');
f.clock.value += 10 * minute;
eq((await f.run()).attempts, 2, 'still-partial publication remains eligible for the next repair');
eq(f.state.posts.length, 2);
f.state.payload = fresh;
eq((await f.run()).outcome, 'complete', 'repair stops after full publication');
eq(f.state.posts.length, 2);

// Both generations of the dispatch API are accepted, with least-privilege inputs.
for (const dispatchStatus of [200, 204]) {
  f = fixture({ dispatchStatus });
  const outcome = await f.run();
  eq(outcome.outcome, 'dispatched'); eq(outcome.attempts, 1);
  eq(outcome.run_id, dispatchStatus === 200 ? 12345 : null);
  eq(f.state.posts[0].ref, 'main'); eq(f.state.posts[0].inputs.scheduler_day, '2026-11-24');
  yes(/^[0-9a-f-]{36}$/.test(f.state.posts[0].inputs.request_id));
  eq(f.db.row().outcome, 'dispatched', 'dispatch is never labelled refresh complete');
}

f = fixture();
await f.run();
f.clock.value += 5 * minute;
eq((await f.run()).outcome, 'awaiting_registration'); eq(f.state.posts.length, 1, 'no immediate duplicate when accepted run is not visible yet');
f.clock.value += 5 * minute;
eq((await f.run()).attempts, 2);
f.clock.value += 10 * minute;
eq((await f.run()).attempts, 3);
f.clock.value += 10 * minute;
eq((await f.run()).outcome, 'attempts_exhausted'); eq(f.state.posts.length, 3, 'hard bound on daily dispatch attempts');
f.state.payload = batch(winterDue + 29 * minute); f.clock.value += 2 * minute;
eq((await f.run()).outcome, 'complete', 'third attempt can still complete after budget exhausted');

f = fixture();
await f.run();
const requestId = f.state.posts[0].inputs.request_id;
f.clock.value += 20 * minute;
f.state.runs = [{ id: 777, created_at: new Date(winterDue).toISOString(), status: 'queued', display_title: `refresh-2026-11-24-${requestId}` }];
eq((await f.run()).outcome, 'running'); eq(f.state.posts.length, 1, 'queued run suppresses duplicates beyond grace');
f.state.runs[0] = { ...f.state.runs[0], status: 'completed', conclusion: 'failure', updated_at: new Date(f.clock.value).toISOString() };
eq((await f.run()).outcome, 'dispatched'); eq(f.state.posts.length, 2, 'confirmed failure may retry');

f = fixture({ runs: [{ id: 778, event: 'push', status: 'in_progress', created_at: new Date(winterDue).toISOString() }] });
eq((await f.run()).outcome, 'dispatched'); eq(f.state.posts.length, 1, 'UI-only push deployment does not block market dispatch');
f = fixture({ runs: [{ id: 779, event: 'workflow_dispatch', status: 'queued', created_at: new Date(winterDue).toISOString() }] });
eq((await f.run()).outcome, 'running'); eq(f.state.posts.length, 0, 'actual market dispatch suppresses duplicates');

f = fixture();
await f.run();
f.clock.value += 5 * minute;
f.state.runs = [{ id: 888, created_at: new Date(winterDue).toISOString(), updated_at: new Date(f.clock.value).toISOString(), status: 'completed', conclusion: 'success', display_title: `refresh-2026-11-24-${f.state.posts[0].inputs.request_id}` }];
eq((await f.run()).outcome, 'awaiting_publication'); eq(f.state.posts.length, 1);
f.state.payload = batch();
eq((await f.run()).outcome, 'complete');

f = fixture({ dispatchThrows: true });
eq((await f.run()).outcome, 'dispatch_uncertain');
f.clock.value += 5 * minute;
eq((await f.run()).outcome, 'awaiting_registration'); eq(f.state.posts.length, 1, 'ambiguous network failure is not immediately retried');

f = fixture({ malformedRuns: true });
eq((await f.run()).outcome, 'check_failed'); eq(f.state.posts.length, 0); eq(f.db.row().attempts, 0);
f = fixture({ morePages: true });
eq((await f.run()).code, 'github_run_list_truncated'); eq(f.state.posts.length, 0, 'unknown active runs fail closed');
f = fixture({ listStatus: 403 });
await assert.rejects(() => f.run(), /permission_denied/); checks++; eq(f.state.posts.length, 0);
f = fixture({ dispatchStatus: 401 });
await assert.rejects(() => f.run(), /permission_denied/); checks++; eq(f.db.row().attempts, 1); eq(f.db.row().lease_token, null);

f = fixture({ slowRuns: true });
const parallel = await Promise.all([f.run(), f.run()]);
eq(f.state.posts.length, 1, 'SQLite atomic lease protects overlapping Cron events');
yes(parallel.some(item => item.outcome === 'lease_busy')); eq(f.db.row().attempts, 1);

// A fresh publisher can invalidate an existing lease before its dispatch CAS.
f = fixture({ now: winterDue + 2 * minute });
const originalFetch = f.dependencies.fetchFn;
let unblock, observedRuns;
const reachedRuns = new Promise(resolve => { observedRuns = resolve; });
const pauseRuns = new Promise(resolve => { unblock = resolve; });
f.dependencies.fetchFn = async (url, options) => {
  if (new URL(url).pathname.endsWith('/runs')) { observedRuns(); await pauseRuns; }
  return originalFetch(url, options);
};
const inFlight = f.run();
await reachedRuns;
f.state.payload = fresh;
eq((await f.run()).outcome, 'complete');
unblock();
eq((await inFlight).outcome, 'lease_lost'); eq(f.state.posts.length, 0); eq(f.db.row().outcome, 'complete');

f = fixture({ feedStatus: 503 });
eq((await f.run()).outcome, 'dispatched', 'one bounded refresh can repair an unreachable feed');

console.log(`PASS ${checks} scheduler checks: DST, full-provider freshness, real SQLite leases, retries, ambiguous dispatches, no private state or credentials used.`);
