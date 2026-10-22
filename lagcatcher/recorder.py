"""Disk recorder for the raw feeds and the paper-trading events.

The raw streams let us replay a session later and train models on it:

* `raw/wallex_book-YYYYmmddHH.jsonl.gz`: the top levels of each book side.
* `raw/wallex_trade-YYYYmmddHH.jsonl.gz`: each Wallex trade.
* `raw/global-YYYYmmddHH.jsonl.gz`: global best quotes, at most one per
  venue and coin per 250 ms.

The paper results are plain JSONL in the output folder: `events.jsonl`
(signals, fills, misses, exits) and `episodes.jsonl` (gap episodes).
`lagcatcher.report` reads them. All times are UTC epoch seconds.
"""

import gzip
import json
import pathlib
import time
import typing


class Recorder:
    """Writes the raw streams with hourly files, and the paper events."""

    def __init__(self, out_dir: str, levels: int, quote_every_s: float = 0.25) -> None:
        """Opens the output folder.

        Args:
            out_dir: The output folder. The recorder creates it.
            levels: The number of book levels per side to keep.
            quote_every_s: The smallest time between two kept quotes of one
                venue and coin.
        """
        self._dir = pathlib.Path(out_dir)
        (self._dir / "raw").mkdir(parents=True, exist_ok=True)
        self._levels = levels
        self._quote_every = quote_every_s
        # The engine turns this off when the disk is almost full.
        self.raw_enabled = True
        self._streams: dict[str, tuple[str, typing.TextIO]] = {}
        self._last_quote: dict[tuple[str, str], float] = {}
        self._events = open(self._dir / "events.jsonl", "a", encoding="utf-8")
        self._episodes = open(self._dir / "episodes.jsonl", "a", encoding="utf-8")

    def _write(self, stream: str, record: dict, ts: float) -> None:
        """Appends one record to the hourly file of `stream`."""
        if not self.raw_enabled:
            return
        hour = time.strftime("%Y%m%d%H", time.gmtime(ts))
        current = self._streams.get(stream)
        if current is None or current[0] != hour:
            if current is not None:
                current[1].close()
            path = self._dir / "raw" / f"{stream}-{hour}.jsonl.gz"
            current = (hour, gzip.open(path, "at", encoding="utf-8"))
            self._streams[stream] = current
        current[1].write(json.dumps(record, separators=(",", ":")) + "\n")

    def book(self, ts: float, market: str, side: str, levels: list) -> None:
        """Records the top levels of one Wallex book side."""
        self._write("wallex_book", {"ts": round(ts, 3), "m": market, "s": side,
                                    "lv": levels[:self._levels]}, ts)

    def trades(self, ts: float, market: str, trades: list) -> None:
        """Records Wallex trades. `bm` is True when the buyer was the maker."""
        for price, qty, buyer_maker, trade_ts in trades:
            self._write("wallex_trade", {"ts": round(ts, 3), "m": market,
                                         "p": price, "q": qty, "bm": buyer_maker,
                                         "tts": trade_ts}, ts)

    def quote(self, ts: float, venue: str, coin: str, bid: float, ask: float) -> None:
        """Records a global quote, at most once per `quote_every_s`."""
        key = (venue, coin)
        if ts - self._last_quote.get(key, 0.0) < self._quote_every:
            return
        self._last_quote[key] = ts
        self._write("global", {"ts": round(ts, 3), "v": venue, "c": coin,
                               "b": bid, "a": ask}, ts)

    def event(self, event: dict) -> None:
        """Appends one paper-trading event."""
        self._events.write(json.dumps(event, separators=(",", ":")) + "\n")

    def episode(self, episode: dict) -> None:
        """Appends one closed gap episode."""
        self._episodes.write(json.dumps(episode, separators=(",", ":")) + "\n")

    def flush(self) -> None:
        """Writes the buffers to disk, so a crash loses little data."""
        for _, stream in self._streams.values():
            stream.flush()
        self._events.flush()
        self._episodes.flush()

    def close(self) -> None:
        """Closes all files."""
        for _, stream in self._streams.values():
            stream.close()
        self._streams.clear()
        self._events.close()
        self._episodes.close()
