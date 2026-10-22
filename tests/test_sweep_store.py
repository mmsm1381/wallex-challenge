"""Tests for the sweep bot SQLite store."""

import pytest

from sweepbot import store


@pytest.fixture
def db() -> store.Store:
    """Returns an in-memory store."""
    return store.Store(":memory:")


def add_order(db: store.Store, order_id: str = "o1", status: str = "open") -> None:
    """Adds one order with fixed values."""
    db.add_order(order_id=order_id, ts=1.0, market="BTCTMN", side="buy",
                 price=100.0, qty=2.0, status=status, reason="sweep",
                 plan_id=None, mode="paper")


def test_order_is_written_and_read_back(db) -> None:
    """An order with no plan reads back with the same values."""
    # given
    add_order(db)

    # when
    rows = db.rows("orders")

    # then
    assert rows == [{"id": "o1", "ts": 1.0, "market": "BTCTMN", "side": "buy",
                     "price": 100.0, "qty": 2.0, "status": "open",
                     "reason": "sweep", "plan_id": None, "mode": "paper"}]


def test_set_order_status_changes_only_that_order(db) -> None:
    """The status of order o1 changes and order o2 stays open."""
    # given
    add_order(db, "o1")
    add_order(db, "o2")

    # when
    db.set_order_status("o1", "filled")

    # then
    assert [r["status"] for r in db.rows("orders")] == ["filled", "open"]


def test_fill_is_written_with_bool_maker_and_returns_id(db) -> None:
    """Two fills get the ids 1 and 2 and maker reads back as a bool."""
    # given
    add_order(db)

    # when
    first = db.add_fill(order_id="o1", ts=2.0, price=100.0, qty=1.0,
                        fee=0.1, maker=True)
    second = db.add_fill(order_id="o1", ts=3.0, price=101.0, qty=1.0,
                         fee=0.2, maker=False)

    # then
    rows = db.rows("fills")
    assert (first, second) == (1, 2)
    assert [r["maker"] for r in rows] == [True, False]
    assert rows[1]["price"] == 101.0


def test_trade_is_written_with_fill_ids_decoded(db) -> None:
    """The fill id lists of a trade read back as Python lists."""
    # given
    trade_id = db.add_trade(
        ts=5.0, market="BTCTMN", mode="paper", entry_fill_ids=[1, 2],
        exit_fill_ids=[3], entry_price=100.0, exit_price=101.0, qty=2.0,
        net_quote=1.5, net_tmn=None, net_pct=0.01, hold_s=30.0,
        exit_reason="target")

    # when
    rows = db.rows("trades")

    # then
    assert trade_id == 1
    assert rows[0]["entry_fill_ids"] == [1, 2]
    assert rows[0]["exit_fill_ids"] == [3]
    assert rows[0]["net_tmn"] is None
    assert rows[0]["exit_reason"] == "target"


def test_plan_is_written_with_bool_accepted(db) -> None:
    """A rejected plan reads back with accepted False and no validated JSON."""
    # given
    plan_id = db.add_plan(ts=1.0, provider="anthropic", model="m",
                          raw_json="{}", validated_json=None, accepted=False,
                          notes="bad")

    # when
    rows = db.rows("plans")

    # then
    assert plan_id == 1
    assert rows[0]["accepted"] is False
    assert rows[0]["validated_json"] is None
    assert rows[0]["notes"] == "bad"


def test_ai_call_is_written_and_read_back(db) -> None:
    """An AI call with an error text reads back with all values."""
    # given
    db.add_ai_call(ts=1.0, job="plan", model="m", input_tokens=10,
                   output_tokens=5, cost_usd=0.01, latency_s=1.5,
                   error="timeout")

    # when
    rows = db.rows("ai_calls")

    # then
    assert rows[0]["job"] == "plan"
    assert rows[0]["input_tokens"] == 10
    assert rows[0]["error"] == "timeout"


def test_filter_event_is_written_and_read_back(db) -> None:
    """A filter start event keeps its filter name, key and kind."""
    # given
    db.add_filter_event(ts=1.0, filter_name="fx", key="USDTTMN", kind="start")

    # when
    rows = db.rows("filter_events")

    # then
    assert rows == [{"id": 1, "ts": 1.0, "filter": "fx", "key": "USDTTMN",
                     "kind": "start"}]


def test_log_fields_are_stored_as_json_and_decoded(db) -> None:
    """The fields dict of a log row reads back as the same dict."""
    # given
    log_id = db.add_log(ts=1.0, kind="trade", text="done",
                        fields={"net": 1.5, "tags": ["a"]})

    # when
    rows = db.rows("logs")

    # then
    assert log_id == 1
    assert rows[0]["fields"] == {"net": 1.5, "tags": ["a"]}
    assert rows[0]["text"] == "done"


def test_ai_cost_total_sums_calls_and_is_zero_when_empty(db) -> None:
    """The total of costs 0.25 and 0.5 is 0.75. An empty table gives 0.0."""
    # given
    empty_total = db.ai_cost_total()
    for cost in (0.25, 0.5):
        db.add_ai_call(ts=1.0, job="plan", model="m", input_tokens=1,
                       output_tokens=1, cost_usd=cost, latency_s=1.0,
                       error=None)

    # when
    total = db.ai_cost_total()

    # then
    assert empty_total == 0.0
    assert total == pytest.approx(0.75)


def test_rows_with_where_filters_by_condition_and_params(db) -> None:
    """A where clause with one parameter returns only the matching order."""
    # given
    add_order(db, "o1", "open")
    add_order(db, "o2", "filled")

    # when
    rows = db.rows("orders", "status = ?", ("filled",))

    # then
    assert [r["id"] for r in rows] == ["o2"]


def test_rows_with_unknown_table_raises(db) -> None:
    """A table name that is not known raises ValueError."""
    # given
    name = "orders; DROP TABLE orders"

    # when / then
    with pytest.raises(ValueError):
        db.rows(name)


def test_file_store_creates_folder_and_data_survives_reopen(tmp_path) -> None:
    """A file path in a new folder is created and the rows stay after reopen."""
    # given
    path = tmp_path / "data" / "sub" / "sweepbot.db"
    first = store.Store(str(path))
    add_order(first)
    first.close()

    # when
    second = store.Store(str(path))

    # then
    assert [r["id"] for r in second.rows("orders")] == ["o1"]
    second.close()
