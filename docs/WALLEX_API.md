# Wallex API facts

This file records what we know about the Wallex REST API for `WallexBroker` (milestone 2). Each fact names its source. Do not add a fact without a source.

Sources:

* **Probe**: `research/scripts/wallex_api_probe.py`, run on 2026-10-22 12:04 UTC from the developer machine in Iran with the account API key. The raw output holds personal data. It is not in git.
* **Docs**: developers.wallex.ir, downloaded on 2026-10-22 with `research/scripts/wallex_docs_fetch.py` (pages `spot-*`, `generated/*`, `socket-*`, `trade-credit-*`).

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

## 2. From the docs

| Fact | Value | Docs page |
|---|---|---|
| Requests | JSON body, header `Content-Type: application/json`. Number fields are strings, such as `"82494"`. | `docs/` (intro) |
| Auth | Header `x-api-key`. A key has read access by default. Trading needs the "trade" permission on the key. A key has an end date. At most 3 keys per account. | `generated/spot`, `create-api-key` |
| Trade-credit account | The challenge account is a trade-credit (sub) account. It has its own wallet, its own API key and its own order history. Spot requests for it need its own key and the header `sub-account-client-id: <clientId>`. | `spot-intro`, `trade-credit-intro` |
| Place order | `POST /v1/account/orders` with `symbol`, `side` (`BUY` or `SELL`), `type` (`LIMIT`, `MARKET`, `STOP_LIMIT`, `STOP_MARKET`), `price`, `quantity`, and optional `client_id` (letters, digits and `_`, unique). Answer: HTTP 201, `result` is the order. | `spot-create-order` |
| Order fields | `clientOrderId`, `symbol`, `side`, `type`, `price`, `origQty`, `executedQty`, `executedPrice`, `executedSum`, `executedPercent`, `status` (such as `NEW`, `FILLED`, `CANCELED`), `active`, `fee`, `fills[]` (`price`, `quantity`, `fee`, `feeAsset`, `feeCoefficient`, `makerFeeCoefficient`, `takerFeeCoefficient`, `isBuyer`, `timestamp`), `created_at`, `updated_at`. | `spot-create-order`, `spot-single-order-detail` |
| Order rate limit | At most 20 order requests per 10 s. The bot uses 15 plus a reserve of 5. | `spot-create-order` |
| Cancel | `DELETE /v1/account/orders/{client_id}`. Allowed while the order is not finished, also after a part fill. Answers 200, 400, 401, 422. | `spot-cancel-order`, `generated/delete-orders-client` |
| One order | `GET /v1/account/orders/{client_id}`. Answers 200 or 404 (order not found). | `spot-single-order-detail`, `generated/get-orders-client` |
| Open orders | `GET /v1/account/openOrders` with optional `symbol`, `page`, `per_page`. | `spot-active-orders` |
| Order history | `GET /v1/account/orders` with optional `from`, `to`, `page`, `per_page`, `type`, `side`, `market`. | `spot-order-list` |
| Market orders | A market order is cancelled when it would move the price more than 2% from the last trade, or when the book is too thin. | `spot-types` |
| Private websocket | Order and balance events exist (`orderSaved`, `orderRemoved`, `tradeDetails`, `balanceUpdated`). The token comes from `POST /v3/auth/token/refresh` with the login cookie of a browser session, not with the API key. So the bot polls the REST API instead. | `socket-private-channel`, `socket-token` |
| Trade-credit terms | A package runs 30 days. It has a daily fee, a warning level and a liquidation level. At the liquidation level, the package closes by itself. | `trade-credit-types` |

## 3. Still open

* The fee asset of a buy fill (base coin or quote currency). The broker handles both.
* The time window of `x-ratelimit-limit` (600 and 900).
* Orders on the challenge account without the `sub-account-client-id` header. The read test below says the key alone selects the account. The smoke test (task 2.4) checks it for orders.

## 4. Challenge account key (2026-10-22)

* `GET /v1/account/balances` with the key of the challenge account gave TMN 5,000,000, with and without the `sub-account-client-id` header.
* So the key alone selects the challenge account, at least for reads. The bot leaves `WALLEX_SUB_ACCOUNT_ID` empty. Set it only when Wallex rejects orders without it.

## 5. Server access (task 2.2)

Not checked yet. Run the probe on the shadow-test server and compare.
