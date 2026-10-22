"""Tests for the sweep bot risk guard: hard limits, stops and the end of the challenge."""

import calendar
import time

from sweepbot import config
from sweepbot import risk

START_EQUITY = 5_000_000.0


def utc(text: str) -> float:
    """Returns the epoch seconds of an ISO UTC text such as 2026-11-04T20:00:00Z."""
    return float(calendar.timegm(time.strptime(text, "%Y-%m-%dT%H:%M:%SZ")))


def make_guard(end_utc: str = "") -> risk.RiskGuard:
    """Builds a guard with the default limits and the given challenge end."""
    cfg = config.load_config(None)
    cfg["challenge"]["end_utc"] = end_utc
    return risk.RiskGuard(cfg, utc("2026-11-01T08:00:00Z"))


def bid(guard: risk.RiskGuard, **overrides) -> str | None:
    """Calls check_bid with a valid bid, changed by `overrides`."""
    args = {"now": utc("2026-11-01T09:00:00Z"), "market": "BTCTMN",
            "size_tmn": 1_000_000.0, "depth_pct": 1.3, "open_count": 0,
            "usdt_exposure_tmn": 0.0, "equity_tmn": START_EQUITY}
    args.update(overrides)
    return guard.check_bid(**args)


def kinds(events: list[dict]) -> list[str]:
    """Returns the kinds of a list of events."""
    return [event["kind"] for event in events]


def test_tehran_day_before_and_after_local_midnight() -> None:
    """Tehran is UTC+3:30. Local midnight is at 20:30 UTC."""
    # given
    before = utc("2026-11-01T20:29:00Z")
    after = utc("2026-11-01T20:31:00Z")

    # when
    day_before = risk.tehran_day(before)
    day_after = risk.tehran_day(after)

    # then
    assert (day_before, day_after) == ("2026-11-01", "2026-11-02")


def test_parse_utc_reads_iso_text_and_empty_text() -> None:
    """A UTC ISO text gives epoch seconds. An empty text gives None."""
    # given
    text = "2026-11-04T20:00:00Z"

    # when
    value = risk.parse_utc(text)
    empty = risk.parse_utc("")

    # then
    assert value == utc(text)
    assert empty is None


def test_check_bid_allows_a_valid_bid() -> None:
    """A bid inside all limits gives None."""
    # given
    guard = make_guard()

    # when
    reason = bid(guard)

    # then
    assert reason is None


def test_check_bid_rejects_size_above_limit() -> None:
    """A bid of 1,000,001 toman is above max_bid_tmn."""
    # given
    guard = make_guard()

    # when
    reason = bid(guard, size_tmn=1_000_001.0)

    # then
    assert reason is not None
    assert "size" in reason


def test_check_bid_rejects_depth_below_minimum() -> None:
    """A depth of 0.9% is below min_depth_pct 1.0."""
    # given
    guard = make_guard()

    # when
    reason = bid(guard, depth_pct=0.9)

    # then
    assert reason is not None
    assert "depth" in reason


def test_check_bid_rejects_depth_above_maximum() -> None:
    """A depth of 2.6% is above max_depth_pct 2.5."""
    # given
    guard = make_guard()

    # when
    reason = bid(guard, depth_pct=2.6)

    # then
    assert reason is not None
    assert "depth" in reason


def test_check_bid_accepts_depth_on_both_edges() -> None:
    """The depth range is closed: 1.0 and 2.5 are allowed."""
    # given
    guard = make_guard()

    # when
    low = bid(guard, depth_pct=1.0)
    high = bid(guard, depth_pct=2.5)

    # then
    assert (low, high) == (None, None)


def test_check_bid_rejects_when_all_slots_are_used() -> None:
    """With 5 open bids or positions, a sixth bid is not allowed."""
    # given
    guard = make_guard()

    # when
    full = bid(guard, open_count=5)
    free = bid(guard, open_count=4)

    # then
    assert full is not None
    assert "slot" in full
    assert free is None


def test_check_bid_rejects_usdt_share_above_limit() -> None:
    """On a USDT market, exposure 1,500,000 + bid 600,000 > 40% of 5,000,000."""
    # given
    guard = make_guard()

    # when
    over = bid(guard, market="ETHUSDT", size_tmn=600_000.0,
               usdt_exposure_tmn=1_500_000.0)
    edge = bid(guard, market="ETHUSDT", size_tmn=500_000.0,
               usdt_exposure_tmn=1_500_000.0)
    tmn = bid(guard, market="ETHTMN", size_tmn=600_000.0,
              usdt_exposure_tmn=1_500_000.0)

    # then
    assert over is not None
    assert "usdt" in over
    assert edge is None
    assert tmn is None


def test_two_usdt_bids_of_1m_fit_the_40_pct_limit_at_start_equity() -> None:
    """The default plan has two USDT markets of 1M. Both fit at 5M equity."""
    # given
    guard = make_guard()

    # when
    second = bid(guard, market="PEPEUSDT", size_tmn=1_000_000.0,
                 usdt_exposure_tmn=1_000_000.0)

    # then
    assert second is None


def test_daily_stop_trips_and_halts_bids() -> None:
    """A loss of 1.5% from the day start trips the daily stop once.

    Scenario: day start 5,000,000. The stop level is 4,925,000.
    """
    # given
    guard = make_guard()
    now = utc("2026-11-01T09:00:00Z")
    guard.update_equity(now, 5_000_000.0)

    # when
    ok = guard.update_equity(now + 10, 4_925_001.0)
    trip = guard.update_equity(now + 20, 4_925_000.0)
    again = guard.update_equity(now + 30, 4_900_000.0)

    # then
    assert ok == []
    assert kinds(trip) == ["daily_stop"]
    assert trip[0]["ts"] == now + 20
    assert again == []
    assert guard.halted(now + 30) == "daily_loss"
    assert bid(guard, now=now + 30) == "daily_loss"


def test_daily_stop_clears_on_next_tehran_day() -> None:
    """The daily stop ends at 20:30 UTC. The new day starts from the new equity."""
    # given
    guard = make_guard()
    guard.update_equity(utc("2026-11-01T09:00:00Z"), 5_000_000.0)
    guard.update_equity(utc("2026-11-01T10:00:00Z"), 4_900_000.0)
    assert guard.halted(utc("2026-11-01T20:29:00Z")) == "daily_loss"

    # when
    events = guard.update_equity(utc("2026-11-01T20:31:00Z"), 4_900_000.0)

    # then
    assert kinds(events) == ["day_start"]
    assert guard.halted(utc("2026-11-01T20:31:00Z")) is None
    assert kinds(guard.update_equity(utc("2026-11-01T21:00:00Z"), 4_880_000.0)) == []


def test_first_update_emits_day_start() -> None:
    """The first equity of the run starts a Tehran day."""
    # given
    guard = make_guard()

    # when
    events = guard.update_equity(utc("2026-11-01T09:00:00Z"), 5_000_000.0)

    # then
    assert kinds(events) == ["day_start"]


def test_equity_floor_trips_on_drawdown_from_peak() -> None:
    """A fall of 2.5% from the peak trips the equity floor.

    Scenario: peak 5,200,000. The floor is 5,070,000. Day start is 5,100,000,
    so the daily stop level 5,023,500 stays below.
    """
    # given
    guard = make_guard()
    now = utc("2026-11-01T09:00:00Z")
    guard.update_equity(now, 5_100_000.0)
    guard.update_equity(now + 10, 5_200_000.0)

    # when
    ok = guard.update_equity(now + 20, 5_070_001.0)
    trip = guard.update_equity(now + 30, 5_070_000.0)
    again = guard.update_equity(now + 40, 5_030_000.0)

    # then
    assert ok == []
    assert kinds(trip) == ["equity_floor"]
    assert again == []
    assert guard.halted(now + 40) == "equity_floor"
    assert bid(guard, now=now + 40) == "equity_floor"


def test_equity_floor_trips_on_drawdown_from_start() -> None:
    """A fall of 4% from the start equity trips the floor.

    The peak limit is always stricter than the start limit, until a reset
    lowers the peak. Scenario: start 5,000,000. The floor trips at 4,850,000
    (peak). A person resets it. The peak is now 4,850,000, so its level is
    4,728,750. The start level 4,800,000 is higher, so 4,800,000 trips it.
    """
    # given
    guard = make_guard()
    guard.update_equity(utc("2026-11-01T09:00:00Z"), 5_000_000.0)
    guard.update_equity(utc("2026-11-01T10:00:00Z"), 4_850_000.0)
    guard.reset_floor()
    ok = guard.update_equity(utc("2026-11-01T10:00:10Z"), 4_800_001.0)

    # when
    events = guard.update_equity(utc("2026-11-01T10:00:20Z"), 4_800_000.0)

    # then
    assert "equity_floor" not in kinds(ok)
    assert "equity_floor" in kinds(events)
    assert guard.halted(utc("2026-11-01T10:00:20Z")) == "equity_floor"


def test_equity_floor_stays_over_a_new_day_until_reset() -> None:
    """The floor halt does not clear at a new day. reset_floor clears it.

    Scenario: peak 5,200,000, then 5,060,000 trips the floor (5,070,000).
    After the reset the peak is the current equity, so the floor does not trip
    again at once.
    """
    # given
    guard = make_guard()
    guard.update_equity(utc("2026-11-01T09:00:00Z"), 5_200_000.0)
    guard.update_equity(utc("2026-11-01T10:00:00Z"), 5_060_000.0)
    guard.update_equity(utc("2026-11-02T09:00:00Z"), 5_060_000.0)
    assert guard.halted(utc("2026-11-02T09:00:00Z")) == "equity_floor"

    # when
    guard.reset_floor()
    events = guard.update_equity(utc("2026-11-02T09:00:10Z"), 5_060_000.0)

    # then
    assert guard.halted(utc("2026-11-02T09:00:10Z")) is None
    assert "equity_floor" not in kinds(events)


def test_position_too_old_after_max_hold() -> None:
    """A position of 900 s or more is too old."""
    # given
    guard = make_guard()

    # when
    young = guard.position_too_old(1899.0, 1000.0)
    old = guard.position_too_old(1900.0, 1000.0)

    # then
    assert (young, old) == (False, True)


def test_end_of_challenge_halts_bids_and_flattens() -> None:
    """From 2 h before the end, bids stop and the bot sells all coins."""
    # given
    guard = make_guard("2026-11-04T20:00:00Z")
    before = utc("2026-11-04T17:59:59Z")
    start = utc("2026-11-04T18:00:00Z")

    # when / then
    assert guard.halted(before) is None
    assert guard.must_flatten(before) is False
    assert guard.halted(start) == "end_of_challenge"
    assert guard.must_flatten(start) is True
    assert bid(guard, now=start) == "end_of_challenge"


def test_end_after_the_nightly_close_flattens_before_the_usdt_sale() -> None:
    """End at 23:00 UTC: USDTTMN closes at 20:00, the USDT sale is at 19:30.

    Coins of USDT markets give USDT when they are sold. So the bot must sell
    its coins before the USDT sale: from 19:00, not from 21:00 (end - 2 h).
    """
    # given
    guard = make_guard("2026-11-04T23:00:00Z")

    # when
    convert_at = guard.usdt_convert_at()
    flatten_at = guard.flatten_at()

    # then
    assert convert_at == utc("2026-11-04T19:30:00Z")
    assert flatten_at == utc("2026-11-04T19:00:00Z")
    assert guard.must_flatten(utc("2026-11-04T18:59:59Z")) is False
    assert guard.halted(utc("2026-11-04T19:00:00Z")) == "end_of_challenge"


def test_must_flatten_is_false_without_end() -> None:
    """An empty end_utc turns the end schedule off."""
    # given
    guard = make_guard("")

    # when
    flatten = guard.must_flatten(utc("2030-01-01T00:00:00Z"))
    convert = guard.usdt_convert_at()

    # then
    assert flatten is False
    assert convert is None
    assert guard.halted(utc("2030-01-01T00:00:00Z")) is None


def test_usdt_convert_at_for_end_at_the_close() -> None:
    """End at 20:00 UTC is the close of USDTTMN. Convert 30 min before."""
    # given
    guard = make_guard("2026-11-04T20:00:00Z")

    # when
    convert = guard.usdt_convert_at()

    # then
    assert convert == utc("2026-11-04T19:30:00Z")


def test_usdt_convert_at_for_end_in_the_closed_window() -> None:
    """End at 02:00 UTC is in the night. The last close is 20:00 UTC the day before."""
    # given
    guard = make_guard("2026-11-05T02:00:00Z")

    # when
    convert = guard.usdt_convert_at()

    # then
    assert convert == utc("2026-11-04T19:30:00Z")


def test_usdt_convert_at_for_end_in_the_open_window() -> None:
    """End at 12:00 UTC is inside the open window. The close is the end itself."""
    # given
    guard = make_guard("2026-11-04T12:00:00Z")

    # when
    convert = guard.usdt_convert_at()

    # then
    assert convert == utc("2026-11-04T11:30:00Z")
