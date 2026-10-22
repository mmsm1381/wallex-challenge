"""Schemas of the AI jobs (DESIGN section 8).

Each model forbids extra fields. The types stay simple, so that a provider
adapter can pass these classes as the structured output format.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict


class SlotPlan(BaseModel):
    """One slot of a plan: the market, the depth of the bid and its size."""

    model_config = ConfigDict(extra="forbid")

    market: str
    depth_pct: float
    size_tmn: float


class Plan(BaseModel):
    """The plan of the supervisor for the next `valid_minutes`."""

    model_config = ConfigDict(extra="forbid")

    valid_minutes: int = 30
    pause: bool = False
    pause_reason: str | None = None
    slots: list[SlotPlan]
    fx_risk: Literal["normal", "high"] = "normal"
    reasoning: str = ""


class HoldOrCut(BaseModel):
    """The answer for a position that has no exit fill."""

    model_config = ConfigDict(extra="forbid")

    decision: Literal["hold", "cut"]
    reason: str


class TradeLog(BaseModel):
    """The text and the lesson of one closed trade."""

    model_config = ConfigDict(extra="forbid")

    text: str
    lesson: str


class Review(BaseModel):
    """The daily review, with proposed changes inside the limits."""

    model_config = ConfigDict(extra="forbid")

    summary: str
    what_worked: str
    what_failed: str
    proposed_changes: list[str]


class NewsSummary(BaseModel):
    """The summary of the news and its risk level."""

    model_config = ConfigDict(extra="forbid")

    summary: str
    risk: Literal["normal", "high"]
