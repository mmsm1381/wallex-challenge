"""Global fair value for Wallex markets.

The shadow test compares each Wallex order book with a fair value. The fair
value comes from the best bid and ask on large global exchanges. This module
owns two parts of that calculation:

* `Quotes` keeps the latest quote per venue and coin. It gives the median
  mid of the venues that are still fresh.
* `Premium` keeps a rolling median of the log ratio between the Wallex mid
  and the raw fair value. Wallex markets trade at a small, slow premium or
  discount. The engine removes it so that only the fast gap is left.

The engine in `lagcatcher.engine` combines both parts. This module does not
know about Wallex books, strategies, or websockets.
"""

import collections
import statistics


class Quotes:
    """Latest best bid and ask per venue and coin.

    A venue that stops sending data keeps its last quote in memory. The age
    check in `global_mid` removes that quote, so a silent feed cannot keep
    an old price alive.
    """

    def __init__(self, max_quote_age_s: float, min_venues: int) -> None:
        """Creates an empty quote store.

        Args:
            max_quote_age_s: A quote older than this many seconds is stale.
            min_venues: The minimum number of fresh venues for a fair value.
        """
        self._max_age = max_quote_age_s
        self._min_venues = min_venues
        self._quotes: dict[str, dict[str, tuple[float, float, float]]] = {}

    def update(self, venue: str, coin: str, bid: float, ask: float, ts: float) -> None:
        """Stores the latest quote of one venue for one coin."""
        self._quotes.setdefault(coin, {})[venue] = (bid, ask, ts)

    def global_mid(self, coin: str, now: float) -> float | None:
        """Returns the median mid of the fresh venues, or None.

        The result is None when fewer than `min_venues` venues have a fresh,
        valid quote. A quote is not valid when a price is zero or less, or
        when the bid is above the ask.
        """
        mids = [
            (bid + ask) / 2
            for bid, ask, ts in self._quotes.get(coin, {}).values()
            if now - ts <= self._max_age and 0 < bid <= ask
        ]
        if len(mids) < self._min_venues:
            return None
        return statistics.median(mids)


class Premium:
    """Rolling median of log(Wallex mid / raw fair value) for one market.

    The median ignores short gaps, which are the signal. It follows only the
    slow premium. The value is None until the window has `min_samples`
    samples, so no strategy uses a premium that is not ready.
    """

    def __init__(self, window_s: float, min_samples: int) -> None:
        """Creates an empty premium tracker.

        Args:
            window_s: Samples older than this many seconds do not count.
            min_samples: The number of samples the value needs.
        """
        self._window = window_s
        self._min_samples = min_samples
        self._samples: collections.deque[tuple[float, float]] = collections.deque()
        # The engine reads the value hundreds of times per second, but the
        # samples change only every few seconds. A cache avoids a sort per read.
        self._median: float | None = None
        self._changed = True

    def add(self, ts: float, log_basis: float) -> None:
        """Adds one sample of log(Wallex mid / raw fair value)."""
        self._samples.append((ts, log_basis))
        self._changed = True

    def export(self) -> list[tuple[float, float]]:
        """Returns the samples as (ts, log basis) pairs, oldest first."""
        return list(self._samples)

    def load(self, samples: list, now: float) -> None:
        """Adds saved samples that are still inside the window at `now`.

        A restarted bot uses this, so that the premium is ready at once and
        does not need a new warm-up.
        """
        for ts, basis in sorted((float(t), float(b)) for t, b in samples):
            if now - ts <= self._window:
                self._samples.append((ts, basis))
        self._changed = True

    def value(self, now: float) -> float | None:
        """Returns the median premium in the window, or None before warm-up."""
        while self._samples and now - self._samples[0][0] > self._window:
            self._samples.popleft()
            self._changed = True
        if len(self._samples) < self._min_samples:
            return None
        if self._changed:
            self._median = statistics.median(basis for _, basis in self._samples)
            self._changed = False
        return self._median
