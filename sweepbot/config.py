"""Configuration of the sweep bot.

`load_config` reads a TOML file and fills each missing key from `DEFAULTS`.
The sweep bot uses the lagcatcher sections (markets, venues, fees, premium)
for the feeds and the fair value, and adds its own sections:

* `bot`: the mode and the output files.
* `plan`: the default plan. The executor uses it when no valid AI plan
  exists. The twin always uses it.
* `limits`: the hard limits of DESIGN section 7. No plan can change them.
* `filters`: the thresholds of DESIGN section 6.
* `slots`: the requote rule, the order budget and the cooldowns.
* `paper`: the latency and the fill rules of the paper broker.
* `funding`: the USDT to buy at the start for the USDT markets.
* `allocation`: which markets get the few bids that the funds allow.
* `challenge`: the start equity and the end of the challenge.
* `ai`: the model provider, the timer and the spending limit.

The numbers come from `docs/RESEARCH.md` section 3. Do not change them
without a new backtest.
"""

import copy

from lagcatcher import config as lag_config

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10 has no tomllib.
    import tomli as tomllib

MODES = ("live", "paper", "replay")

DEFAULTS: dict = lag_config._merge(lag_config.DEFAULTS, {
    "bot": {"mode": "paper", "out_dir": "data/sweepbot",
            "db_path": "data/sweepbot.db", "timer_s": 0.1,
            # Price ticks and quantity steps per market. "" turns rounding off.
            "market_rules": "research/wallex_markets.json",
            "status_every_s": 60.0,
            # How often the wallet is read and valued (panel and equity).
            "balances_every_s": 15.0,
            # The saved premium samples, so a restart needs no warm-up.
            # "" means <out_dir>/premiums.json.
            "premium_file": "", "premium_save_s": 60.0},
    # The 5 best markets of the first 30 days of the candle study. They were
    # tested out of sample on the last 31 days (RESEARCH.md section 3.2).
    # The depth depends on the quote currency (RESEARCH.md section 3.4): the
    # USDT round trip costs 0.20%, the toman round trip 0.50%.
    "plan": {"markets": ["ADATMN", "ETHUSDT", "TRXTMN", "ETHTMN", "PEPEUSDT"],
             "depth_pct_tmn": 1.3, "depth_pct_usdt": 1.0,
             "size_tmn": 1_000_000.0, "valid_minutes": 30},
    "limits": {"max_bid_tmn": 1_000_000.0, "max_slots": 5,
               "max_usdt_share": 0.40, "min_depth_pct": 1.0,
               "max_depth_pct": 2.5, "max_hold_s": 900.0,
               "daily_loss_pct": 1.5, "peak_drawdown_pct": 2.5,
               "start_drawdown_pct": 4.0, "stop_before_end_s": 7200.0,
               "orders_per_10s": 15, "safety_reserve": 5},
    "filters": {"global_drop_pct": 0.3, "global_window_s": 60.0,
                "global_wait_s": 120.0, "fx_move_pct": 0.5,
                "fx_window_s": 300.0, "fx_wait_s": 600.0,
                "wallex_stale_s": 5.0, "max_spread_pct": 1.0,
                "usdttmn_frozen_s": 300.0},
    "slots": {"requote_pct": 0.2, "cooldown_s": 60.0,
              "loss_cooldown_s": 300.0, "reject_cooldown_s": 60.0,
              "hold_or_cut_after_s": 300.0,
              "cut_slippage_pct": 2.0},
    "paper": {"latency_s": 0.5},
    # Allocation of the bids over all markets (DESIGN section 5.1). The
    # ranking is the per-market result of the 60-day candle study at 1.3%
    # with both filters (RESEARCH.md section 3.2), best first. The markets
    # without fills in the study come last.
    "allocation": {"enabled": False,
                   "ranking": ["ADATMN", "BTCTMN", "ETHUSDT", "TRXTMN", "ETHTMN",
                               "PEPEUSDT", "SHIBTMN", "ADAUSDT", "DOGEUSDT", "DOGETMN",
                               "XRPTMN", "PEPETMN", "XRPUSDT", "SOLTMN", "BNBTMN",
                               "BTCUSDT", "SOLUSDT", "BNBUSDT"],
                   "tmn_slots": 3, "usdt_slots": 2, "switch_min_s": 60.0,
                   "log_every_s": 60.0},
    # USDT to buy at the start, as a part of the start equity. 0 turns the
    # purchase off. It must not be above limits.max_usdt_share.
    "funding": {"usdt_target_share": 0.0, "slippage_pct": 0.3},
    # Times are ISO 8601 UTC, such as "2026-11-01T08:00:00Z". An empty end
    # turns the end-of-challenge schedule off.
    "challenge": {"start_equity_tmn": 5_000_000.0, "end_utc": ""},
    "ai": {"enabled": False, "provider": "claude", "model": "claude-opus-5-5",
           "effort": "medium", "timeout_s": 60.0, "supervisor_every_s": 900.0,
           "max_failures": 2, "budget_usd": 50.0, "web_search": True,
           "news_every_s": 3600.0,
           # For provider "openai_compat" (an OpenAI-compatible router).
           "base_url": "", "api_key_env": "AI_API_KEY", "json_mode": True,
           "max_tokens": 4000, "price_in_per_mtok": 0.0, "price_out_per_mtok": 0.0},
})


def _merge(base: dict, override: dict) -> dict:
    """Returns a copy of `base` with the sections of `override` applied."""
    return lag_config._merge(base, override)


def trade_markets(cfg: dict) -> list[str]:
    """Returns the Wallex markets that the feeds read, without USDTTMN."""
    markets = cfg["markets"]
    return ([f"{coin}USDT" for coin in markets["usdt"]]
            + [f"{coin}TMN" for coin in markets["tmn"]])


def default_depth(cfg: dict, market: str) -> float:
    """Returns the default plan depth of `market`, by its quote currency."""
    plan = cfg["plan"]
    return plan["depth_pct_usdt"] if market.endswith("USDT") else plan["depth_pct_tmn"]


def validate(cfg: dict) -> None:
    """Raises ValueError when a value of `cfg` breaks a rule.

    The rules: a known mode, a default plan inside the hard limits, positive
    limits, and an AI budget of zero or more.
    """
    if cfg["bot"]["mode"] not in MODES:
        raise ValueError(f"bot.mode must be one of {MODES}, not {cfg['bot']['mode']!r}")
    limits = cfg["limits"]
    plan = cfg["plan"]
    for key in ("depth_pct_tmn", "depth_pct_usdt"):
        if not limits["min_depth_pct"] <= plan[key] <= limits["max_depth_pct"]:
            raise ValueError(f"plan.{key} {plan[key]} is outside "
                             f"[{limits['min_depth_pct']}, {limits['max_depth_pct']}]")
    if not 0 < plan["size_tmn"] <= limits["max_bid_tmn"]:
        raise ValueError(f"plan.size_tmn {plan['size_tmn']} is outside "
                         f"(0, {limits['max_bid_tmn']}]")
    if len(plan["markets"]) > limits["max_slots"]:
        raise ValueError(f"plan.markets has more than {limits['max_slots']} markets")
    if len(set(plan["markets"])) != len(plan["markets"]):
        raise ValueError("plan.markets has a duplicate market")
    unknown = sorted(set(plan["markets"]) - set(trade_markets(cfg)))
    if unknown:
        raise ValueError(f"plan.markets has unknown markets: {unknown}")
    for key in ("max_bid_tmn", "max_slots", "max_hold_s", "daily_loss_pct",
                "peak_drawdown_pct", "start_drawdown_pct", "orders_per_10s"):
        if limits[key] <= 0:
            raise ValueError(f"limits.{key} must be positive")
    if not 0 <= limits["max_usdt_share"] <= 1:
        raise ValueError("limits.max_usdt_share must be in [0, 1]")
    if limits["safety_reserve"] < 0:
        raise ValueError("limits.safety_reserve must be zero or more")
    if not 0 <= cfg["funding"]["usdt_target_share"] <= limits["max_usdt_share"]:
        raise ValueError("funding.usdt_target_share must be in [0, limits.max_usdt_share]")
    alloc = cfg["allocation"]
    if alloc["tmn_slots"] < 0 or alloc["usdt_slots"] < 0:
        raise ValueError("allocation slots must be zero or more")
    if alloc["tmn_slots"] + alloc["usdt_slots"] > limits["max_slots"]:
        raise ValueError("allocation.tmn_slots + usdt_slots is above limits.max_slots")
    unknown = sorted(set(alloc["ranking"]) - set(trade_markets(cfg)))
    if unknown:
        raise ValueError(f"allocation.ranking has unknown markets: {unknown}")
    if cfg["ai"]["budget_usd"] < 0:
        raise ValueError("ai.budget_usd must be zero or more")


def load_config(path: str | None) -> dict:
    """Reads the TOML file at `path`, fills the missing keys and checks them.

    Args:
        path: The TOML file. None gives the defaults only.

    Returns:
        A plain dict with the sections of `DEFAULTS`.

    Raises:
        ValueError: A value breaks a rule of `validate`.
    """
    if path is None:
        cfg = copy.deepcopy(DEFAULTS)
    else:
        with open(path, "rb") as config_file:
            cfg = _merge(DEFAULTS, tomllib.load(config_file))
    validate(cfg)
    return cfg


def fee_rates(quote: str, cfg: dict) -> tuple[float, float]:
    """Returns (maker, taker) fee rates as fractions for a quote currency."""
    return lag_config.fee_rates(quote, cfg)
