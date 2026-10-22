"""Gap episodes: how long a buyable gap stays on the Wallex book.

A gap is 1 - best ask / fair value. An episode starts when the gap reaches
a threshold and ends when it falls below it again. The episode length and
the book depth answer the key question of the shadow test: does a cheap ask
stay long enough, and is it big enough, for our bot to buy it?

The engine calls `Episodes.update` on each book or fair-value change and
writes the closed episodes to disk.
"""

from collections.abc import Mapping, Sequence


class Episodes:
    """Tracks open gap episodes per market and threshold."""

    def __init__(self, thresholds_pct: Sequence[float]) -> None:
        """Creates a tracker for each threshold, in percent."""
        self._thresholds = list(thresholds_pct)
        self._open: dict[tuple[str, float], dict] = {}

    def update(self, *, market: str, now: float, gap: float | None,
               depth_tmn: Mapping[float, float] | None) -> list[dict]:
        """Updates the episodes of one market and returns the closed ones.

        Args:
            market: The Wallex market.
            now: The current time in seconds.
            gap: The gap as a fraction, or None when the fair value or the
                book is not known. None closes each open episode.
            depth_tmn: Per threshold, the toman value of the asks priced at
                or below fair value times (1 - threshold).

        Returns:
            One dict per closed episode.
        """
        closed = []
        for threshold in self._thresholds:
            key = (market, threshold)
            episode = self._open.get(key)
            if gap is None:
                if episode is not None:
                    closed.append(self._close(key, now, "no_data"))
                continue
            if 100 * gap >= threshold:
                depth = depth_tmn.get(threshold) if depth_tmn else None
                if episode is None:
                    self._open[key] = {
                        "market": market, "threshold_pct": threshold,
                        "start_ts": now, "max_gap_pct": 100 * gap,
                        "depth_tmn_at_max": depth, "updates": 1}
                    continue
                episode["updates"] += 1
                if 100 * gap > episode["max_gap_pct"]:
                    episode["max_gap_pct"] = 100 * gap
                    episode["depth_tmn_at_max"] = depth
            elif episode is not None:
                closed.append(self._close(key, now, "closed"))
        return closed

    def _close(self, key: tuple[str, float], now: float, reason: str) -> dict:
        """Removes an open episode and returns it with its end and length."""
        episode = self._open.pop(key)
        episode.update(end_ts=now, duration_s=now - episode["start_ts"],
                       end_reason=reason)
        return episode
