"""Configuration for the lagcatcher shadow test.

`load_config` reads a TOML file and fills each missing key from `DEFAULTS`.
The defaults hold the decisions of the MVP plan: the markets, the venues,
the real fees of the challenge account, and the paper-trading parameters.
Each other module receives the plain dict that `load_config` returns.
"""

import copy

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10 has no tomllib.
    import tomli as tomllib

# Fees are percent per side. They come from the challenge account screen.
DEFAULTS: dict = {
    "run": {"out_dir": "data", "status_every_s": 60.0, "record_levels": 10,
            "wallex_stale_s": 5.0, "min_free_mb_record": 600.0,
            "min_free_mb_run": 300.0},
    "markets": {
        "usdt": ["BTC", "ETH", "XRP", "SOL", "PEPE", "DOGE", "BNB", "ADA"],
        "tmn": ["BTC", "ETH", "XRP", "SOL", "PEPE", "DOGE", "BNB", "ADA",
                "SHIB", "TRX"],
    },
    "venues": {
        "enabled": ["bybit", "kucoin", "gate"],
        "max_quote_age_s": 10.0,
        "min_venues": 2,
    },
    "fees": {"tmn_maker": 0.25, "tmn_taker": 0.30,
             "usdt_maker": 0.10, "usdt_taker": 0.13},
    # The premium uses only mids of books with a spread up to max_spread_pct.
    # The mid of a wide book is far from the real price.
    "premium": {"window_s": 3600.0, "sample_s": 5.0, "min_samples": 120,
                "max_spread_pct": 0.3},
    "paper": {"notional_tmn": 1_000_000.0, "min_fill_fraction": 0.5,
              "max_entry_spread_pct": 0.5},
    "taker": {"latencies_s": [0.3, 1.0, 3.0], "buffer_pct": 0.30,
              "min_gap_pct": 0.60},
    "resting": {"latency_s": 0.5, "buffer_pct": 0.30, "requote_bps": 5.0},
    "exit": {"max_hold_s": 900.0, "stop_pct": 1.0, "requote_bps": 5.0,
             "stop_confirm_s": 5.0, "cooldown_s": 300.0},
    "limits": {"orders_per_10s": 15},
    "episodes": {"thresholds_pct": [0.5, 1.0]},
}


def _merge(base: dict, override: dict) -> dict:
    """Returns a copy of `base` with the keys of `override` applied.

    The merge is one level deep. A key in a section of `override` replaces
    the same key in `base`. The other keys of that section stay.
    """
    merged = copy.deepcopy(base)
    for section, values in override.items():
        if isinstance(values, dict) and isinstance(merged.get(section), dict):
            merged[section].update(values)
        else:
            merged[section] = values
    return merged


def load_config(path: str | None) -> dict:
    """Reads the TOML file at `path` and fills the missing keys.

    Args:
        path: The TOML file. None gives the defaults only.

    Returns:
        A plain dict with the same sections as `DEFAULTS`.
    """
    if path is None:
        return copy.deepcopy(DEFAULTS)
    with open(path, "rb") as config_file:
        return _merge(DEFAULTS, tomllib.load(config_file))


def fee_rates(quote: str, cfg: dict) -> tuple[float, float]:
    """Returns (maker, taker) fee rates as fractions for a quote currency.

    Args:
        quote: "TMN" or "USDT".
        cfg: The config dict from `load_config`.
    """
    fees = cfg["fees"]
    prefix = quote.lower()
    return fees[f"{prefix}_maker"] / 100, fees[f"{prefix}_taker"] / 100
