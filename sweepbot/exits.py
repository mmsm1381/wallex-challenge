"""Exit rules of a sweep position (DESIGN section 5).

* The exit is a maker sell at the fair value of the time of the fill. It
  moves when fair value moves more than the requote limit. This is the rule
  of the backtest. Do not add a minimum profit without a new backtest.
* A position is cut with a taker sell when it is older than the time limit,
  when the hold-or-cut answer says "cut", or when the end of the challenge
  needs all coins sold.
"""

import math

TARGET = "target"
TIME = "time"
CUT = "cut"
FLATTEN = "flatten"


def round_price(price: float, tick: float, up: bool) -> float:
    """Rounds `price` to the price tick of a market.

    Args:
        price: The raw price.
        tick: The price tick. 0 leaves the price as it is (the ticks come
            from the Wallex API in milestone 2).
        up: True rounds up (a sell), False rounds down (a buy).
    """
    if tick <= 0:
        return price
    steps = price / tick
    steps = math.ceil(steps - 1e-9) if up else math.floor(steps + 1e-9)
    return steps * tick


def exit_target(fv: float, tick: float = 0.0) -> float:
    """Returns the price of the maker exit: fair value, rounded up."""
    return round_price(fv, tick, up=True)


def cut_price(best_bid: float, slippage_pct: float, tick: float = 0.0) -> float:
    """Returns the limit of a taker sell: below the best bid by the slippage.

    The limit sells into the visible bids at once and does not sell below
    the slippage limit.
    """
    return round_price(best_bid * (1 - slippage_pct / 100), tick, up=False)


def cut_reason(*, now: float, entry_ts: float, max_hold_s: float,
               cut_requested: bool, flatten: bool) -> str | None:
    """Returns why the position must be cut now, or None.

    The order of the checks: the end of the challenge, the hold-or-cut
    answer, the time limit.
    """
    if flatten:
        return FLATTEN
    if cut_requested:
        return CUT
    if now - entry_ts >= max_hold_s:
        return TIME
    return None
