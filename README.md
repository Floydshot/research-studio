# Research Studio

The public Open Projects board contains Tech Stonks and Value Stonks. Floyd Projects and TS Projects use separate authenticated workspaces and add a combined Overview. A24 remains in the researched data/catalog but is not a navigation tab.

## Runtime

`index.html` and `client/` are the browser application. `server/project-api.mjs` handles passwords, sessions and versioned private state in Cloudflare D1. The private `ProjectPasswordVerifier` Durable Object performs PBKDF2 verification through RPC so password hashing fits within the Workers Free CPU budget without reducing its iteration count. `scripts/build-worker.mjs` embeds the public assets and API into `dist/worker.mjs`. Only the public market-data feed is fetched from the existing GitHub Pages deployment; its daily workflow continues to run independently.

Run `npm run build`, then deploy the built Worker using Wrangler. Bind the project database as `DB`. The deployment account and actual D1 database ID belong in `wrangler.jsonc` after provisioning; neither is a password. Runtime secrets must never be checked into source or static assets:

- `FLOYD_PASSWORD_HASH` and `TS_PASSWORD_HASH`: `pbkdf2-sha256$100000$<16-byte-or-longer random salt in hex>$<32-byte PBKDF2-SHA256 result in hex>`.
- `SESSION_SECRET`: at least 32 random characters, used to pseudonymize rate-limit keys.

Provision secrets through a protected JSON file or stdin to Wrangler. Do not pass the password hashes through Dotenv or shell interpolation: they contain literal dollar signs. Keep secrets outside this checkout and delete temporary deployment files after provisioning. Normal code deployments preserve existing secrets. Changing a profile password hash invalidates that profile's older sessions.

## Private state

Each private workspace has independent Tech/Value scenario settings and an Overview horizon. Authentication is enforced on the server for every read and write. Session cookies are HttpOnly, Secure, SameSite=Strict and scoped to the selected profile API path. Login attempts are limited to five per fifteen minutes per profile and pseudonymized source IP. Responses containing private settings are not cached.

Updates use `If-Match` revisions; stale updates return HTTP 409 instead of overwriting another device. Only the public sandbox uses browser-local scenario storage. No existing browser-local personal input is silently copied into the public sandbox or uploaded to a private workspace.

The old GitHub Pages address redirects to the Cloudflare application. `legacy.html` retains the previous browser-local view and can export Tech/Value inputs as a JSON file. The import action in the unlocked Floyd Projects menu validates and previews that file, and saves it only after explicit confirmation. A24 is excluded from this migration.

The Overview sums both independent monthly projections for a common selected horizon. It preserves each depot's capital, monthly purchases, saved exclusions, extra positions and reinvestment choice, without changing its individual saved horizon. Private Tech calculations include distributions so the combined Overview includes both depots; the individual Tech interface keeps its concise growth-only layout. The public Tech sandbox retains its prior growth-only calculation. Historical rates remain assumptions rather than predictions.

## Market updates

`.github/workflows/daily_update.yml` refreshes `data.json` every day at 06:30 UTC and on manual workflow dispatch. The Worker proxies that public JSON with a short cache. New researched universe entries participate through `RESERVE_REFERENCES`; private holdings and login configuration never belong in this dataset or the public GitHub repository.
