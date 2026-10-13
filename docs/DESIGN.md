# AION 2 Market Assistant — Design Doc

Oct 12, 2026 · Danny

## Instructions for Claude Code

- This document is the spec. Build milestone by milestone (see Build plan), and stop after each milestone for review.
- The API spec is in `docs/openapi.json` (a copy of `https://aion2.exchange/api/openapi.json`). Generate the pydantic response models from that file. Do not scrape the aion2.exchange website; its web pages block automated access. Only call the `/api/...` endpoints.
- Never add code that interacts with the game client: no screen capture, OCR, memory reading, packet capture or input automation.
- Where something is listed under Open questions, use the stated default and make it a setting rather than guessing silently.

## Overview

A local Windows desktop app that pulls AION 2 Marketplace data from the aion2.exchange API, stores it in SQLite, and tells Danny what to craft, flip or hold to earn unbound Kina. Build in Python. It never touches the game client: no screen capture, no OCR, no input.

**In scope (v1)**

- Periodic refresh of listings, sale statistics and craft data from the aion2.exchange API for Danny's server.
- Price history, craft profit calculator, flip finder, server-merge watchlist, and extra Kina tools (see Analysis features).
- A realistic pricing model that uses actual sales, not just the cheapest listing.
- Danny's own trades and crafting results, entered by hand, to check the numbers against reality.

**Out of scope**

- Any interaction with the game: no screen capture, OCR, memory reading, packet capture, or input.
- Scraping aion2.exchange web pages; only its published API is used.
- Uploading anything to a server; all data stays local.

**Design rules**

1. The app runs independently of the game; nothing it does is visible to the game client.
2. API calls stay within polite limits (see External data) and follow the site's terms.
3. The data layer is source-agnostic (a `PriceSource` interface), so another source can be added later without changes to the analysis code.

## Architecture

Two threads keep the UI responsive: a background worker that refreshes data from the API on a schedule, and the Qt UI thread that reads from SQLite.

```
 aion2.exchange API  -->  API client           -->  market.db        -->  Analysis
 (read-only:              (scheduled refresh,       (SQLite, local:       (pricing model,
  listings, sales,         cache, freshness)         prices, sales,        crafts, flips,
  crafts)                                            recipes)              alerts)
                                                        ^                     |
                                                        |                     v
                                                        +- - - - - - -  Desktop UI (PySide6)
                                               Danny's trades,          opportunities, charts
                                               craft results
```

Market data moves one way, from the API into the database and on to the UI; the only other input is what Danny enters himself (dashed line).

- **Refresh worker** (`external/scheduler.py`): runs the API client on its schedule, writes results to SQLite, recomputes the pricing model for changed items, and emits Qt signals when new data lands.
- **API client** (`external/aion2exchange.py`): implements the `PriceSource` interface; all HTTP, caching and freshness checks live here.
- **UI thread** (`ui/`): reads from SQLite only; never waits on the network. A manual "Refresh now" button queues a job on the worker.

## Data model

One SQLite file (`data/market.db`) holds the API data the app has fetched, Danny's recipes, trades and craft results, and a raw response cache. Prices are in Kina; timestamps are UTC ISO strings. The API's 9-digit `item_key` is the canonical item id.

```sql
CREATE TABLE servers (
  id INTEGER PRIMARY KEY,
  api_server_id INTEGER UNIQUE,          -- /api/servers server_id
  name TEXT, region TEXT, faction TEXT,
  is_mine INTEGER DEFAULT 0,
  is_cross_server INTEGER DEFAULT 0      -- 1 once the cross-server market opens
);

CREATE TABLE items (
  item_key INTEGER PRIMARY KEY,          -- API id (9 digits)
  name TEXT, section TEXT, category_id INTEGER, category_name TEXT,
  watch INTEGER DEFAULT 0, hold_qty INTEGER DEFAULT 0, hold_avg_cost INTEGER,
  notes TEXT
);

CREATE TABLE item_snapshots (            -- one row per /api/items refresh
  item_key INTEGER, server_id INTEGER, captured_at TEXT,
  price INTEGER, listings INTEGER, change_pct REAL,
  units_sold INTEGER, sold_avg REAL, sold_min REAL, sold_max REAL,
  fetched_at TEXT,
  PRIMARY KEY (item_key, server_id, captured_at)
);

CREATE TABLE price_points (              -- /prices buckets
  item_key INTEGER, server_id INTEGER, bucket TEXT, ts TEXT,
  low REAL, high REAL, avg REAL, close REAL,
  listings INTEGER, samples INTEGER,
  PRIMARY KEY (item_key, server_id, bucket, ts)
);

CREATE TABLE sales_days (                -- /volume batch days
  item_key INTEGER, server_id INTEGER, registered TEXT,
  units_sold INTEGER, units_delta INTEGER, days_since_previous INTEGER,
  avg_price REAL, min_price REAL, max_price REAL,
  window_from TEXT, window_to TEXT,
  PRIMARY KEY (item_key, server_id, registered)
);

CREATE TABLE recipes (
  id INTEGER PRIMARY KEY,
  output_key INTEGER REFERENCES items(item_key), output_qty INTEGER DEFAULT 1,
  success_rate REAL, combo_probability REAL,
  combo_output_key INTEGER, combo_output_qty INTEGER,
  kina_cost INTEGER DEFAULT 0, craft_seconds INTEGER,
  profession TEXT, mastery_level INTEGER,
  rate_source TEXT,                      -- 'manual' | 'api'
  rate_updated_at TEXT, api_recipe_id INTEGER, notes TEXT
);

CREATE TABLE recipe_inputs (
  recipe_id INTEGER REFERENCES recipes(id),
  item_key INTEGER REFERENCES items(item_key),
  qty INTEGER NOT NULL, npc_price INTEGER,
  PRIMARY KEY (recipe_id, item_key)
);

CREATE TABLE craft_outcomes (            -- Danny's own crafting results
  id INTEGER PRIMARY KEY, recipe_id INTEGER REFERENCES recipes(id),
  at TEXT, attempts INTEGER, successes INTEGER, combos INTEGER, notes TEXT
);

CREATE TABLE trades (                    -- Danny's own buys and sells
  id INTEGER PRIMARY KEY, item_key INTEGER,
  side TEXT CHECK (side IN ('buy','sell')),
  qty INTEGER, unit_price INTEGER, fee_paid INTEGER,
  listed_at TEXT, done_at TEXT,          -- done_at NULL = still listed
  notes TEXT
);

CREATE TABLE api_cache (url TEXT PRIMARY KEY, body TEXT, fetched_at TEXT, status INTEGER);
CREATE TABLE fetch_log (id INTEGER PRIMARY KEY, at TEXT, endpoint TEXT, status INTEGER, ms INTEGER, error TEXT);
CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT);
-- e.g. sales_tax_pct = 10, listing_fee_rule, refresh_minutes, horizon_days, thresholds
```

## External data: aion2.exchange API

The aion2.exchange read-only API (OpenAPI 0.1.0, spec in `docs/openapi.json`) is the app's only data source: server-wide listings, sale statistics, market depth and crafting costs for every item its collector sees.

**Endpoints the app uses** (all GET, all take `server=<id>`)

| Endpoint | Gives | Used for |
| --- | --- | --- |
| `/api/servers` | Server ids, names, region, faction, `has_data` | Server picker; map Danny's server |
| `/api/health`, `/api/collection` | Latest snapshot time and age, last collector run, failures | Freshness check before trusting any number |
| `/api/categories` | Sections and category ids | Catalog grouping |
| `/api/items` | Per item: 9-digit `item_key`, cheapest `price`, `listings`, `change_pct`, `units_sold`, `sold_avg/min/max_price`; filter by `keys` (≤ 100), sort, window | Bulk refresh of the watchlist; flip and mover screens |
| `/api/items/{key}/prices` | Cheapest-price history (low/high/avg/close, listings, samples) by bucket | Price history charts |
| `/api/items/{key}/depth`, `/depth/ladder` | Cost to buy N units; listings grouped by unit price | Depth price; flip sizing |
| `/api/items/{key}/volume` | Units sold per batch day over a rolling 28-day window, with sale avg/min/max | Sell-through rate and realistic sale price |
| `/api/items/{key}/observations` | Every fetch of the sale statistics | Detect when the game updates sales data |
| `/api/crafts`, `/api/crafts/{key}`, `/api/crafts/model` | Expected craft cost per step: `success_rate`, `combo_probability`, expected `attempts`, materials, route (buy/morph/craft), margin vs market | Craft calculator inputs and cross-check |
| `/api/overview` | Price and listing movers over 1h–24h–7d | Opportunities tab |

Not used in v1: `/api/items/{key}` (covered by `/api/items?keys=`), `/api/sparklines` (covered by `/prices`), `/api/crafts/index` (trend chart only).

**Client rules**

- Module `external/aion2exchange.py`, using `httpx` with a 15 s timeout and retry with backoff on 5xx or network errors only.
- Polite polling: watchlist refresh at most every 10 minutes, batched through `/api/items?keys=` (100 ids a call). Item history and volume are fetched on demand and cached. Total under about 1 request per second.
- Cache every response in SQLite with its fetch time; serve from cache when the API is down, marked stale.
- Call `/api/health` first; if `snapshot_age_seconds` > 2 hours, show the external data as stale.
- Item ids: use the API's `item_key` as the catalog's canonical id. Recipes, trades and the watchlist all reference items by it.
- The spec shows no auth and no published rate limit or terms. Check the site's terms before release and contact the owner if the app will poll regularly.

**Trust and accuracy checks** (there is no second source, so the app checks the API against itself and against Danny's results)

- **Freshness:** every number shows its age. Data older than 2 hours (listings) or 2 days (sales) is greyed out and excluded from opportunities.
- **Collector health:** `/api/collection` shows failed runs and failure streaks; if the collector is failing, a banner says so and alerts pause.
- **Coverage:** `/api/overview` coverage (snapshots in the last 24 h) and per-item `samples`; items with thin data get a low-confidence tag.
- **Sanity checks:** flag an item when the cheapest price jumps more than 5× or drops below 0.2× its 7-day median between refreshes, or when sale min/avg/max are inconsistent.
- **Reality check:** Danny's own trades are compared with the API's sale prices for the same item and day; craft outcomes are compared with the API's success rates. Large gaps are shown on the Trust tab.
- **Update timing:** `/api/items/{key}/observations` shows when the game updates sale statistics, so the app can schedule sales refreshes just after.

## Pricing model

Every profit number uses a realistic sale price and a sell-through rate instead of the cheapest listing, and every craft uses expected value over success, failure and COMBO. This stops overpriced listings that never sell and lucky-case crafts from showing up as opportunities.

**Realistic sale price** (per item, recomputed after each API refresh)

1. **Sale statistics first:** `sold_avg_price` and `units_sold` from `/api/items` and `/volume` (28-day rolling window, daily batches). If the item sold ≥ 10 units in the window, its sale average anchors the estimate.
2. **Robust listing price:** the depth price for 10 units from `/depth` (cheapest listings of the latest poll), which ignores a single cheap or expensive outlier.
3. **Spike filter:** if the cheapest price moved more than 3 MADs (median absolute deviations) from its 7-day series in `/prices`, use the 7-day median instead until the move persists for 3 refreshes.
4. **Stuck listings:** if `listings` stays flat while `units_sold` doesn't grow for 3+ days, the item is marked illiquid and its listing price is not trusted.

Estimated sale price = the lowest of: the sale average (if anchored), the depth price, and the current cheapest listing minus one undercut step. Selling assumes Danny has to undercut the cheapest seller.

**Sell-through and liquidity**

- Units per day = `units_sold` ÷ window days (API), cross-checked with the day-to-day `units_delta`.
- Days to sell Q units = Q ÷ units per day; competing listings ahead of Danny's price are added to Q.
- Rankings use expected profit within a horizon (default 3 days): profit per unit × min(units Danny would sell, units the market absorbs in the horizon). An item with a large margin but almost no sales ranks low.

**Crafting expected value** (failure loses all materials; COMBO gives more items or a higher grade)

```
EV = p_s × [ (1 − p_c) × net(V_n) + p_c × net(V_c) ] − C_m − C_f
```

- p_s = success rate, p_c = COMBO probability, V_n = value of a normal success, V_c = value of a COMBO result, C_m = materials per attempt, C_f = crafting fee. Output values use the estimated sale price, not the cheapest listing.
- Rates come from, in order: Danny's own entry from the in-game Expected Probability tab (matches his mastery); the API's `/crafts/model` steps (`success_rate`, `combo_probability`, `mastery_level`); otherwise flagged as unknown.
- Multi-step crafts chain the expected attempts per step (the API's `attempts` field = 1 ÷ success rate) so an intermediate that fails costs its expected re-tries.
- **Break-even rate** = (C_m + C_f) ÷ the success value; shown beside p_s so the safety margin is visible.
- **Losing-streak risk:** chance of zero successes in N attempts = (1 − p_s)^N, plus the material budget needed to survive a 1-in-20 bad streak. Expensive low-rate crafts get a warning.
- **Outcome log:** Danny records each batch's successes, failures and COMBOs; the app compares them with the expected rate and flags recipes where results run well below it.

## Analysis features

Every feature uses the same net-sale rule: what Danny actually keeps after the Marketplace commission (about 10%, set in Settings) and the listing fee. All thresholds below are defaults, editable in Settings.

```
net(p) = p × (1 − tax) − listing_fee(p)
```

The listing fee rule is an open question until it is confirmed in game (flat amount or percentage).

**1. Price history**

- Per item: line chart of min and median price over time, with listing count as bars underneath.
- Ranges: 24 h, 7 days, 30 days, all. Weekday vs weekend shading.
- Stats: 7-day median, 7-day low/high, change vs previous week, volatility (coefficient of variation).

**2. Craft profit calculator**

- Danny enters recipes (output, inputs, quantities, crafting fee, success rate, NPC prices where relevant), or imports them from `/api/crafts/model`.
- Input cost per material = cheaper of the latest Marketplace price and the NPC price; or "own stock" valued at its net sale price (opportunity cost).
- Profit per craft = the crafting EV from the Pricing model.
- Ranked table of all recipes: EV per attempt, margin %, profit per hour (from craft time), break-even rate, streak risk, and a **saturation** warning when the output already has many listings or its price is falling.
- Data age shown per row; prices older than 24 h are greyed out.

**3. Flip finder**

- Candidate = a listing whose price p satisfies net(resale target) − p ≥ minimum profit (default 5,000 Kina) and ≥ minimum ROI (default 8%).
- Resale target = the estimated sale price from the Pricing model (never above the 7-day median or the second-cheapest current listing).
- Liquidity filter: skip items with fewer than 3 days of data or with fewer than 5 listings on average.
- Output: ranked list with buy price, resale target, expected profit, ROI, days to sell and confidence (based on data depth).

**4. Server-merge watchlist**

- Tracks Danny's server prices for watched items. Once the cross-server market opens, he marks it in Settings and new data is tagged cross-server.
- Before the merge: compare his server's prices with other servers in the region (the API's `server` parameter), to see whether his server is cheap or expensive.
- After the merge: compares pre-merge vs post-merge medians, so held stock can be sold where the price lands highest.
- Hold list: items Danny is stockpiling, with quantity, average cost and current value.

**5. Extra Kina tools**

- **Undercut alert:** for Danny's own active sell listings (entered in Trades), flag when the API shows a cheaper listing for the same item.
- **Supply squeeze:** flag items whose listing count dropped ≥ 40% over 3 days while the median rose. These are often good to sell into or stop crafting for.
- **Price dip alert:** watched item's current min is ≥ 20% below its 7-day median.
- **Sell vs. use/deliver:** Danny enters what an alternative pays (Abyss supply-order reward, value of keeping a duplicate for soul engraving); the app shows which is worth more after fees.
- **Farming value per hour:** Danny logs a run's drops and duration; the app values them at net market prices and ranks his farming routes by Kina/hour.
- **Timing insights:** once 2+ weeks of data exist, show per-item average price by weekday and time of day, to suggest when to list.
- **Event tags:** Danny tags dates (patch, event, merge) so charts show how prices reacted.
- **Trade ledger:** realized profit from his own buys and sells, with fees, by item and by week.

## UI

One PySide6 desktop window with a status bar at the top and tabs for the analysis features below. It can run on any monitor or stay minimized to the tray while alerts run.

**Status bar (always visible)**

- Server dropdown and a **Refresh now** button.
- Data age (last listings snapshot, last sales batch) and API health: green, amber (stale), red (collector failing or API down).
- Next scheduled refresh and the count of new opportunities since the last one.

**Tabs**

| Tab | Shows | Danny can |
| --- | --- | --- |
| Opportunities | Top flips, top crafts, movers and active alerts in one list, sorted by expected profit within the horizon | Open the item, dismiss, add to watchlist |
| Items | Searchable catalog with cheapest price, estimated sale price, units sold, change | Open price, depth and sales charts; watch; add notes |
| Crafting | Recipe table ranked by expected profit per hour, with break-even rate and streak risk | Add and edit recipes, set rates, log craft results |
| Flips | Flip candidates with ROI, days to sell and confidence | Mark as bought (creates a trade) |
| Watchlist and merge | Watched and held items; this server vs other servers; pre/post-merge comparison | Toggle cross-server mode, set hold quantities |
| Trades | Ledger of buys and sells, active listings, realized profit by week | Add, edit, mark sold |
| Trust | API health, collector failures, data coverage, gaps between API prices and Danny's real results | Read only |
| Settings | Tax %, listing fee rule, refresh interval, horizon, thresholds | Edit |

**Notifications**

- Desktop toast when a refresh finds a flip or craft above a set profit, a watched-item dip, or an undercut on one of Danny's listings.
- Optional sound. Alerts pause automatically while the API data is stale.

**Charts:** `pyqtgraph` embedded in the Items tab (fast and native to Qt).

## Build plan

Build in four milestones, each runnable on its own. Nothing depends on the game client, so work can start immediately.

**Project structure**

```
aion2-market/
  app/
    main.py              # PySide6 entry point
    ui/                  # status bar, tabs, charts
    external/
      source.py          # PriceSource interface
      aion2exchange.py   # API client: httpx, cache, health
      scheduler.py       # refresh worker thread
    analysis/
      fees.py            # net() rule
      pricing.py         # estimated sale price, sell-through
      crafting.py        # expected value, break-even, streak risk
      flips.py
      merge.py
      alerts.py
      trust.py           # freshness, sanity, reality checks
    db/
      schema.sql
      repo.py
  docs/
    DESIGN.md            # this file
    openapi.json         # aion2.exchange API spec
  tests/
    fixtures/api/        # recorded API responses
  data/market.db
  pyproject.toml
```

**Dependencies:** Python 3.12, PySide6, pyqtgraph, httpx, pydantic (response models generated from `docs/openapi.json`), numpy, pytest, respx (HTTP mocking). Package with PyInstaller later.

**Milestones**

1. **API client + database:** schema, pydantic models from `docs/openapi.json`, `aion2exchange.py` with caching, health and freshness checks, refresh worker. Done when the watchlist refreshes on schedule, survives the API being down, and stale data is marked.
2. **Pricing model + analysis core:** estimated sale price, sell-through, craft expected value with API rates, flip finder, merge comparison, trust checks, all as plain functions with unit tests.
3. **Desktop UI:** status bar, Opportunities, Items with charts, Crafting, Flips, Trust, Settings.
4. **Extras:** watchlist and merge mode, trades ledger and craft outcome log, alerts and toasts, timing insights, farming value per hour.

**Testing**

- Record real API responses once into `tests/fixtures/api/` and replay them with respx, so tests never hit the live API.
- Unit tests for `net()`, estimated sale price, sell-through, craft EV, break-even and streak risk, flip ranking, sanity checks.
- Failure tests: API timeout, 5xx, stale `/health`, collector failing, missing fields; the app must keep working from cache.
- Manual checklist per release: server switch, refresh while offline, alerts pause on stale data.

## Open questions

Settle these with a quick in-game check before the related milestone. Until then, use the default shown and keep it configurable.

- [ ] Exact Marketplace fees: commission % and how the listing fee is calculated. Default: 10% commission, no listing fee.
- [ ] Does the Marketplace have buy orders, or only sell listings? (Buy orders would add an order-book spread feature.) Default: sell listings only.
- [ ] Starter list of items and recipes to track, and Danny's crafting professions and mastery levels. Default: empty watchlist; import recipes from `/api/crafts/model`.

**API questions** (resolve before milestone 1 ships)

- [ ] Which aion2.exchange `server_id` is Danny's server, and does it show `has_data`? Default: the API's default server from `/api/servers`, changeable in the status bar.
- [ ] The API's terms of use and an acceptable polling rate; ask the site owner if unclear. Default: the polite limits under Client rules.
- [ ] How often the sale statistics update (daily batch?) and how far they lag the in-game market. Use `/observations` to measure this in milestone 1.
