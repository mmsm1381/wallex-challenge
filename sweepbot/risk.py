"""Risk guard of the sweep bot: the hard limits of DESIGN section 7.

The guard keeps no orders. The caller gives it the equity and the state of a
new bid. The guard tracks:

* the equity at the start of each Tehran day, and the daily loss stop,
* the peak equity, and the equity floor halt,
* the end of the challenge: the stop of new bids, the sale of all coins and
  the time to convert USDT to toman.

All times are UTC epoch seconds.
"""

import calendar
import datetime
import time

TEHRAN_OFFSET_S = 12600  # Tehran is UTC+3:30, no daylight saving.

_USDTTMN_OPEN_S = 8 * 3600  # USDTTMN opens at 08:00 UTC.
_USDTTMN_CLOSE_S = 20 * 3600  # USDTTMN closes at 20:00 UTC.
_DAY_S = 86400


def tehran_day(ts: float) -> str:
    """Returns the Tehran date of `ts` as "YYYY-MM-DD"."""
    shifted = datetime.datetime.fromtimestamp(
        ts + TEHRAN_OFFSET_S, tz=datetime.timezone.utc)
    return shifted.strftime("%Y-%m-%d")


def parse_utc(text: str) -> float | None:
    """Parses a text such as "2026-11-04T20:00:00Z" into epoch seconds.

    Args:
        text: An ISO 8601 UTC time with a "Z" at the end. May be empty.

    Returns:
        The epoch seconds, or None when `text` is empty.
    """
    if not text:
        return None
    return float(calendar.timegm(time.strptime(text, "%Y-%m-%dT%H:%M:%SZ")))


class RiskGuard:
    """Checks the hard limits before each bid and tracks the equity stops."""

    def __init__(self, cfg: dict, start_ts: float) -> None:
        """Builds the guard.

        Args:
            cfg: The config dict from `sweepbot.config.load_config`.
            start_ts: The start time of the run.
        """
        self.limits = cfg["limits"]
        self.start_ts = start_ts
        self.start_equity = float(cfg["challenge"]["start_equity_tmn"])
        self.end_ts = parse_utc(cfg["challenge"]["end_utc"])
        # The USDT sale starts this long before the USDTTMN close, and the
        # coins are sold this long before the USDT sale.
        self.usdt_sell_before_close_s = float(self.limits["usdt_sell_before_close_s"])
        self.flatten_before_usdt_s = float(self.limits["flatten_before_usdt_s"])
        self.peak = self.start_equity
        self.last_equity = self.start_equity
        self.day: str | None = None
        self.day_start_equity = self.start_equity
        self.daily_stopped = False
        self.floor_tripped = False

    def update_equity(self, now: float, equity_tmn: float) -> list[dict]:
        """Records the equity and returns the new events.

        Args:
            now: The time of the equity value.
            equity_tmn: The equity in toman.

        Returns:
            New events, each {"ts", "kind", "text"}. The kinds are "day_start",
            "daily_stop" and "equity_floor".
        """
        events: list[dict] = []
        day = tehran_day(now)
        if day != self.day:
            self.day = day
            self.day_start_equity = equity_tmn
            self.daily_stopped = False
            events.append(self._event(
                now, "day_start",
                f"Tehran day {day} starts at equity {equity_tmn:,.0f}."))
        self.last_equity = equity_tmn
        self.peak = max(self.peak, equity_tmn)

        daily_level = self.day_start_equity * (
            1 - self.limits["daily_loss_pct"] / 100)
        if not self.daily_stopped and equity_tmn <= daily_level:
            self.daily_stopped = True
            events.append(self._event(
                now, "daily_stop",
                f"Equity {equity_tmn:,.0f} is at or below the daily level "
                f"{daily_level:,.0f}."))

        peak_level = self.peak * (1 - self.limits["peak_drawdown_pct"] / 100)
        start_level = self.start_equity * (
            1 - self.limits["start_drawdown_pct"] / 100)
        if not self.floor_tripped and equity_tmn <= max(peak_level, start_level):
            self.floor_tripped = True
            events.append(self._event(
                now, "equity_floor",
                f"Equity {equity_tmn:,.0f} is at or below the floor "
                f"{max(peak_level, start_level):,.0f}."))
        return events

    def reset_floor(self) -> None:
        """Clears the equity floor halt. A person calls this.

        The peak becomes the last equity, so the peak limit starts again from
        here. The start limit still applies.
        """
        self.floor_tripped = False
        self.peak = self.last_equity

    def halted(self, now: float) -> str | None:
        """Returns the reason why no new bid is allowed, or None.

        The reasons are "equity_floor", "daily_loss" and "end_of_challenge".
        """
        if self.floor_tripped:
            return "equity_floor"
        if self.daily_stopped:
            return "daily_loss"
        if self.must_flatten(now):
            return "end_of_challenge"
        return None

    def check_bid(self, *, now: float, market: str, size_tmn: float,
                  depth_pct: float, open_count: int, usdt_exposure_tmn: float,
                  equity_tmn: float) -> str | None:
        """Checks a new bid against the hard limits.

        Args:
            now: The current time.
            market: The market of the bid, such as "ETHUSDT".
            size_tmn: The size of the bid in toman.
            depth_pct: The depth of the bid below the fair value, in percent.
            open_count: Open bids plus positions, before this bid.
            usdt_exposure_tmn: Open bids and positions on USDT markets, in
                toman.
            equity_tmn: The current equity in toman.

        Returns:
            None when the bid is allowed, else a short reason.
        """
        halt = self.halted(now)
        if halt is not None:
            return halt
        limits = self.limits
        if size_tmn > limits["max_bid_tmn"]:
            return f"size {size_tmn:,.0f} above {limits['max_bid_tmn']:,.0f}"
        if not limits["min_depth_pct"] <= depth_pct <= limits["max_depth_pct"]:
            return (f"depth {depth_pct} outside "
                    f"[{limits['min_depth_pct']}, {limits['max_depth_pct']}]")
        if open_count >= limits["max_slots"]:
            return f"slots full: {open_count} of {limits['max_slots']}"
        if market.endswith("USDT"):
            cap = limits["max_usdt_share"] * equity_tmn
            if usdt_exposure_tmn + size_tmn > cap:
                return f"usdt share above cap {cap:,.0f}"
        return None

    def position_too_old(self, now: float, entry_ts: float) -> bool:
        """Returns True when a position is older than the hold limit."""
        return now - entry_ts >= self.limits["max_hold_s"]

    def flatten_at(self) -> float | None:
        """Returns the time from which the bot stops bids and sells all coins.

        It is `stop_before_end_s` before the end, and at the latest
        `flatten_before_usdt_s` before the USDT sale. Coins of USDT markets give USDT when
        they are sold, so they must be sold before the USDT sale.
        """
        if self.end_ts is None:
            return None
        return min(self.end_ts - self.limits["stop_before_end_s"],
                   self.usdt_convert_at() - self.flatten_before_usdt_s)

    def must_flatten(self, now: float) -> bool:
        """Returns True from `flatten_at`: no new bids, sell all coins."""
        flatten_at = self.flatten_at()
        return flatten_at is not None and now >= flatten_at

    def usdt_convert_at(self) -> float | None:
        """Returns the time to sell all USDT for toman, or None without an end.

        USDTTMN is open from 08:00 to 20:00 UTC. The close is the end of the
        challenge when it falls in an open window, else the last 20:00 UTC
        at or before the end. The result is `usdt_sell_before_close_s`
        before that close.
        """
        if self.end_ts is None:
            return None
        second_of_day = self.end_ts % _DAY_S
        day_start = self.end_ts - second_of_day
        if _USDTTMN_OPEN_S <= second_of_day < _USDTTMN_CLOSE_S:
            close = self.end_ts
        elif second_of_day >= _USDTTMN_CLOSE_S:
            close = day_start + _USDTTMN_CLOSE_S
        else:
            close = day_start - _DAY_S + _USDTTMN_CLOSE_S
        return close - self.usdt_sell_before_close_s

    @staticmethod
    def _event(now: float, kind: str, text: str) -> dict:
        """Builds an event dict."""
        return {"ts": now, "kind": kind, "text": text}
