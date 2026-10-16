# FinancialJuice source review

Reviewed: 2026-09-10. Decision: stop collector implementation pending written permission.

## Official source

The RSS link on the [official homepage](https://www.financialjuice.com/home)
points to https://www.financialjuice.com/feed.ashx?xy=rss . This address was
verified from the homepage link, not inferred from a private API.
No endpoint is configured for automated use in this repository.

A single read through the web retrieval tool returned an internal retrieval error;
this does not prove that the origin requires authentication or blocks RSS clients.
The XML payload was not obtained. Item count, item fields, timestamp timezone,
category feeds and update delay therefore remain unverified. Website category
tabs (Bonds, Commodities, Equities, Forex, Macro and Market Moving) are visible,
but their presence does not establish equivalent RSS category metadata or feeds.

## Reason implementation stopped

The official [Terms](https://www.financialjuice.com/tos.aspx), under Access to the
Service, limit collection, aggregation, copying and automated extraction unless
FinancialJuice expressly permits them in writing. Proprietary Rights also restricts
redistribution and non-personal copying to other servers without written consent.
No RSS-specific exception authorizing the requested public GitHub mirror was found.

The user's instruction explicitly requires stopping when the proposed collection
may conflict with site terms. Periodic collection, JSON transformation and public
snapshot-data publication cannot presently be established as permitted. This is a
project implementation decision based on that instruction, not a claim that all
personal RSS-reader use is prohibited. RSS availability alone is insufficient
evidence of permission for this particular redistribution workflow.

Before implementation resumes, obtain written permission or official RSS terms
covering 15-minute automated retrieval, 48-hour retention, headline/summary
storage, keyword tagging and public GitHub/raw redistribution. No request was
sent to FinancialJuice and no subscription or agreement was accepted.

## Implementation and verification status

- Collector, tagging rules, XML parser tests and scheduled workflow: not implemented.
- latest_financialjuice.json and public raw output: not created.
- Manual collection run, count, newest timestamp and headline samples: not verified.
- FinancialJuice Actions usage added by this change: zero.
- Proposed 15-minute interval, if later permitted: 96 jobs/day, about 2,880 per
  30 days; runner duration and account allowances determine usage, not this count alone.
- Existing Telegram code, workflow and snapshot-data are unchanged by this change.
- No authentication, cookies, tokens, private endpoints, HTML scraper or Railway
  service was used or added. SaveTicker, Reuters and Barchart are outside this change.

If permission is established, normalize timezone-aware RSS dates to UTC ISO 8601;
do not guess the timezone of naive dates. Preserve last good data on collection
failure, reject empty or malformed feed data, and report stale source timestamps.
These are proposed acceptance criteria, not completed behavior.
