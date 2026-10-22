"""Tests for the Wallex broker with a fake HTTP transport. No real requests."""

import asyncio

import pytest

from sweepbot import rules
from sweepbot import wallex_broker

RULES = {"BTCTMN": rules.MarketRule(price_tick=1.0, qty_step=1e-8, min_qty=1e-8,
                                    min_notional=50_000),
         "ETHTMN": rules.MarketRule(price_tick=1.0, qty_step=1e-5, min_qty=1e-5,
                                    min_notional=50_000)}


class FakeTransport:
    """Answers requests from a script per (method, path) and records them."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.scripts: dict[tuple[str, str], list] = {}

    def add(self, method: str, path: str, *answers) -> None:
        """Adds answers for one route. The last answer repeats."""
        self.scripts.setdefault((method, path), []).extend(answers)

    async def __call__(self, method, path, *, params=None, body=None):
        self.calls.append((method, path, params, body))
        answers = self.scripts.get((method, path))
        if not answers:
            return 404, {"success": False, "message": "not found"}
        return answers.pop(0) if len(answers) > 1 else answers[0]


class Clock:
    """A clock that the test moves by hand."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def order(cid: str, status: str = "NEW", fills: list | None = None, executed: str = "0",
          symbol: str = "BTCTMN", side: str = "BUY") -> dict:
    """Builds a Wallex order object."""
    return {"clientOrderId": cid, "symbol": symbol, "side": side, "type": "LIMIT",
            "price": "21710000000", "origQty": "0.00004606", "executedQty": executed,
            "status": status, "active": status == "NEW", "fills": fills or []}


def fill(qty: str, price: str = "21710000000", fee: str = "2500",
         fee_asset: str = "TMN", maker: bool = True) -> dict:
    """Builds a Wallex fill object."""
    return {"price": price, "quantity": qty, "fee": fee, "feeAsset": fee_asset,
            "feeCoefficient": "0.0025" if maker else "0.003",
            "makerFeeCoefficient": "0.0025", "takerFeeCoefficient": "0.003"}


def make() -> tuple[wallex_broker.WallexBroker, FakeTransport, Clock, list]:
    """Builds a broker with a fake transport, a fake clock and a fake sleep."""
    transport, clock, sleeps = FakeTransport(), Clock(), []

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)

    wallex = wallex_broker.WallexBroker(transport=transport, rules=RULES, clock=clock,
                                        sleep=sleep, poll_s=1.0)
    return wallex, transport, clock, sleeps


def run(coroutine):
    """Runs one coroutine."""
    return asyncio.run(coroutine)


def place(wallex, price=21_710_000_000.4, qty=0.000046061) -> str:
    """Places a BTCTMN buy and returns its client id."""
    return run(wallex.place_limit("BTCTMN", "buy", price, qty))


def test_place_sends_limit_order_with_wallex_number_strings() -> None:
    """POST /v1/account/orders with integer toman price and 8 decimals, rounded down."""
    # given
    wallex, transport, _, _ = make()
    transport.add("POST", "/v1/account/orders", (201, {"success": True, "result": {}}))

    # when
    client_id = place(wallex)

    # then
    method, path, _, body = transport.calls[0]
    assert (method, path) == ("POST", "/v1/account/orders")
    assert body == {"symbol": "BTCTMN", "side": "BUY", "type": "LIMIT",
                    "price": "21710000000", "quantity": "0.00004606",
                    "client_id": client_id}
    assert client_id.replace("_", "").isalnum()


def test_place_answer_new_gives_live_event() -> None:
    """The order is on the book at once: BidPending -> BidLive."""
    # given
    wallex, transport, _, _ = make()
    transport.add("POST", "/v1/account/orders",
                  (201, {"success": True, "result": order("x", "NEW")}))

    # when
    client_id = place(wallex)
    events = run(wallex.poll())

    # then
    assert [(e.kind, e.order_id) for e in events] == [("live", client_id)]


def test_place_validation_error_gives_rejected_event_without_retry() -> None:
    """HTTP 422 is a final answer. The bot must not send the order again."""
    # given
    wallex, transport, _, _ = make()
    transport.add("POST", "/v1/account/orders",
                  (422, {"success": False, "message": "price precision"}))

    # when
    place(wallex)
    events = run(wallex.poll())

    # then
    assert [e.kind for e in events] == ["rejected"]
    assert "price precision" in events[0].reason
    assert len(transport.calls) == 1


def test_place_network_error_checks_the_order_before_retry() -> None:
    """A lost answer: the order is not on Wallex (404), so the broker retries once."""
    # given
    wallex, transport, _, sleeps = make()
    transport.add("POST", "/v1/account/orders", (None, None),
                  (201, {"success": True, "result": order("x")}))

    # when
    place(wallex)

    # then
    routes = [(m, p.split("/")[-1] if p.count("/") > 3 else p) for m, p, _, _ in transport.calls]
    assert [m for m, _ in routes] == ["POST", "GET", "POST"]
    assert sleeps == [1.0]


def test_place_network_error_with_order_on_wallex_does_not_send_twice() -> None:
    """The order exists although the answer was lost. No second POST."""
    # given
    wallex, transport, clock, _ = make()
    transport.add("POST", "/v1/account/orders", (None, None))
    client_id = f"sb{int(clock.now * 1000)}_1"
    transport.add("GET", f"/v1/account/orders/{client_id}",
                  (200, {"success": True, "result": order(client_id)}))

    # when
    place(wallex)
    events = run(wallex.poll())

    # then
    assert [m for m, _, _, _ in transport.calls].count("POST") == 1
    assert [e.kind for e in events] == ["live"]


def test_poll_reports_each_new_fill_once() -> None:
    """A part fill shows in the open orders. A second poll repeats no fill."""
    # given
    wallex, transport, clock, _ = make()
    transport.add("POST", "/v1/account/orders", (201, {"success": True, "result": order("x")}))
    client_id = place(wallex)
    run(wallex.poll())
    part = order(client_id, "NEW", fills=[fill("0.00002000")], executed="0.00002")
    transport.add("GET", "/v1/account/openOrders",
                  (200, {"success": True, "result": {"orders": [part]}}))

    # when
    clock.now += 1.0
    first = run(wallex.poll())
    clock.now += 1.0
    second = run(wallex.poll())

    # then
    assert [(e.kind, e.qty, e.maker) for e in first] == [("fill", 0.00002, True)]
    assert first[0].fee == 2500.0
    assert second == []


def test_poll_is_throttled_to_poll_s() -> None:
    """Two polls within poll_s send one open-orders request."""
    # given
    wallex, transport, clock, _ = make()
    transport.add("POST", "/v1/account/orders", (201, {"success": True, "result": order("x")}))
    place(wallex)
    transport.add("GET", "/v1/account/openOrders", (200, {"success": True, "result": {"orders": []}}))

    # when
    clock.now += 1.0
    run(wallex.poll())
    clock.now += 0.5
    run(wallex.poll())

    # then
    assert [p for _, p, _, _ in transport.calls].count("/v1/account/openOrders") == 1


def test_order_that_left_the_open_list_is_read_for_its_final_state() -> None:
    """A filled order is not open any more. The broker reads it by client id."""
    # given
    wallex, transport, clock, _ = make()
    transport.add("POST", "/v1/account/orders", (201, {"success": True, "result": order("x")}))
    client_id = place(wallex)
    run(wallex.poll())
    transport.add("GET", "/v1/account/openOrders", (200, {"success": True, "result": {"orders": []}}))
    done = order(client_id, "FILLED", fills=[fill("0.00004606")], executed="0.00004606")
    transport.add("GET", f"/v1/account/orders/{client_id}", (200, {"success": True, "result": done}))

    # when
    clock.now += 1.0
    events = run(wallex.poll())
    clock.now += 1.0
    later = run(wallex.poll())

    # then
    assert [(e.kind, e.qty) for e in events] == [("fill", 0.00004606)]
    assert later == []


def test_fee_in_base_coin_lowers_the_quantity() -> None:
    """A buy fee of 0.0000001 BTC: the bot holds 0.0000199 BTC, the fee is in toman."""
    # given
    wallex, transport, clock, _ = make()
    transport.add("POST", "/v1/account/orders", (201, {"success": True, "result": order("x")}))
    client_id = place(wallex)
    run(wallex.poll())
    part = order(client_id, fills=[fill("0.00002", price="20000000000", fee="0.0000001",
                                        fee_asset="BTC")], executed="0.00002")
    transport.add("GET", "/v1/account/openOrders", (200, {"success": True, "result": {"orders": [part]}}))

    # when
    clock.now += 1.0
    event = run(wallex.poll())[0]

    # then
    assert event.qty == pytest.approx(0.0000199)
    assert event.fee == pytest.approx(2000.0)
    assert event.price * event.qty + event.fee == pytest.approx(20_000_000_000 * 0.00002)


def test_cancel_answer_canceled_gives_cancelled_event() -> None:
    """DELETE /v1/account/orders/{client_id} -> CANCELED -> Idle."""
    # given
    wallex, transport, _, _ = make()
    transport.add("POST", "/v1/account/orders", (201, {"success": True, "result": order("x")}))
    client_id = place(wallex)
    run(wallex.poll())
    transport.add("DELETE", f"/v1/account/orders/{client_id}",
                  (200, {"success": True, "result": order(client_id, "CANCELED")}))

    # when
    run(wallex.cancel(client_id))
    events = run(wallex.poll())

    # then
    assert ("DELETE", f"/v1/account/orders/{client_id}") in [(m, p) for m, p, _, _ in transport.calls]
    assert [e.kind for e in events] == ["cancelled"]


def test_failed_cancel_is_sent_again_at_the_next_poll() -> None:
    """DESIGN section 12: retry a cancel until it is confirmed."""
    # given
    wallex, transport, clock, _ = make()
    transport.add("POST", "/v1/account/orders", (201, {"success": True, "result": order("x")}))
    client_id = place(wallex)
    run(wallex.poll())
    path = f"/v1/account/orders/{client_id}"
    transport.add("DELETE", path, (None, None),
                  (200, {"success": True, "result": order(client_id, "CANCELED")}))
    transport.add("GET", "/v1/account/openOrders",
                  (200, {"success": True, "result": {"orders": [order(client_id)]}}))

    # when
    run(wallex.cancel(client_id))
    clock.now += 1.0
    events = run(wallex.poll())

    # then
    assert [(m, p) for m, p, _, _ in transport.calls].count(("DELETE", path)) == 2
    assert [e.kind for e in events] == ["cancelled"]


def test_cancel_unknown_cancels_only_foreign_orders_of_bot_markets() -> None:
    """At start, old orders of the bot's markets go. Other markets stay."""
    # given
    wallex, transport, _, _ = make()
    orders = [order("old1", symbol="BTCTMN"), order("other", symbol="DOGETMN")]
    transport.add("GET", "/v1/account/openOrders", (200, {"success": True, "result": {"orders": orders}}))
    transport.add("DELETE", "/v1/account/orders/old1", (200, {"success": True, "result": {}}))

    # when
    cancelled = run(wallex.cancel_unknown(["BTCTMN", "ETHTMN"]))

    # then
    assert cancelled == ["old1"]


def test_balances_reads_value_per_asset() -> None:
    """GET /v1/account/balances gives a dict per asset with string values."""
    # given
    wallex, transport, _, _ = make()
    transport.add("GET", "/v1/account/balances", (200, {"success": True, "result": {"balances": {
        "TMN": {"asset": "TMN", "value": "309969", "locked": "0"},
        "USDT": {"asset": "USDT", "value": "0.5", "locked": "0"}}}}))

    # when
    balances = run(wallex.balances())

    # then
    assert balances == {"TMN": 309969.0, "USDT": 0.5}


def test_from_env_without_key_raises(monkeypatch) -> None:
    """The live broker needs WALLEX_API_KEY."""
    # given
    monkeypatch.delenv("WALLEX_API_KEY", raising=False)

    # when / then
    with pytest.raises(RuntimeError):
        wallex_broker.WallexBroker.from_env(RULES)
