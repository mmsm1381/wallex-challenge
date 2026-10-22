"""Price and quantity rules of the Wallex markets.

The rules come from the public market list (`GET /v1/markets`), saved in
`research/wallex_markets.json` by `research/scripts/wallex_api_probe.py`.
In that list, `tickSize` and `stepSize` are numbers of decimals: the price
tick is 10**-tickSize and the quantity step is 10**-stepSize. The step
agrees with `minQty` for every market of the bot (2026-10-22).
"""

import dataclasses
import json
import math
import pathlib

# A relative rules path is found from the working folder or the repo root.
_REPO = pathlib.Path(__file__).resolve().parents[1]


@dataclasses.dataclass(frozen=True)
class MarketRule:
    """Order rules of one market.

    Attributes:
        price_tick: The smallest price change, in the quote currency.
        qty_step: The smallest quantity change, in the base coin.
        min_qty: The smallest order quantity.
        min_notional: The smallest order value, in the quote currency.
    """

    price_tick: float = 0.0
    qty_step: float = 0.0
    min_qty: float = 0.0
    min_notional: float = 0.0

    @property
    def price_decimals(self) -> int:
        """Returns the number of decimals of a price."""
        return _decimals(self.price_tick)

    @property
    def qty_decimals(self) -> int:
        """Returns the number of decimals of a quantity."""
        return _decimals(self.qty_step)

    def round_qty(self, qty: float) -> float:
        """Rounds a quantity down to the step."""
        if self.qty_step <= 0:
            return qty
        return math.floor(qty / self.qty_step + 1e-9) * self.qty_step

    def order_qty(self, value_quote: float, price: float) -> float | None:
        """Returns the quantity of an order of `value_quote` at `price`.

        Returns:
            The quantity rounded down to the step, or None when the order is
            below the minimum quantity or the minimum value.
        """
        qty = self.round_qty(value_quote / price)
        if qty <= 0 or qty < self.min_qty or qty * price < self.min_notional:
            return None
        return qty


def _decimals(step: float) -> int:
    """Returns the decimals of a tick or step such as 0.001 (3). 0 gives 8."""
    if step <= 0:
        return 8
    return max(0, round(-math.log10(step)))


def load_rules(path: str | None) -> dict[str, MarketRule]:
    """Reads the saved market list and returns the rules per market.

    Args:
        path: The JSON file, or None or "" for no rules (no rounding).
    """
    if not path:
        return {}
    file_path = pathlib.Path(path)
    if not file_path.is_absolute() and not file_path.exists():
        file_path = _REPO / path
    with open(file_path, encoding="utf-8") as rules_file:
        symbols = json.load(rules_file)["symbols"]
    return {name: MarketRule(price_tick=10.0 ** -int(item["tickSize"]),
                             qty_step=10.0 ** -int(item["stepSize"]),
                             min_qty=float(item["minQty"]),
                             min_notional=float(item["minNotional"]))
            for name, item in symbols.items()}
