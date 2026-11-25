# Research Studio

The public Open Projects board contains Tech Stonks and Value Stonks. Floyd Projects and TS Projects use separate authenticated workspaces and add a combined Overview. A24 remains in the researched data/catalog but is not a navigation tab.

## Runtime

`index.html` and `client/` are the browser application. `server/project-api.mjs` handles passwords, sessions and versioned private state in Cloudflare D1. The private `ProjectPasswordVerifier` Durable Object performs PBKDF2 verification through RPC so password hashing fits within the Workers Free CPU budget without reducing its iteration count. `scripts/build-worker.mjs` embeds the public assets and API into `dist/worker.mjs`. Only the public market-data feed is fetched from the existing GitHub Pages deployment; its daily workflow continues to run independently.

Run `npm run build`, then deploy the built Worker using Wrangler. Bind the project database as `DB`. The deployment account and actual D1 database ID belong in `wrangler.jsonc` after provisioning; neither is a password. Runtime secrets must never be checked into source or static assets:

- `FLOYD_PASSWORD_HASH` and `TS_PASSWORD_HASH`: `pbkdf2-sha256$100000$<16-byte-or-longer random salt in hex>$<32-byte PBKDF2-SHA256 result in hex>`.
- `SESSION_SECRET`: at least 32 random characters, used to pseudonymize rate-limit keys.

Provision secrets through a protected JSON file or stdin to Wrangler. Do not pass the password hashes through Dotenv or shell interpolation: they contain literal dollar signs. Keep secrets outside this checkout and delete temporary deployment files after provisioning. Normal code deployments preserve existing secrets. Changing a profile password hash invalidates that profile's older sessions.

## Private state

Each private workspace has independent Tech/Value scenario inputs and one shared Outlook horizon for Tech, Value and Overview. The horizon is mirrored into the existing depot and Overview settings when changed; older saved settings use the first valid Overview, Tech or Value horizon in that order. Authentication is enforced on the server for every read and write. Session cookies are HttpOnly, Secure, SameSite=Strict and scoped to the selected profile API path. Login attempts are limited to five per fifteen minutes per profile and pseudonymized source IP. Responses containing private settings are not cached.

Updates use `If-Match` revisions; stale updates return HTTP 409 instead of overwriting another device. Only the public sandbox uses browser-local scenario storage. No existing browser-local personal input is silently copied into the public sandbox or uploaded to a private workspace.

Portfolio membership is separate from scenario inclusion: `removed` hides a position in that project's Shopping and Outlook views, while `excluded` only greys it out of a projection. Confirmed removals retain the researched catalog entry and saved quantities, so the catalog search can restore them. The Open sandbox stores its own membership locally; private membership uses the existing authenticated, versioned state API. Old imports without membership data preserve the current project's removals.

Each Outlook card can override its annual gross total-return assumption through `returnOverrides[ticker]` (−99 to 1000 percent). A missing ticker inherits the selected manual or historical basis; `null` is an unfinished input, and resetting a card removes its key. Explicit typing pins a card rate even when it equals the inherited basis; focusing an unchanged field does not. Overrides are separate for each project and depot and feed both its projection and the combined Overview. Older clients/imports that omit the map preserve saved overrides; an explicit empty map clears them. The published historical returns and dividend-growth observations remain source data, independent of these scenario assumptions.

Both Tech and Value expose `Allocation % / Shares` as direct input-mode buttons. Switching modes preserves both numeric maps; share quantities and EUR-converted quote values determine the effective allocation in Shares mode. Card labels show `% growth p.a.` and `% allocation`. Tech's `Wachstum pro Jahr` KPI is the normalized start-allocation-weighted mean of the effective per-card annual assumptions, including inherited rates only where no own rate exists. Value retains its year-end net-dividend KPI.

The one-time `project-membership-2026-11-21-v1` database migration runs on the first authenticated state read/write. It removes NET/NOW from TS Tech and AVGO/TSM from Floyd Tech, and leaves Allianz, AXA, Munich Re, Hannover Re and DBS in Floyd Value. It retains numeric inputs and dormant shares/weights, and records a separate atomic migration marker so later explicit restorations are respected. Older clients that omit `removed` preserve the stored map.

The old GitHub Pages address redirects to the Cloudflare application. `legacy.html` retains the previous browser-local view and can export Tech/Value inputs as a JSON file. The import action in the unlocked Floyd Projects menu validates and previews that file, and saves it only after explicit confirmation. A24 is excluded from this migration.

The Overview sums both independent monthly projections for the shared selected horizon. Changing the horizon in any Outlook view updates all three views while preserving each depot's capital, monthly purchases, saved exclusions and extra positions. Tech uses the selected total-return basis directly for growth in every workspace and has no separate dividend model. Overview dividends and the reinvestment control come exclusively from Value Stonks. Its charts follow the individual depot presentation: consolidated portfolio value and annual net distributions by the origin of Value's invested capital. Historical rates remain assumptions rather than predictions.

## Custom depots

Private Floyd and TS workspaces can create up to 20 named custom depots with `+ New Depot`. Each depot has an immutable UUID, a growth/dividend type, and its own scenario, positions, shares, allocations and rate overrides. New depots start with zero capital and no positions; the existing catalog search and Portfolio input add researched securities. Custom definitions stay in the private project state, never in the public market-data feed. Open Projects retains its shared preset views.

The `Add to Overview` switch applies to each built-in or custom depot and defaults to enabled. Overview sums only included depots, including their independent savings and dividend reinvestment choices; growth depots contribute no separate dividend payouts. Zero-funded empty depots add zero, while funded depots without positions require correction when included. The selected horizon is shared across every depot and Overview. Private state uses the existing authenticated revision/CAS storage across devices; older clients and legacy imports preserve custom collections and omitted inclusion settings.

Custom depot checks: `node scripts/test-custom-depots-api.mjs`, `node scripts/test-custom-depots-overview.cjs`, `node scripts/test-custom-depots-dialog.cjs`, and `node scripts/test-custom-depots-integration.cjs`.

## Market updates

The primary market-data trigger is the Cloudflare Worker's scheduled handler in `server/refresh-scheduler.mjs`. Its UTC polling window covers both European seasons; a `Europe/Berlin` check starts the daily refresh at 07:30 local time and watches it until 08:25. The handler dispatches `.github/workflows/daily_update.yml`, where Python still performs the complete Yahoo/CNN/ECB update and Pages publication. Runner and provider latency affect completion time; the trigger time is not a guarantee that the dataset is finished at exactly 07:30.

Activation requires the encrypted Worker secret `GITHUB_REFRESH_TOKEN`: a fine-grained GitHub token limited to `Floydshot/research-studio` with Actions write access. It does not need Contents write access. Provision the secret without printing it, adding it to source, or passing it in command-line arguments; preserve the existing login and mail secrets. Without this credential the scheduled handler fails closed while normal dashboard/API requests continue to work. Configuring this additional access requires the owner's explicit approval.

The scheduler uses a separate D1 day/lease table for idempotency, correlates dispatches using `refresh-<Berlin date>-<request ID>`, avoids dispatching over an active run, and limits retries to three attempts. It checks the published `refresh-status.json`, a small sidecar generated from the same dataset and deployed in the same Pages artifact. An accepted dispatch alone is not success: the feed must contain a completed post-07:30 batch with successful provider retrievals. Known partial stock fields remain honestly flagged and do not cause endless retries. Scheduler queries never read private portfolio state or mail requests.

GitHub's own 05:30/06:30 UTC schedules remain a fallback, with a Berlin-time/freshness guard in `scripts/refresh_due.py`; early seasonal triggers and already-published daily batches are skipped. Workflow concurrency keeps all pending runs instead of replacing a queued refresh with a later UI push. A manual workflow dispatch with no scheduler inputs always forces a full refresh. The Worker proxies the public JSON with a short cache. New researched universe entries participate through `RESERVE_REFERENCES`; private holdings and login configuration never belong in the dataset or public repository.

`issuer_net.py` rechecks Cloudflare's latest SEC filing and USD GAAP `NetIncomeLoss` contexts on every full run. TTM earnings use the previous full year minus prior-year YTD plus current YTD, never rounded EPS or adjusted earnings. A bounded official quarterly-release fallback preserves its rounding uncertainty. A confirmed nonpositive result clears the old initial P/E, suppresses the P/E bar/discount, and displays an economic N/A with its source; it counts as complete only when the evidence was retrieved in that batch. A source outage keeps the original evidence date and remains partial.

`issuer_dbs.py` discovers DBS' latest quarterly financial PDF from its official index and reads the cumulative reported ROE with the annualisation and ordinary-shareholder footnotes checked. This period-specific issuer observation takes precedence over the vendor field and is explicitly labelled, e.g. `ROE · 9M 2026`, rather than called TTM. Unsupported layouts, newer conflicting provider periods and stale reports fail closed.

Checks: `python -m unittest scripts/test_issuer_net.py scripts/test_issuer_dbs.py scripts/test_researched_metrics.py`.

## Stock requests

The Outlook catalog opens an in-app request form instead of redirecting to GitHub. `POST /api/projects/{open|floyd|ts}/stock-requests` only accepts a bounded stock name, a client-generated UUID and an empty honeypot. Private routes require the existing profile session. Same-origin checks, transactional limits of five requests per source IP per UTC day and thirty globally, and a 24-hour name duplicate check bound notification volume. D1 stores only requests and delivery state in separate tables; this endpoint cannot alter holdings, the researched catalog or source code.

Set `STOCK_REQUEST_FROM`, `STOCK_REQUEST_TO` and `RESEND_API_KEY` as Worker secrets before enabling production requests. The recipient is a single fixed server-side address. The Resend bootstrap sender `onboarding@resend.dev` can send only to that Resend account's own registered email; another recipient requires a verified sending domain. Alternatively a server-side `STOCK_REQUEST_EMAIL.send()` binding can supply the same transport contract. Never put recipient addresses, keys, hashes or provider credentials in browser assets or checked-in configuration.

Only a confirmed provider acceptance produces `sent`; it does not guarantee inbox delivery. Failed or uncertain sends remain `pending` with their original request UUID and never retry automatically. Duplicate submissions do not generate additional mail. Until the mail secrets/binding are provisioned, the Worker keeps the previous GitHub request link active; it enables the in-app form on the next page load after mail configuration is present. The public activation flag contains no credentials or addresses. The backend persists no private portfolio amounts and never runs submitted text as instructions.
