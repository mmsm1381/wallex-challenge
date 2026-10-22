"""Fast filters of the sweep bot (DESIGN section 6). No AI is involved.

* Global drop: the global mid of a coin fell `global_drop_pct` or more
  within `global_window_s`. All bids on the markets of that coin must go.
  The filter ends `global_wait_s` after the last time the drop held.
* FX shock: the USDT rate moved `fx_move_pct` or more (up or down) within
  `fx_window_s`. All bids on toman markets must go. The filter ends
  `fx_wait_s` after the last time the move held. During the nightly
  USDTTMN close, the caller gives the implied rate instead of the mid.

The candle study (RESEARCH.md section 3.2) chose both thresholds. The
executor feeds a sample on every 100 ms timer tick and reads `blocks`.
`update` returns start and end events for the `filter_events` table.
"""

import collections

GLOBAL_DROP = "global_drop"
FX_SHOCK = "fx_shock"
FX_KEY = "USDTTMN"


class RollingExtremes:
    """Minimum and maximum of the samples in a time window, in O(1) per sample."""

    def __init__(self, window_s: float) -> None:
        """Creates an empty window of `window_s` seconds."""
        self._window = window_s
        self._max: collections.deque[tuple[float, float]] = collections.deque()
        self._min: collections.deque[tuple[float, float]] = collections.deque()

    def add(self, ts: float, value: float) -> None:
        """Adds one sample and drops the samples that left the window."""
        while self._max and self._max[-1][1] <= value:
            self._max.pop()
        self._max.append((ts, value))
        while self._min and self._min[-1][1] >= value:
            self._min.pop()
        self._min.append((ts, value))
        for side in (self._max, self._min):
            while side and ts - side[0][0] > self._window:
                side.popleft()

    def high(self) -> float | None:
        """Returns the largest sample in the window, or None."""
        return self._max[0][1] if self._max else None

    def low(self) -> float | None:
        """Returns the smallest sample in the window, or None."""
        return self._min[0][1] if self._min else None


class Filters:
    """State of the global drop filter per coin and of the FX shock filter."""

    def __init__(self, cfg: dict) -> None:
        """Reads the thresholds from `cfg["filters"]`."""
        filters = cfg["filters"]
        self._drop = filters["global_drop_pct"] / 100
        self._global_window = filters["global_window_s"]
        self._global_wait = filters["global_wait_s"]
        self._fx_move = filters["fx_move_pct"] / 100
        self._fx_window = filters["fx_window_s"]
        self._fx_wait = filters["fx_wait_s"]
        self._global: dict[str, RollingExtremes] = {}
        self._fx = RollingExtremes(self._fx_window)
        # Key -> the time at which the filter ends. A key is a coin or FX_KEY.
        self._until: dict[tuple[str, str], float] = {}
        self._pending: list[dict] = []

    def _trip(self, name: str, key: str, now: float, move_pct: float) -> None:
        """Starts or extends one filter."""
        if (name, key) not in self._until:
            self._pending.append({"ts": now, "filter": name, "key": key,
                                  "kind": "start", "move_pct": round(move_pct, 4)})
        wait = self._global_wait if name == GLOBAL_DROP else self._fx_wait
        self._until[(name, key)] = now + wait

    def on_global_mid(self, coin: str, mid: float, now: float) -> None:
        """Adds a global mid sample of `coin` and trips the drop filter."""
        window = self._global.setdefault(coin, RollingExtremes(self._global_window))
        window.add(now, mid)
        high = window.high()
        if high and 1 - mid / high >= self._drop:
            self._trip(GLOBAL_DROP, coin, now, 100 * (mid / high - 1))

    def on_usdt_rate(self, rate: float, now: float) -> None:
        """Adds a USDT rate sample and trips the FX filter on a large move.

        The move compares the new rate with the highest and the lowest rate
        in the window, so a fall and a rise both count.
        """
        self._fx.add(now, rate)
        high, low = self._fx.high(), self._fx.low()
        if 1 - rate / high >= self._fx_move:
            self._trip(FX_SHOCK, FX_KEY, now, 100 * (rate / high - 1))
        elif rate / low - 1 >= self._fx_move:
            self._trip(FX_SHOCK, FX_KEY, now, 100 * (rate / low - 1))

    def update(self, now: float) -> list[dict]:
        """Ends the filters whose wait is over and returns the new events."""
        for (name, key), until in list(self._until.items()):
            if now >= until:
                del self._until[(name, key)]
                self._pending.append({"ts": now, "filter": name, "key": key,
                                      "kind": "end"})
        events, self._pending = self._pending, []
        return events

    def active(self) -> list[tuple[str, str]]:
        """Returns the (filter, key) pairs that are on now."""
        return sorted(self._until)

    def blocks(self, market: str, coin: str, is_tmn: bool) -> str | None:
        """Returns the filter that forbids a bid on `market`, or None.

        Args:
            market: The Wallex market, such as "BTCTMN".
            coin: The base coin of the market.
            is_tmn: True for a toman market.
        """
        if (GLOBAL_DROP, coin) in self._until:
            return GLOBAL_DROP
        if is_tmn and (FX_SHOCK, FX_KEY) in self._until:
            return FX_SHOCK
        return None
