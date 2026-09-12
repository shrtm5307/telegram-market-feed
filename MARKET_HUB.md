# Market hub

This change adds a live, no-key TickerTick news collector and retains the newest
24 hours of link metadata. It does not copy article bodies or automate SaveTicker.
The options foundation remains disabled until a source with documented collection
and public-redistribution rights is configured. Existing Telegram collection,
authentication and snapshot mirroring are unchanged.

## Sources and boundaries

| Source | Use | Status |
| --- | --- | --- |
| [TickerTick](https://github.com/hczhu/TickerTick-API#terms-of-use) | Documented JSON API for ticker, market, analysis, earnings and SEC news. The terms permit commercial and non-commercial API use and impose a 10 requests/minute rate limit. | Enabled without a key. Only title, source, publication time, ticker tags and a query-free article link are published. |
| [SaveTicker](https://saveticker.com/news) | Public browser list and manual research. Detail access may require login; ordinary HTTP returned 403 and no supported public API or redistribution permission was verified. | Not automated or mirrored. |
| [Cboe delayed quotes](https://www.cboe.com/delayed_quotes/now) | Page prohibits automated extraction of its quote table. | Not used. |
| [Tradier](https://docs.tradier.com/docs/market-data) | Documented options API; account and token required. | Candidate only. |
| [Alpha Vantage](https://www.alphavantage.co/documentation/) | Documented options product requires a key and premium entitlement. | Not selected. |
| [Market Data](https://www.marketdata.app/docs/api/options/chain/) | Free-plan terms do not permit a public GitHub mirror. | Not selected. |

TickerTick requests are built from hard-coded documented queries. No cookie,
login, secret or article-detail route is used. The watchlist is NVDA, MU, AVGO,
AMD, TSM, SMH, SOXX, SKHY, SNDK, WDC and STX. Separate market, analysis,
earnings and SEC feeds improve coverage. Curated, market, trade and industry
stories are combined into one request, and earnings and SEC stories into another.
The collector makes four requests per run, remaining below the provider's limit
of no more than five requests in any 30-second window.

## News normalization

The collector converts TickerTick's millisecond timestamps to UTC, removes stories
older than 24 hours and deduplicates by ID, normalized URL and source/title hash.
Ticker tags are normalized to uppercase. URL query parameters and fragments are
removed before publication so tracking or signed values cannot enter the public
snapshot. `summary` remains null because article bodies are not fetched.

Output:

- `data/news/latest_news.json`
- `data/health/tickertick.json`

The news snapshot includes `timestamp`, `status`, `source`, `window_hours: 24`,
`freshness_status`, `source_lag_minutes`, `latest_published_at`, `count` and
`items`. Freshness is `ok` through 15 minutes, `delayed` through 60 minutes and
`stale` beyond 60 minutes. A delayed provider is reported as a warning while its
valid 24-hour data remains available. A failed fetch preserves the last good
snapshot and writes an unavailable health record.

## Options calculations

The disabled options adapter accepts one row per expiration, strike and side.
Prices, IV and missing values remain separate for calls and puts. It calculates
volume/OI totals and put/call ratios, top strikes, ATM IV, expiration
concentration, volume/OI observations and a one-sigma expected move. These values
do not establish dealer direction, signed GEX, call walls or put walls.

## Combined snapshot

The builder reads the existing Telegram snapshot, the 24-hour TickerTick snapshot
and, when available, options summaries. It writes
`data/market/market_snapshot.json`. A stale, empty or failed source is explicitly
marked unavailable; the builder does not substitute fixture data or claim that an
old quote is current.

All JSON is checked for sensitive field names, configured environment secret
values and known token patterns before publication. The public data branch receives
generated `data/` commits only.

## Schedule and validation

- TickerTick runs every 5 minutes. GitHub may delay scheduled workflow starts;
  source lag is measured from the newest provider timestamp rather than the job time.
- The combined snapshot builds hourly at minute 27.
- Options schedules remain gated by `MARKET_HUB_ENABLE_OPTIONS=true`.
- Pull requests run unit tests, validate the public Telegram snapshot, perform a
  live TickerTick request and verify that the news window is exactly 24 hours.

Run locally with:

```text
python -m unittest test_snapshot test_market_hub -v
```

Public Telegram input:
[latest_snapshot.json](https://raw.githubusercontent.com/shrtm5307/telegram-market-feed/snapshot-data/latest_snapshot.json).
After merge and the first successful news job, the TickerTick output will be at
`https://raw.githubusercontent.com/shrtm5307/telegram-market-feed/snapshot-data/data/news/latest_news.json`.
