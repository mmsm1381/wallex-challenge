# Wallex API facts

This file records what we know about the Wallex REST API for `WallexBroker` (milestone 2). Each fact names its source. Do not add a fact without a source.

Sources:

* **Probe**: `research/scripts/wallex_api_probe.py`, run on 2026-10-22 12:04 UTC from the developer machine in Iran with the account API key. The raw output holds personal data. It is not in git.
* **Docs**: developers.wallex.ir. Not read yet. `research/scripts/wallex_docs_fetch.py` downloads it as text.

## 1. Confirmed by the probe

| Fact | Value | Source |
|---|---|---|
| Base URL | `https://api.wallex.ir` | Probe |
| Auth header | `x-api-key: <key>`. Every account GET answered 200 with this header. | Probe |
| Balances | `GET /v1/account/balances` → `result.balances` is a dict per asset with `value` and `locked` (strings). `GET /v2/account/balances` gives a list. | Probe |
| Open orders | `GET /v1/account/openOrders` → `result.orders` (list). `GET /v2/account/openOrders` → `result` (list). | Probe |
| Order history | `GET /v1/account/orders` → `result.orders`, 25 per page, `result_info.has_more_pages` | Probe |
| Own trades | `GET /v1/account/trades` → `result.AccountLatestTrades`, 100 per page | Probe |
| Fees | `GET /v1/account/fee` → per market `makerFeeRate`, `takerFeeRate`. BTCTMN 0.25% / 0.30%, ETHUSDT 0.10% / 0.13%. This agrees with the config. | Probe |
| Rate limit headers | `x-ratelimit-limit` and `x-ratelimit-remaining` on account calls. The limit was 600 on balances and 900 on order history. The time window is not known. | Probe |
| Market rules | `GET /v1/markets` → `result.symbols`. `tickSize` and `stepSize` are numbers of decimals. Saved in `research/wallex_markets.json` and used by `sweepbot/rules.py`. | Probe |
| Minimum order | `minNotional` 50,000 toman on TMN markets, 1 USDT on USDT markets. `minQty` = 10^-stepSize. | Probe |
| Market orders | `isMarketTypeEnable` is false on ADATMN, TRXTMN, PEPEUSDT and others. The bot uses limit orders for every exit, also for a cut. | Probe |
| Errors | A missing route gives HTTP 404 with `{"success": false, "code": 404, "message": ...}`. A missing parameter gives HTTP 422 with `result.<field>`. | Probe |

Rules of the default plan markets:

| Market | Price tick | Quantity step | Min value |
|---|---|---|---|
| ADATMN | 1 toman | 0.1 | 50,000 toman |
| TRXTMN | 1 toman | 0.1 | 50,000 toman |
| ETHTMN | 1 toman | 0.00001 | 50,000 toman |
| ETHUSDT | 0.01 USDT | 0.00001 | 1 USDT |
| PEPEUSDT | 0.00000001 USDT | 1 | 1 USDT |
| USDTTMN | 1 toman | 0.01 | 50,000 toman |

## 2. Still open (needs the docs)

* Place order: path, method, body fields (symbol, side, type, price, quantity), and the answer (order id field).
* Cancel order: path, method, and the answer.
* One order by id: path, and the status values (new, partial, filled, cancelled).
* The time window of the rate limit.
* Error codes of a rejected order (funds, precision, minimum value).

## 3. Server access (task 2.2)

Not checked yet. Run the probe on the shadow-test server and compare.
