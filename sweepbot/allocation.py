"""Allocation of the bids over all markets (DESIGN section 5.1).

The bot watches all markets, but the funds allow only a few resting bids at
the same time: `allocation.tmn_slots` on toman markets and
`allocation.usdt_slots` on USDT markets. A bid must rest before a sweep
comes, so the bot cannot wait for a sweep and then bid. `allocate` chooses
the markets that get the bids now:

1. A slot that holds coins (a position or an exit order) or a bid that is
   being cancelled keeps its funds. It uses one place of its quote.
2. The other places go to the eligible markets in the order of
   `allocation.ranking` (the 60-day backtest, best first). A market is
   eligible when it has a fair value, a narrow spread, no filter and no
   cooldown.
3. A live bid that is younger than `switch_min_s` keeps its place, so that
   a better market does not make the bot cancel and send bids all the time.
4. Each eligible market without a place is "skipped: no funds".

The function is pure: the executor gives it the state and logs the result.
"""

import dataclasses

from sweepbot import market as market_mod

# The kinds of a busy slot.
HOLDING = "holding"  # coins, an exit order, or a bid that is being cancelled
BID = "bid"  # a pending or live bid


@dataclasses.dataclass
class Allocation:
    """The result of one allocation.

    Attributes:
        selected: The markets that may have a bid now, best first.
        no_funds: The eligible markets without a place, best first.
        used: Per quote, the markets that use a place (holding or bid).
    """

    selected: list[str]
    no_funds: list[str]
    used: dict[str, list[str]]


def allocate(*, ranking: list[str], caps: dict[str, int],
             ineligible: dict[str, str | None], busy: dict[str, tuple[str, float | None]],
             now: float, switch_min_s: float) -> Allocation:
    """Chooses the markets that get a bid now.

    Args:
        ranking: All markets, best first.
        caps: The number of places per quote, such as {"TMN": 3, "USDT": 2}.
        ineligible: Per market, the reason why it may not bid now, or None.
        busy: Per busy market, (kind, order time). The kind is HOLDING or BID.
        now: The current time.
        switch_min_s: The age under which a live bid keeps its place.

    Returns:
        The selected markets, the skipped markets and the used places.
    """
    selected: list[str] = []
    no_funds: list[str] = []
    used: dict[str, list[str]] = {}
    for quote, cap in caps.items():
        ranked = [m for m in ranking if market_mod.quote_of(m) == quote]
        holding = [m for m, (kind, _) in busy.items()
                   if kind == HOLDING and market_mod.quote_of(m) == quote]
        eligible = [m for m in ranked if ineligible.get(m) is None and m not in holding]
        free = max(cap - len(holding), 0)
        chosen = eligible[:free]
        # A young bid keeps its place against a better market.
        young = [m for m in eligible if m not in chosen and busy.get(m, ("", None))[0] == BID
                 and now - (busy[m][1] or now) < switch_min_s]
        for market in young:
            newcomers = [m for m in chosen if m not in busy]
            if not newcomers:
                break
            chosen.remove(newcomers[-1])
            chosen.append(market)
        chosen.sort(key=ranked.index)
        selected += chosen
        no_funds += [m for m in eligible if m not in chosen]
        used[quote] = holding + chosen
    return Allocation(selected=selected, no_funds=no_funds, used=used)
