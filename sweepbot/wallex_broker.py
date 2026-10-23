"""Live broker of the sweep bot on the Wallex REST API (task 2.3).

The facts come from `docs/WALLEX_API.md`. The broker follows the `Broker`
protocol of `sweepbot.broker`:

* `place_limit` sends `POST /v1/account/orders` with our own `client_id`.
  The client id is the order id of the bot. On a network error or a server
  error, the broker first asks for the order by its client id, so that a
  retry cannot place the same order twice. Then it retries once after 1 s
  (DESIGN section 12).
* `cancel` sends `DELETE /v1/account/orders/{client_id}`. A failed cancel
  is sent again at each poll until the order is finished.
* `poll` reads `GET /v1/account/openOrders` at most every `poll_s`. A new
  entry in the `fills` list of an order gives a fill event. An order that
  left the open list is read by its client id for its final state.

The private websocket of Wallex needs a browser login cookie, so the bot
polls. A fill can come up to `poll_s` late. This changes the exit time,
not the fill price.

Fees: a fill reports its fee and the fee asset. A fee in the base coin
lowers the coins that the bot holds. The fill event then carries the net
quantity, and the fee in the quote currency.
"""

import asyncio
import dataclasses
import itertools
import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Awaitable, Callable

from sweepbot import broker
from sweepbot import market as market_mod
from sweepbot import rules as rules_mod

log = logging.getLogger(__name__)

BASE_URL = "https://api.wallex.ir"
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")
FINISHED = {"FILLED", "CANCELED", "CANCELLED", "EXPIRED", "REJECTED"}
CANCELLED_STATES = {"CANCELED", "CANCELLED", "EXPIRED"}

# (status or None on a network error, JSON body or None)
Response = tuple[int | None, dict | None]
Transport = Callable[..., Awaitable[Response]]


class UrllibTransport:
    """Sends Wallex requests with the standard library in a worker thread."""

    def __init__(self, api_key: str, sub_account_id: str | None = None,
                 timeout_s: float = 10.0) -> None:
        """Stores the credentials. They never go to a log.

        Args:
            api_key: The Wallex API key of the trading account.
            sub_account_id: The `clientId` of a trade-credit account, sent
                as the `sub-account-client-id` header. None for the main
                account.
            timeout_s: The timeout of one request.
        """
        self._headers = {"x-api-key": api_key, "Content-Type": "application/json",
                         "User-Agent": UA}
        if sub_account_id:
            self._headers["sub-account-client-id"] = sub_account_id
        self._timeout = timeout_s
        # Bypass the dead ALL_PROXY of the developer machine (AGENTS.md).
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def _send(self, method: str, path: str, params: dict | None,
              body: dict | None) -> Response:
        """Sends one request and returns (status, JSON body)."""
        url = BASE_URL + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(url, data=data, headers=self._headers,
                                         method=method)
        try:
            with self._opener.open(request, timeout=self._timeout) as response:
                status, raw = response.status, response.read()
        except urllib.error.HTTPError as error:
            status, raw = error.code, error.read()
        except (OSError, ValueError) as error:
            log.warning("wallex %s %s failed: %s", method, path, type(error).__name__)
            return None, None
        try:
            return status, json.loads(raw)
        except ValueError:
            return status, None

    async def __call__(self, method: str, path: str, *, params: dict | None = None,
                       body: dict | None = None) -> Response:
        """Sends one request in a worker thread."""
        return await asyncio.to_thread(self._send, method, path, params, body)


@dataclasses.dataclass
class _Tracked:
    """One order of the bot and what the broker already reported."""

    market: str
    side: str
    price: float
    qty: float
    live_sent: bool = False
    fills_seen: int = 0
    filled: float = 0.0
    cancel_requested: bool = False
    done: bool = False


def _num(value, default: float = 0.0) -> float:
    """Converts a Wallex number string to float."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _order_list(result) -> list[dict]:
    """Returns the orders of an open-orders answer, in either known shape."""
    if isinstance(result, list):
        return [item for item in result if isinstance(item, dict)]
    if isinstance(result, dict):
        if isinstance(result.get("orders"), list):
            return result["orders"]
        orders = []
        for value in result.values():
            if isinstance(value, list):
                orders += [item for item in value if isinstance(item, dict)]
        return orders
    return []


class WallexBroker:
    """`Broker` on the Wallex REST API."""

    def __init__(self, *, transport: Transport, rules: dict[str, rules_mod.MarketRule],
                 poll_s: float = 1.0, clock: Callable[[], float] = time.time,
                 sleep: Callable[[float], Awaitable] = asyncio.sleep,
                 prefix: str = "sb") -> None:
        """Creates the broker.

        Args:
            transport: Sends one request: transport(method, path, params=,
                body=) -> (status, JSON). Tests use a fake.
            rules: The price and quantity rules per market.
            poll_s: The smallest time between two open-order reads.
            clock: The time source.
            sleep: The sleep function (tests pass a fake).
            prefix: The start of the client ids (letters and digits).
        """
        self._send = transport
        self._rules = rules
        self._poll_s = poll_s
        self._clock = clock
        self._sleep = sleep
        self._prefix = prefix
        self._ids = itertools.count(1)
        self._orders: dict[str, _Tracked] = {}
        self._events: list[broker.OrderEvent] = []
        self._last_poll = -1e18

    @classmethod
    def from_env(cls, rules: dict[str, rules_mod.MarketRule], **kwargs) -> "WallexBroker":
        """Builds the broker with WALLEX_API_KEY and WALLEX_SUB_ACCOUNT_ID.

        Raises:
            RuntimeError: WALLEX_API_KEY is not set.
        """
        key = os.environ.get("WALLEX_API_KEY")
        if not key:
            raise RuntimeError("WALLEX_API_KEY is not set")
        transport = UrllibTransport(key, os.environ.get("WALLEX_SUB_ACCOUNT_ID"))
        return cls(transport=transport, rules=rules, **kwargs)

    # Formatting ------------------------------------------------------------

    def format_order(self, market: str, price: float, qty: float) -> tuple[str, str]:
        """Returns the price and the quantity as Wallex number strings.

        The quantity is rounded down to the step, so the bot never sells
        more coins than it holds.
        """
        rule = self._rules.get(market, rules_mod.MarketRule())
        qty = rule.round_qty(qty)
        return (f"{price:.{rule.price_decimals}f}", f"{qty:.{rule.qty_decimals}f}")

    # Broker protocol -------------------------------------------------------

    def advance(self, now: float) -> None:
        """Does nothing. The exchange keeps its own time."""

    async def place_limit(self, market: str, side: str, price: float,
                          qty: float) -> str:
        """Sends a limit order and returns its client id."""
        client_id = f"{self._prefix}{int(self._clock() * 1000)}_{next(self._ids)}"
        price_text, qty_text = self.format_order(market, price, qty)
        body = {"symbol": market, "side": side.upper(), "type": "LIMIT",
                "price": price_text, "quantity": qty_text, "client_id": client_id}
        self._orders[client_id] = _Tracked(market=market, side=side,
                                           price=float(price_text), qty=float(qty_text))
        for attempt in range(2):
            status, data = await self._send("POST", "/v1/account/orders", body=body)
            if status in (200, 201) and data and data.get("success", True):
                self._apply(client_id, data.get("result") or {})
                return client_id
            if status is not None and status < 500:
                self._reject(client_id, f"HTTP {status}: {self._message(data)}")
                return client_id
            # A network or server error: the order may exist anyway.
            status, data = await self._send("GET", f"/v1/account/orders/{client_id}")
            if status == 200 and data and data.get("result"):
                self._apply(client_id, data["result"])
                return client_id
            if attempt == 0:
                await self._sleep(1.0)
        self._reject(client_id, "no answer after one retry")
        return client_id

    async def cancel(self, order_id: str) -> None:
        """Asks Wallex to cancel an order. A failure is retried at each poll."""
        tracked = self._orders.get(order_id)
        if tracked is None or tracked.done:
            return
        tracked.cancel_requested = True
        await self._send_cancel(order_id)

    async def open_orders(self) -> list[broker.Order]:
        """Returns the open orders of the account (also orders of other tools)."""
        status, data = await self._send("GET", "/v1/account/openOrders",
                                        params={"per_page": 100})
        if status != 200 or not data:
            raise RuntimeError(f"open orders failed: HTTP {status}")
        orders = []
        for item in _order_list(data.get("result")):
            orders.append(broker.Order(
                id=str(item.get("clientOrderId")), market=str(item.get("symbol")),
                side=str(item.get("side", "")).lower(), price=_num(item.get("price")),
                qty=_num(item.get("origQty")), ts=self._clock(),
                filled=_num(item.get("executedQty")), status=broker.OPEN))
        return orders

    async def balances(self) -> dict[str, float]:
        """Returns the free balance per asset: `value` minus `locked`."""
        status, data = await self._send("GET", "/v1/account/balances")
        if status != 200 or not data:
            raise RuntimeError(f"balances failed: HTTP {status}")
        balances = (data.get("result") or {}).get("balances") or {}
        items = balances.values() if isinstance(balances, dict) else balances
        return {item["asset"]: _num(item.get("value")) - _num(item.get("locked"))
                for item in items if "asset" in item}

    async def balances_detail(self) -> dict[str, dict[str, float]]:
        """Returns {asset: {"total", "locked"}}: `value` and `locked` of Wallex."""
        status, data = await self._send("GET", "/v1/account/balances")
        if status != 200 or not data:
            raise RuntimeError(f"balances failed: HTTP {status}")
        balances = (data.get("result") or {}).get("balances") or {}
        items = balances.values() if isinstance(balances, dict) else balances
        return {item["asset"]: {"total": _num(item.get("value")),
                                "locked": _num(item.get("locked"))}
                for item in items if "asset" in item}

    async def poll(self) -> list[broker.OrderEvent]:
        """Reads the order changes at most every `poll_s` and returns the events."""
        now = self._clock()
        active = {cid: t for cid, t in self._orders.items() if not t.done}
        if active and now - self._last_poll >= self._poll_s:
            self._last_poll = now
            await self._poll_orders(active)
        self._orders = {cid: t for cid, t in self._orders.items() if not t.done}
        events, self._events = self._events, []
        return events

    async def cancel_unknown(self, markets: list[str]) -> list[str]:
        """Cancels the open orders of `markets` that this broker did not send.

        DESIGN section 9: at start, the live broker removes old orders of
        the bot's markets, so that no order rests without a state machine.

        Returns:
            The client ids that it cancelled.
        """
        cancelled = []
        for order in await self.open_orders():
            if order.market in markets and order.id not in self._orders:
                status, _ = await self._send("DELETE", f"/v1/account/orders/{order.id}")
                log.warning("cancelled unknown order %s on %s: HTTP %s",
                            order.id, order.market, status)
                cancelled.append(order.id)
        return cancelled

    # Internals -------------------------------------------------------------

    async def _send_cancel(self, client_id: str) -> None:
        """Sends one DELETE and applies the answer."""
        status, data = await self._send("DELETE", f"/v1/account/orders/{client_id}")
        if status == 200 and data and isinstance(data.get("result"), dict):
            self._apply(client_id, data["result"])
        elif status is not None and status != 200:
            log.warning("cancel %s: HTTP %s %s", client_id, status, self._message(data))

    async def _poll_orders(self, active: dict[str, _Tracked]) -> None:
        """Reads the open orders, and the final state of finished ones."""
        status, data = await self._send("GET", "/v1/account/openOrders",
                                        params={"per_page": 100})
        if status != 200 or not data:
            log.warning("open orders poll: HTTP %s", status)
            return
        open_by_id = {str(item.get("clientOrderId")): item
                      for item in _order_list(data.get("result"))}
        for client_id, tracked in active.items():
            item = open_by_id.get(client_id)
            if item is not None:
                self._apply(client_id, item)
                if tracked.cancel_requested and not tracked.done:
                    await self._send_cancel(client_id)
                continue
            status, detail = await self._send("GET", f"/v1/account/orders/{client_id}")
            if status == 200 and detail and isinstance(detail.get("result"), dict):
                self._apply(client_id, detail["result"])

    def _apply(self, client_id: str, order: dict) -> None:
        """Turns the state of one Wallex order into new events."""
        tracked = self._orders.get(client_id)
        if tracked is None or tracked.done:
            return
        now = self._clock()
        state = str(order.get("status", "")).upper()
        if not tracked.live_sent and state not in ("REJECTED",):
            tracked.live_sent = True
            self._events.append(broker.OrderEvent("live", client_id, now))
        fills = order.get("fills") or []
        for fill in fills[tracked.fills_seen:]:
            self._fill(client_id, tracked, fill, now)
        tracked.fills_seen = max(tracked.fills_seen, len(fills))
        executed = _num(order.get("executedQty"))
        if executed > tracked.filled * (1 + 1e-9) + 1e-12 and not fills:
            # No fill list in this answer: report the new part at the mean price.
            self._fill(client_id, tracked, {
                "price": order.get("executedPrice") or order.get("price"),
                "quantity": executed - tracked.filled, "fee": 0}, now)
        if state in FINISHED:
            tracked.done = True
            if state in CANCELLED_STATES:
                self._events.append(broker.OrderEvent("cancelled", client_id, now))
            elif state == "REJECTED":
                self._events.append(broker.OrderEvent("rejected", client_id, now,
                                                      reason="rejected by Wallex"))

    def _fill(self, client_id: str, tracked: _Tracked, fill: dict, now: float) -> None:
        """Adds one fill event. A fee in the base coin lowers the quantity."""
        price = _num(fill.get("price"))
        qty = _num(fill.get("quantity"))
        if qty <= 0 or price <= 0:
            return
        tracked.filled += qty
        quote = market_mod.quote_of(tracked.market)
        base = tracked.market[:-len(quote)]
        fee = _num(fill.get("fee"))
        fee_asset = str(fill.get("feeAsset") or quote).upper()
        net_qty = qty
        fee_qty = 0.0
        if fee_asset == base:
            net_qty = qty - fee
            fee_qty = fee
            fee_quote = fee * price
        else:
            fee_quote = fee
        maker = False
        if fill.get("feeCoefficient") is not None and fill.get("makerFeeCoefficient") is not None:
            maker = abs(_num(fill["feeCoefficient"]) - _num(fill["makerFeeCoefficient"])) < 1e-12
        # The slot books cost = price x net qty + fee, which is price x qty.
        self._events.append(broker.OrderEvent("fill", client_id, now, price=price,
                                              qty=net_qty, fee=fee_quote, fee_qty=fee_qty,
                                              maker=maker))

    def _reject(self, client_id: str, reason: str) -> None:
        """Adds a rejected event and forgets the order."""
        log.warning("order %s rejected: %s", client_id, reason)
        self._orders[client_id].done = True
        self._events.append(broker.OrderEvent("rejected", client_id, self._clock(),
                                              reason=reason))

    @staticmethod
    def _message(data: dict | None) -> str:
        """Returns the message of a Wallex answer, or ""."""
        if not data:
            return ""
        return str(data.get("message") or data.get("result") or "")[:200]
