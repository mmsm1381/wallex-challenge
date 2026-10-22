"""The no-AI twin (DESIGN section 8.4).

The twin is a second executor with the fixed default plan and its own
`PaperBroker`. It runs on the same market data as the bot. The daily
report puts the bot and the twin side by side, so the effect of the AI
part shows in toman.
"""

from sweepbot import broker
from sweepbot import executor
from sweepbot import filters as filters_mod
from sweepbot import market as market_mod
from sweepbot import risk as risk_mod
from sweepbot import store as store_mod
from sweepbot.ai import validator

TWIN = "twin"


def build_twin(cfg: dict, *, market: market_mod.Market, store: store_mod.Store,
               filters: filters_mod.Filters, start_ts: float) -> executor.Executor:
    """Builds the twin executor and connects its paper broker to the market.

    Args:
        cfg: The config dict from `sweepbot.config.load_config`.
        market: The shared market state.
        store: The SQLite store. Twin rows have mode "twin".
        filters: The shared filters.
        start_ts: The start time of the run.

    Returns:
        The twin executor. The runner calls its `step` next to the bot.
    """
    plan = validator.default_plan(cfg)
    plan_id = store.add_plan(ts=start_ts, provider=TWIN, model="default",
                             raw_json=plan.model_dump_json(),
                             validated_json=plan.model_dump_json(), accepted=True,
                             notes="fixed default plan of the twin")
    paper = broker.PaperBroker(cfg, name=TWIN)
    market.add_listener(on_book=paper.on_book, on_trade=paper.on_trade)
    return executor.Executor(
        cfg, market=market, broker_=paper, store=store, mode=TWIN,
        risk=risk_mod.RiskGuard(cfg, start_ts), filters=filters,
        plan_source=lambda now: (plan, plan_id),
        allocate=cfg["allocation"]["enabled"])
