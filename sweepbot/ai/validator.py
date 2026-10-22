"""Plan validator: checks a plan against the hard limits (DESIGN section 7).

The validator rejects a plan that it cannot repair: bad JSON, a schema error,
an unknown or duplicate market, or a size of zero or less. It clamps the
other values to the limits and adds one note for each change. It never
changes its input.
"""

import dataclasses
import json

import pydantic

from sweepbot import config
from sweepbot.ai import schema

MIN_VALID_MINUTES = 5
MAX_VALID_MINUTES = 120


@dataclasses.dataclass
class ValidationResult:
    """The result of `validate_plan`.

    Attributes:
        plan: The checked plan. None when the plan is rejected.
        accepted: True when `plan` is set.
        notes: One short note per change, or per reason of a reject.
    """

    plan: schema.Plan | None
    accepted: bool
    notes: list[str]


def default_plan(cfg: dict) -> schema.Plan:
    """Returns the default plan of `cfg["plan"]`: one slot per market.

    The depth of a slot depends on the quote currency of its market.
    """
    plan = cfg["plan"]
    slots = [schema.SlotPlan(market=market,
                             depth_pct=config.default_depth(cfg, market),
                             size_tmn=plan["size_tmn"])
             for market in plan["markets"]]
    return schema.Plan(valid_minutes=plan["valid_minutes"], slots=slots,
                       reasoning="default plan")


def _reject(notes: list[str]) -> ValidationResult:
    """Returns a rejected result with `notes`."""
    return ValidationResult(plan=None, accepted=False, notes=notes)


def _parse(raw: dict | str | schema.Plan) -> schema.Plan:
    """Returns a new `Plan` from `raw`.

    Raises:
        ValueError: The text is not JSON, or the data breaks the schema.
    """
    if isinstance(raw, schema.Plan):
        return raw.model_copy(deep=True)
    if isinstance(raw, str):
        raw = json.loads(raw)
    return schema.Plan.model_validate(raw)


def validate_plan(raw: dict | str | schema.Plan, *, cfg: dict,
                  equity_tmn: float) -> ValidationResult:
    """Checks a plan and clamps it to the limits of `cfg`.

    Args:
        raw: A plan as a dict, a JSON string or a `Plan`.
        cfg: The config with the `limits` section.
        equity_tmn: The equity in toman, for the USDT share.

    Returns:
        The result. A plan that is inside all limits passes unchanged.
    """
    try:
        plan = _parse(raw)
    except (ValueError, pydantic.ValidationError) as error:
        return _reject([f"invalid plan: {error}"])

    limits = cfg["limits"]
    known = set(config.trade_markets(cfg))
    notes: list[str] = []
    seen: set[str] = set()
    for slot in plan.slots:
        if slot.market not in known:
            notes.append(f"unknown market {slot.market}")
        elif slot.market in seen:
            notes.append(f"duplicate market {slot.market}")
        if slot.size_tmn <= 0:
            notes.append(f"size {slot.size_tmn} of {slot.market} is not positive")
        seen.add(slot.market)
    if notes:
        return _reject(notes)

    for slot in plan.slots:
        low, high = limits["min_depth_pct"], limits["max_depth_pct"]
        if not low <= slot.depth_pct <= high:
            clamped = min(max(slot.depth_pct, low), high)
            notes.append(f"depth of {slot.market} changed from "
                         f"{slot.depth_pct} to {clamped}")
            slot.depth_pct = clamped
        if slot.size_tmn > limits["max_bid_tmn"]:
            notes.append(f"size of {slot.market} changed from "
                         f"{slot.size_tmn} to {limits['max_bid_tmn']}")
            slot.size_tmn = limits["max_bid_tmn"]

    if len(plan.slots) > limits["max_slots"]:
        notes.append(f"slots cut from {len(plan.slots)} to {limits['max_slots']}")
        plan.slots = plan.slots[:limits["max_slots"]]

    usdt_cap = limits["max_usdt_share"] * equity_tmn
    while True:
        usdt = [slot for slot in plan.slots if slot.market.endswith("USDT")]
        if sum(slot.size_tmn for slot in usdt) <= usdt_cap:
            break
        notes.append(f"slot {usdt[-1].market} dropped for the USDT share")
        plan.slots.remove(usdt[-1])

    clamped = min(max(plan.valid_minutes, MIN_VALID_MINUTES), MAX_VALID_MINUTES)
    if clamped != plan.valid_minutes:
        notes.append(f"valid_minutes changed from {plan.valid_minutes} to {clamped}")
        plan.valid_minutes = clamped

    return ValidationResult(plan=plan, accepted=True, notes=notes)
