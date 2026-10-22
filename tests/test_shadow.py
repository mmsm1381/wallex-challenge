"""Tests for the paper strategies: entry fills, exit fills, fees, and PnL."""

import pytest

from lagcatcher import shadow


def make_taker(
    *,
    latency_s: float,
    notional_tmn: float,
    min_gap_pct: float,
    buffer_pct: float,
    tmn_fees: tuple[float, float] = (0.0025, 0.003),
    usdt_fees: tuple[float, float] = (0.001, 0.0013),
    exit_latency_s: float = 0.5,
    max_hold_s: float = 900.0,
    stop_pct: float = 1.0,
    stop_confirm_s: float = 0.0,
    cooldown_s: float = 0.0,
    max_entry_spread_pct: float = 100.0,
) -> shadow.TakerStrategy:
    """Builds one taker strategy with a large order budget."""
    return shadow.TakerStrategy(
        name="taker",
        latency_s=latency_s,
        notional_tmn=notional_tmn,
        min_gap_pct=min_gap_pct,
        buffer_pct=buffer_pct,
        fees={"TMN": tmn_fees, "USDT": usdt_fees},
        exit_rules=shadow.ExitRules(
            max_hold_s=max_hold_s, stop_pct=stop_pct, requote_bps=5.0,
            latency_s=exit_latency_s, stop_confirm_s=stop_confirm_s,
            cooldown_s=cooldown_s),
        bucket=shadow.TokenBucket(capacity=1000, per_s=10.0),
        min_fill_fraction=0.5,
        max_entry_spread_pct=max_entry_spread_pct,
    )


def make_resting(
    *,
    latency_s: float,
    notional_tmn: float,
    buffer_pct: float,
    tmn_fees: tuple[float, float] = (0.0025, 0.003),
) -> shadow.RestingBidStrategy:
    """Builds one resting-bid strategy with a large order budget."""
    return shadow.RestingBidStrategy(
        name="resting",
        latency_s=latency_s,
        notional_tmn=notional_tmn,
        buffer_pct=buffer_pct,
        requote_bps=5.0,
        fees={"TMN": tmn_fees, "USDT": (0.001, 0.0013)},
        exit_rules=shadow.ExitRules(
            max_hold_s=900.0, stop_pct=1.0, requote_bps=5.0, latency_s=0.5),
        bucket=shadow.TokenBucket(capacity=1000, per_s=10.0),
        min_fill_fraction=0.5,
    )


def test_taker_with_ask_still_there_after_latency_fills_at_ask() -> None:
    """A 9% gap gives a signal, and the ask is still there after the latency."""
    # given
    taker = make_taker(latency_s=1.0, notional_tmn=1_000_000.0,
                       min_gap_pct=0.6, buffer_pct=0.3)
    book = shadow.Book(bids=[(95.0, 1e9)], asks=[(100.0, 1e9)], ts=0.0)
    taker.on_tick(market="PEPETMN", now=0.0, book=book, fv=110.0, usdttmn=None)

    # when
    events = taker.on_timer(now=1.0, books={"PEPETMN": book},
                            fvs={"PEPETMN": 110.0}, usdttmn=None)

    # then
    assert events[0]["type"] == "fill"
    assert events[0]["price"] == 100.0
    assert events[0]["qty"] == 10000.0


def test_taker_with_ask_moved_above_limit_records_miss() -> None:
    """Other traders take the cheap ask during our latency, so we miss."""
    # given
    taker = make_taker(latency_s=1.0, notional_tmn=1_000_000.0,
                       min_gap_pct=0.6, buffer_pct=0.3)
    signal_book = shadow.Book(bids=[(95.0, 1e9)], asks=[(100.0, 1e9)], ts=0.0)
    later_book = shadow.Book(bids=[(95.0, 1e9)], asks=[(105.0, 1e9)], ts=1.0)
    taker.on_tick(market="PEPETMN", now=0.0, book=signal_book, fv=110.0,
                  usdttmn=None)

    # when
    events = taker.on_timer(now=1.0, books={"PEPETMN": later_book},
                            fvs={"PEPETMN": 110.0}, usdttmn=None)

    # then
    assert len(events) == 1
    assert events[0]["type"] == "miss"
    assert events[0]["reason"] == "ask_moved"


def test_taker_with_gap_below_fee_hurdle_sends_no_signal() -> None:
    """A 0.5% gap is below the 0.85% hurdle (0.30 + 0.25 fees + 0.30 buffer)."""
    # given
    taker = make_taker(latency_s=1.0, notional_tmn=1_000_000.0,
                       min_gap_pct=0.6, buffer_pct=0.3)
    book = shadow.Book(bids=[(99.0, 1e9)], asks=[(100.0, 1e9)], ts=0.0)

    # when
    events = taker.on_tick(market="PEPETMN", now=0.0, book=book, fv=100.5,
                           usdttmn=None)

    # then
    assert events == []


def test_taker_with_spread_above_limit_sends_no_signal() -> None:
    """The ask is 9% below fair value, but the spread is 3% and the limit is 0.5%.

    Expected: no signal, because a stop would sell near the far bid.
    """
    # given
    taker = make_taker(latency_s=0.3, notional_tmn=1_000_000.0, min_gap_pct=0.6,
                       buffer_pct=0.3, max_entry_spread_pct=0.5)
    book = shadow.Book(bids=[(97.0, 1e9)], asks=[(100.0, 1e9)], ts=0.0)

    # when
    events = taker.on_tick(market="PEPETMN", now=0.0, book=book, fv=110.0,
                           usdttmn=None)

    # then
    assert events == []


def test_taker_with_part_depth_at_limit_fills_available_qty_at_vwap() -> None:
    """Only 6000 of 10000 units are at or below the limit, which is above 50%."""
    # given
    taker = make_taker(latency_s=1.0, notional_tmn=1_000_000.0,
                       min_gap_pct=0.6, buffer_pct=0.3)
    signal_book = shadow.Book(bids=[(95.0, 1e9)], asks=[(100.0, 1e9)], ts=0.0)
    fill_book = shadow.Book(
        bids=[(95.0, 1e9)],
        asks=[(99.0, 3000.0), (100.0, 3000.0), (101.0, 1e9)],
        ts=1.0)
    taker.on_tick(market="PEPETMN", now=0.0, book=signal_book, fv=110.0,
                  usdttmn=None)

    # when
    events = taker.on_timer(now=1.0, books={"PEPETMN": fill_book},
                            fvs={"PEPETMN": 110.0}, usdttmn=None)

    # then
    assert events[0]["type"] == "fill"
    assert events[0]["qty"] == 6000.0
    assert events[0]["price"] == 99.5


def test_taker_with_depth_below_half_at_limit_records_thin_depth_miss() -> None:
    """Only 4000 of 10000 units are at the limit, which is below 50%."""
    # given
    taker = make_taker(latency_s=1.0, notional_tmn=1_000_000.0,
                       min_gap_pct=0.6, buffer_pct=0.3)
    signal_book = shadow.Book(bids=[(95.0, 1e9)], asks=[(100.0, 1e9)], ts=0.0)
    fill_book = shadow.Book(bids=[(95.0, 1e9)],
                            asks=[(100.0, 4000.0), (105.0, 1e9)], ts=1.0)
    taker.on_tick(market="PEPETMN", now=0.0, book=signal_book, fv=110.0,
                  usdttmn=None)

    # when
    events = taker.on_timer(now=1.0, books={"PEPETMN": fill_book},
                            fvs={"PEPETMN": 110.0}, usdttmn=None)

    # then
    assert len(events) == 1
    assert events[0]["type"] == "miss"
    assert events[0]["reason"] == "thin_depth"


def test_resting_bid_with_sell_trade_below_bid_fills_at_bid_price() -> None:
    """The bid rests at 99.0 (fair value 100 minus 1%). A sell prints at 98.5."""
    # given
    resting = make_resting(latency_s=0.5, notional_tmn=990_000.0, buffer_pct=0.5)
    book = shadow.Book(bids=[(98.0, 1e9)], asks=[(101.0, 1e9)], ts=0.0)
    resting.on_tick(market="PEPETMN", now=0.0, book=book, fv=100.0, usdttmn=None)
    resting.on_tick(market="PEPETMN", now=0.6, book=book, fv=100.0, usdttmn=None)

    # when
    events = resting.on_trade(market="PEPETMN", now=1.0, price=98.5, qty=1e9,
                              buyer_maker=True, usdttmn=None)

    # then
    assert events[0]["type"] == "fill"
    assert events[0]["price"] == pytest.approx(99.0)
    assert events[0]["qty"] == pytest.approx(10000.0)
    assert events[0]["maker"] is True


def test_resting_bid_with_trade_at_equal_price_does_not_fill() -> None:
    """A trade at exactly our price does not prove a fill, as the queue is unknown."""
    # given
    resting = make_resting(latency_s=0.5, notional_tmn=990_000.0, buffer_pct=0.5)
    book = shadow.Book(bids=[(98.0, 1e9)], asks=[(101.0, 1e9)], ts=0.0)
    resting.on_tick(market="PEPETMN", now=0.0, book=book, fv=100.0, usdttmn=None)
    resting.on_tick(market="PEPETMN", now=0.6, book=book, fv=100.0, usdttmn=None)

    # when
    events = resting.on_trade(market="PEPETMN", now=1.0, price=99.0, qty=1e9,
                              buyer_maker=True, usdttmn=None)

    # then
    assert events == []


def test_resting_bid_with_buy_initiated_trade_below_bid_does_not_fill() -> None:
    """A buyer took an ask, so no seller reached our bid."""
    # given
    resting = make_resting(latency_s=0.5, notional_tmn=990_000.0, buffer_pct=0.5)
    book = shadow.Book(bids=[(98.0, 1e9)], asks=[(101.0, 1e9)], ts=0.0)
    resting.on_tick(market="PEPETMN", now=0.0, book=book, fv=100.0, usdttmn=None)
    resting.on_tick(market="PEPETMN", now=0.6, book=book, fv=100.0, usdttmn=None)

    # when
    events = resting.on_trade(market="PEPETMN", now=1.0, price=98.5, qty=1e9,
                              buyer_maker=False, usdttmn=None)

    # then
    assert events == []


def test_resting_bid_requote_keeps_old_price_live_until_latency_passes() -> None:
    """Fair value drops to 98, but the old 99.0 bid is still live for 0.5 s.

    Scenario:
        - The bid rests at 99.0. Fair value then drops to 98.0 at t=1.0.
        - The new bid of 97.02 is live only at t=1.5.
    Expected: a sell at 98.5 at t=1.2 fills the old bid at 99.0.
    """
    # given
    resting = make_resting(latency_s=0.5, notional_tmn=990_000.0, buffer_pct=0.5)
    book = shadow.Book(bids=[(97.0, 1e9)], asks=[(101.0, 1e9)], ts=0.0)
    resting.on_tick(market="PEPETMN", now=0.0, book=book, fv=100.0, usdttmn=None)
    resting.on_tick(market="PEPETMN", now=0.6, book=book, fv=100.0, usdttmn=None)
    resting.on_tick(market="PEPETMN", now=1.0, book=book, fv=98.0, usdttmn=None)

    # when
    events = resting.on_trade(market="PEPETMN", now=1.2, price=98.5, qty=1e9,
                              buyer_maker=True, usdttmn=None)

    # then
    assert events[0]["type"] == "fill"
    assert events[0]["price"] == pytest.approx(99.0)


def test_resting_bid_marketable_at_activation_fills_as_taker_at_ask() -> None:
    """The ask falls to 98.8 before our 99.0 bid is live, so the bid takes it."""
    # given
    resting = make_resting(latency_s=0.5, notional_tmn=990_000.0, buffer_pct=0.5)
    first_book = shadow.Book(bids=[(98.0, 1e9)], asks=[(101.0, 1e9)], ts=0.0)
    later_book = shadow.Book(bids=[(98.0, 1e9)], asks=[(98.8, 1e9)], ts=0.6)
    resting.on_tick(market="PEPETMN", now=0.0, book=first_book, fv=100.0,
                    usdttmn=None)

    # when
    events = resting.on_tick(market="PEPETMN", now=0.6, book=later_book,
                             fv=100.0, usdttmn=None)

    # then
    assert events[0]["type"] == "fill"
    assert events[0]["price"] == 98.8
    assert events[0]["maker"] is False


def test_exit_with_buy_trade_above_target_closes_as_maker_with_maker_fee() -> None:
    """Entry at 100 (taker 0.30%), exit at the 110 target (maker 0.25%).

    Scenario:
        - Gross profit is 10000 x 10 = 100000 toman.
        - Fees are 3000 (entry) and 2750 (exit).
    Expected: net 94250 toman, which is 9.425% of the entry value.
    """
    # given
    taker = make_taker(latency_s=1.0, notional_tmn=1_000_000.0,
                       min_gap_pct=0.6, buffer_pct=0.3, exit_latency_s=0.5)
    book = shadow.Book(bids=[(99.5, 1e9)], asks=[(100.0, 1e9)], ts=0.0)
    books = {"PEPETMN": book}
    fvs = {"PEPETMN": 110.0}
    taker.on_tick(market="PEPETMN", now=0.0, book=book, fv=110.0, usdttmn=None)
    taker.on_timer(now=1.0, books=books, fvs=fvs, usdttmn=None)
    taker.on_timer(now=2.0, books=books, fvs=fvs, usdttmn=None)
    taker.on_timer(now=3.0, books=books, fvs=fvs, usdttmn=None)

    # when
    events = taker.on_trade(market="PEPETMN", now=4.0, price=110.5, qty=1e9,
                            buyer_maker=False, usdttmn=None)

    # then
    assert events[0]["type"] == "exit"
    assert events[0]["reason"] == "target"
    assert events[0]["maker"] is True
    assert events[0]["price"] == 110.0
    assert events[0]["net_tmn"] == pytest.approx(94250.0)
    assert events[0]["net_pct"] == pytest.approx(9.425)


def test_exit_with_buy_trade_at_target_price_does_not_close() -> None:
    """A trade at exactly the target does not prove our ask was filled."""
    # given
    taker = make_taker(latency_s=1.0, notional_tmn=1_000_000.0,
                       min_gap_pct=0.6, buffer_pct=0.3, exit_latency_s=0.5)
    book = shadow.Book(bids=[(99.5, 1e9)], asks=[(100.0, 1e9)], ts=0.0)
    books = {"PEPETMN": book}
    fvs = {"PEPETMN": 110.0}
    taker.on_tick(market="PEPETMN", now=0.0, book=book, fv=110.0, usdttmn=None)
    taker.on_timer(now=1.0, books=books, fvs=fvs, usdttmn=None)
    taker.on_timer(now=2.0, books=books, fvs=fvs, usdttmn=None)
    taker.on_timer(now=3.0, books=books, fvs=fvs, usdttmn=None)

    # when
    events = taker.on_trade(market="PEPETMN", now=4.0, price=110.0, qty=1e9,
                            buyer_maker=False, usdttmn=None)

    # then
    assert events == []


def test_exit_with_bid_below_stop_closes_as_taker_at_bid() -> None:
    """The bid falls to 98.9, below the 1% stop at 99.0.

    Scenario:
        - Gross loss is 10000 x 1.1 = 11000 toman.
        - Fees are 3000 (entry) and 2967 (exit taker 0.30% of 989000).
    Expected: net -16967 toman.
    """
    # given
    taker = make_taker(latency_s=1.0, notional_tmn=1_000_000.0,
                       min_gap_pct=0.6, buffer_pct=0.3, stop_pct=1.0)
    entry_book = shadow.Book(bids=[(99.5, 1e9)], asks=[(100.0, 1e9)], ts=0.0)
    taker.on_tick(market="PEPETMN", now=0.0, book=entry_book, fv=110.0,
                  usdttmn=None)
    taker.on_timer(now=1.0, books={"PEPETMN": entry_book},
                   fvs={"PEPETMN": 110.0}, usdttmn=None)
    falling_book = shadow.Book(bids=[(98.9, 1e9)], asks=[(99.0, 1e9)], ts=2.0)

    # when
    events = taker.on_tick(market="PEPETMN", now=2.0, book=falling_book,
                           fv=110.0, usdttmn=None)

    # then
    assert events[0]["type"] == "exit"
    assert events[0]["reason"] == "stop"
    assert events[0]["maker"] is False
    assert events[0]["price"] == 98.9
    assert events[0]["net_tmn"] == pytest.approx(-16967.0)


def _open_taker_position(**settings) -> shadow.TakerStrategy:
    """Returns a taker with a PEPETMN position bought at 100 at t=1."""
    taker = make_taker(latency_s=1.0, notional_tmn=1_000_000.0,
                       min_gap_pct=0.6, buffer_pct=0.3, stop_pct=1.0, **settings)
    entry_book = shadow.Book(bids=[(99.5, 1e9)], asks=[(100.0, 1e9)], ts=0.0)
    taker.on_tick(market="PEPETMN", now=0.0, book=entry_book, fv=110.0,
                  usdttmn=None)
    taker.on_timer(now=1.0, books={"PEPETMN": entry_book},
                   fvs={"PEPETMN": 110.0}, usdttmn=None)
    return taker


def test_exit_with_short_low_bid_and_stop_confirm_does_not_stop() -> None:
    """The bid falls below the 99.0 stop for 0.2 s only, then comes back.

    Scenario:
        - The stop needs a low bid for 1.0 s.
        - The bid is 98.9 at t=2.0 and t=2.2, and 99.5 at t=2.3.
    Expected: no exit, and the next low bid starts a new 1.0 s wait.
    """
    # given
    taker = _open_taker_position(stop_confirm_s=1.0)
    low_book = shadow.Book(bids=[(98.9, 1e9)], asks=[(99.6, 1e9)], ts=2.0)
    normal_book = shadow.Book(bids=[(99.5, 1e9)], asks=[(99.6, 1e9)], ts=2.3)

    # when
    events = taker.on_tick(market="PEPETMN", now=2.0, book=low_book, fv=110.0,
                           usdttmn=None)
    events += taker.on_tick(market="PEPETMN", now=2.2, book=low_book, fv=110.0,
                            usdttmn=None)
    events += taker.on_tick(market="PEPETMN", now=2.3, book=normal_book,
                            fv=110.0, usdttmn=None)
    events += taker.on_tick(market="PEPETMN", now=3.1, book=low_book, fv=110.0,
                            usdttmn=None)

    # then
    assert [e for e in events if e["type"] == "exit"] == []


def test_exit_with_low_bid_for_stop_confirm_time_stops_at_bid() -> None:
    """The bid stays at 98.9, below the 99.0 stop, from t=2.0 to t=3.0.

    Expected: the stop sells at 98.9 at t=3.0.
    """
    # given
    taker = _open_taker_position(stop_confirm_s=1.0)
    low_book = shadow.Book(bids=[(98.9, 1e9)], asks=[(99.6, 1e9)], ts=2.0)
    taker.on_tick(market="PEPETMN", now=2.0, book=low_book, fv=110.0,
                  usdttmn=None)

    # when
    events = taker.on_tick(market="PEPETMN", now=3.0, book=low_book, fv=110.0,
                           usdttmn=None)

    # then
    assert events[0]["reason"] == "stop"
    assert events[0]["price"] == 98.9


def test_exit_with_fair_value_below_stop_stops_at_once() -> None:
    """Fair value falls to 98.0, below the 99.0 stop. The bid is still 99.5.

    Expected: the stop sells at the 99.5 bid without the confirm wait.
    """
    # given
    taker = _open_taker_position(stop_confirm_s=1.0)
    book = shadow.Book(bids=[(99.5, 1e9)], asks=[(99.6, 1e9)], ts=2.0)

    # when
    events = taker.on_tick(market="PEPETMN", now=2.0, book=book, fv=98.0,
                           usdttmn=None)

    # then
    assert events[0]["reason"] == "stop"
    assert events[0]["price"] == 99.5


def test_taker_after_stop_waits_for_cooldown_before_next_signal() -> None:
    """A stop at t=2 starts a 300 s cooldown. The gap is still large.

    Expected: no signal at t=100, and a signal at t=302.
    """
    # given
    taker = _open_taker_position(cooldown_s=300.0)
    low_book = shadow.Book(bids=[(98.9, 1e9)], asks=[(99.0, 1e9)], ts=2.0)
    taker.on_tick(market="PEPETMN", now=2.0, book=low_book, fv=110.0,
                  usdttmn=None)

    # when
    early = taker.on_tick(market="PEPETMN", now=100.0, book=low_book, fv=110.0,
                          usdttmn=None)
    late = taker.on_tick(market="PEPETMN", now=302.0, book=low_book, fv=110.0,
                         usdttmn=None)

    # then
    assert early == []
    assert late[0]["type"] == "signal"


def test_exit_after_max_hold_closes_as_taker_at_bid() -> None:
    """After 900 s the position closes at the 100.5 bid.

    Scenario:
        - Gross profit is 10000 x 0.5 = 5000 toman.
        - Fees are 3000 (entry) and 3015 (exit taker 0.30% of 1005000).
    Expected: net -1015 toman.
    """
    # given
    taker = make_taker(latency_s=1.0, notional_tmn=1_000_000.0,
                       min_gap_pct=0.6, buffer_pct=0.3, max_hold_s=900.0)
    entry_book = shadow.Book(bids=[(99.5, 1e9)], asks=[(100.0, 1e9)], ts=0.0)
    taker.on_tick(market="PEPETMN", now=0.0, book=entry_book, fv=110.0,
                  usdttmn=None)
    taker.on_timer(now=1.0, books={"PEPETMN": entry_book},
                   fvs={"PEPETMN": 110.0}, usdttmn=None)
    late_book = shadow.Book(bids=[(100.5, 1e9)], asks=[(100.6, 1e9)], ts=901.0)

    # when
    events = taker.on_tick(market="PEPETMN", now=901.0, book=late_book,
                           fv=None, usdttmn=None)

    # then
    assert events[0]["type"] == "exit"
    assert events[0]["reason"] == "time"
    assert events[0]["net_tmn"] == pytest.approx(-1015.0)


def test_exit_for_usdt_pair_converts_net_to_toman_at_exit_rate() -> None:
    """A USDT pair reports PnL in toman at the USDTTMN rate of the exit.

    Scenario:
        - 1M toman at 250000 toman/USDT buys 8 XRP at 0.5 USDT.
        - The exit at 0.6 nets 0.8 - 0.0052 - 0.0048 = 0.79 USDT.
    Expected: 0.79 USDT x 260000 = 205400 toman.
    """
    # given
    taker = make_taker(latency_s=1.0, notional_tmn=1_000_000.0,
                       min_gap_pct=0.6, buffer_pct=0.3,
                       usdt_fees=(0.001, 0.0013), exit_latency_s=0.5)
    book = shadow.Book(bids=[(0.499, 1e9)], asks=[(0.5, 1e9)], ts=0.0)
    books = {"XRPUSDT": book}
    fvs = {"XRPUSDT": 0.6}
    taker.on_tick(market="XRPUSDT", now=0.0, book=book, fv=0.6, usdttmn=250000.0)
    taker.on_timer(now=1.0, books=books, fvs=fvs, usdttmn=250000.0)
    taker.on_timer(now=2.0, books=books, fvs=fvs, usdttmn=250000.0)
    taker.on_timer(now=3.0, books=books, fvs=fvs, usdttmn=250000.0)

    # when
    events = taker.on_trade(market="XRPUSDT", now=4.0, price=0.61, qty=1e9,
                            buyer_maker=False, usdttmn=260000.0)

    # then
    assert events[0]["net_quote"] == pytest.approx(0.79)
    assert events[0]["net_tmn"] == pytest.approx(205400.0)


def test_token_bucket_with_capacity_used_refuses_until_refill() -> None:
    """Two actions per 10 s: the third action at t=0 waits for the refill."""
    # given
    bucket = shadow.TokenBucket(capacity=2, per_s=10.0)
    bucket.take(now=0.0)
    bucket.take(now=0.0)

    # when
    third_now = bucket.take(now=0.0)
    after_refill = bucket.take(now=5.0)

    # then
    assert third_now is False
    assert after_refill is True
