# Market hub: validation-ready foundation, live sources pending

This change does **not** deliver live SaveTicker or options collection. It provides
normalization, calculations, source-health reporting, tests and separate Actions.
Neither provider is enabled. Do not interpret a passing test as a successful live
collection. Existing Telegram code, authentication and mirror workflow are unchanged.
No Railway service, paid subscription, login automation or credentials are added.

## Source investigation (2026-09-10)

| Source | Observed method / constraint | Decision |
| --- | --- | --- |
| [SaveTicker](https://saveticker.com/news) | Public browser list eventually renders; opening a detail article prompts login. Ordinary HTTP request returned 403. No supported public XHR/JSON contract or redistribution permission verified. | Disabled. No invented internal endpoint, cookie reuse, detail scraping, or authentication bypass. |
| [Cboe delayed quotes](https://www.cboe.com/delayed_quotes/now) | Page explicitly prohibits automated extraction of its quote table. | Not used, including underlying CDN endpoints. |
| [Tradier](https://docs.tradier.com/docs/market-data) | Documented options API, account/token required; sandbox delayed. | Candidate only; credentials and public redistribution rights unverified. |
| [Alpha Vantage](https://www.alphavantage.co/documentation/) | Documented options product requires API key and premium entitlement. | Not selected for a no-cost public mirror. |
| [Market Data](https://www.marketdata.app/docs/api/options/chain/) | Documented chain API; [plan limits](https://www.marketdata.app/docs/account/plan-limits/) disallow redistribution on the free plan. | Not mirrored to public GitHub. |

Next step is a supported source with **both** automated collection and public
redistribution permission. Record its policy evidence and implement/test its actual
response adapter in a follow-up PR before enabling a schedule. An API key alone
does not establish redistribution rights. Do not paste keys into JSON or URLs.

`market_sources.json` defaults to disabled. The current collector supports only a
documented interchange JSON input from explicitly configured, query-free HTTPS
URLs on `raw.githubusercontent.com` or SaveTicker's public hosts. It is **not** a
native SaveTicker parser or a native brokerage API client. The booleans are a
reviewed configuration gate, not automated legal verification. Unexpected schema,
missing data, denied robots access and HTTP errors fail with sanitized identifiers.
SaveTicker requests require an accessible robots policy permitting the request.

## Input contract and calculations

News envelope: `timestamp` and nonempty `items`. Every item requires string
`news_id`, `title`, `published_at`, `source`, public SaveTicker `url`; optional
`category` and ticker list. All times require ISO 8601 with timezone. Normalize to
UTC, retain latest 48 hours, deduplicate ID, normalized URL and source/title.
Source envelope must be under 2 hours old. Summary is null because article bodies
are not fetched; `fetch_news_detail` explicitly rejects the login-only route.

Options envelope: `symbol`, `as_of`, positive `spot_price`, nonempty `contracts`.
Each contract has timezone-explicit `expiration`, positive `strike`, `side`
(`call`/`put`), and nullable `volume`, `open_interest`, `implied_volatility`, `bid`,
`ask`. IV is an annualized **decimal** (0.4 = 40%). Adapter must resolve exchange
expiration time and adjusted contract identities; duplicate expiration/strike/side
is rejected. Inputs older than 24 hours are unavailable, including weekends; no
exchange holiday calendar is implemented. Coverage is the supplied chain, not a
claim of full market coverage. Prices, IV and missing fields stay separate by side.

Per-symbol summaries include total volume/OI, put/call ratios, top five contracts
by OI and volume for each side, ATM IV, ATM strike, expiration concentration and
volume/OI >= 3 with volume >= 100 and positive OI. Unknown totals/zero-denominator
ratios remain null. OI-zero contracts do not receive an infinite ratio. Expected
move uses mean call/put ATM IV × sqrt(actual days / 365), choosing the expiration
closest to 7 days, and is null if that expiry is beyond 14 days. This is a one-sigma
estimate, not a forecast. Concentration is not signed GEX or a confirmed dealer wall.

## Data branch and output

Only `snapshot-data` receives generated `data/` commits:

- `data/saveticker/latest_saveticker.json` (only after successful authorized collection)
- `data/options/{NVDA,MU,AVGO,AMD,TSM,SMH,SOXX}.json` (same restriction)
- `data/options/options_summary.json` (same restriction)
- `data/health/saveticker.json` and `data/health/options.json` (also on failure)
- `data/market/market_snapshot.json` (can explicitly be `degraded`)

Failures preserve last good data and mark health unavailable. The builder checks
health before accepting last good data. It checks Telegram counts, timestamps and
latest item age (48 hours); it never writes `latest_snapshot.json`. Signals
`ai_semiconductor`, `memory_hbm`, `optical_networking`, `power_infrastructure`,
`macro_rates_risk` remain null. Malformed/stale/empty sources are unavailable.
Fetch/check timestamps alone do not generate a new commit; unchanged observations
retain their original timestamp. Source timestamps and quote `as_of` are preserved.
Check Actions for the most recent polling time.

All JSON is scanned before publication for sensitive fields, known token patterns
and configured environment secret values. This supplements allowlisted schemas;
it cannot prove the absence of every possible unknown secret. No credentials are
needed or provided to pull-request validation.

## Schedules and cost

Schedules run on the default branch after merge and may be delayed by GitHub:

- SaveTicker every 30 minutes, only when repository variable
  `MARKET_HUB_ENABLE_SAVETICKER=true` **and** reviewed source configuration permits it.
- Options hourly 13:17–21:17 UTC weekdays (covers US regular hours across DST),
  plus 00:17 and 06:17 UTC daily; gated by `MARKET_HUB_ENABLE_OPTIONS=true`.
- Builder hourly at :27 UTC. It reports a failing/degraded run until sources are ready.
- Manual dispatch is available for diagnostics; disabled sources return exit code 2.

At full activation: about 1,440 news + 330 options + 720 builder jobs per 30 days,
roughly 2,490 short jobs/month before PR checks. Each has a 10-minute timeout, not
a promised cost. Current source schedules are skipped. Check the account's Actions
allowance before enabling. Writers share concurrency, skip unchanged commits, use
the bot identity and retry fetch/rebase/push three times without force pushes.
Existing Telegram mirror retains its own concurrency; a simultaneous push may
cause it to retry on its next scheduled run. No branch protection is bypassed.

## Validation and raw links

`python -m unittest test_snapshot test_market_hub -v` runs the existing seven
Telegram tests and nine new tests. PR Actions also fetches the actual public
Telegram JSON, checks freshness, verifies honest unavailable-provider diagnostics,
scans outputs and uploads only diagnostic JSON as a seven-day artifact. Test
fixtures stay in temporary directories; they are never published as market data.
Live source validation remains blocked until a permitted source is available.

Existing public input:
[Telegram raw JSON](https://raw.githubusercontent.com/shrtm5307/telegram-market-feed/snapshot-data/latest_snapshot.json).
New raw files are **not yet verified or published** by this PR. After an approved
merge and successful data job their URL prefix is
`https://raw.githubusercontent.com/shrtm5307/telegram-market-feed/snapshot-data/`
followed by the paths above. Do not advertise those URLs as live before checking
HTTP status, schema, source time, nonempty items and source health.
