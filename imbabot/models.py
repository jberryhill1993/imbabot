"""Typed primitives shared across the bot.

Deliberately dependency-free so the strategy math can be unit-tested without
installing requests/keyring or touching the network.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from enum import Enum, IntEnum
from typing import List, Optional


class OrderType(IntEnum):
    """ProjectX order types (see API reference /api/Order/place)."""

    LIMIT = 1
    MARKET = 2
    STOP = 4
    TRAILING_STOP = 5
    JOIN_BID = 6
    JOIN_ASK = 7


class OrderSide(IntEnum):
    """ProjectX order sides. Bid == buy, Ask == sell."""

    BUY = 0
    SELL = 1


class TradeMode(str, Enum):
    """How the two straddle legs are managed after they're placed."""

    # Place both entries; the trader manages/cancels them by hand.
    SEMI_AUTO = "semi_auto"
    # Fully automated single trade: whichever entry fills first stays, the bot
    # cancels the opposite entry automatically (a one-cancels-the-other pair).
    ONE_TRADE = "one_trade"


@dataclass
class Account:
    id: int
    name: str
    can_trade: bool
    is_visible: bool

    @classmethod
    def from_api(cls, d: dict) -> "Account":
        return cls(
            id=int(d["id"]),
            name=str(d.get("name", "")),
            can_trade=bool(d.get("canTrade", False)),
            is_visible=bool(d.get("isVisible", True)),
        )


@dataclass
class Contract:
    id: str
    name: str
    description: str
    tick_size: float
    tick_value: float
    active: bool
    symbol_id: str

    @classmethod
    def from_api(cls, d: dict) -> "Contract":
        return cls(
            id=str(d["id"]),
            name=str(d.get("name", "")),
            description=str(d.get("description", "")),
            tick_size=float(d.get("tickSize", 0.0) or 0.0),
            tick_value=float(d.get("tickValue", 0.0) or 0.0),
            active=bool(d.get("activeContract", False)),
            symbol_id=str(d.get("symbolId", "")),
        )


@dataclass
class Bar:
    t: str
    o: float
    h: float
    l: float
    c: float
    v: float

    @classmethod
    def from_api(cls, d: dict) -> "Bar":
        return cls(
            t=str(d.get("t", "")),
            o=float(d.get("o", 0.0)),
            h=float(d.get("h", 0.0)),
            l=float(d.get("l", 0.0)),
            c=float(d.get("c", 0.0)),
            v=float(d.get("v", 0.0)),
        )


@dataclass
class OrderResult:
    order_id: Optional[int]
    success: bool
    error_code: int
    error_message: Optional[str]

    @classmethod
    def from_api(cls, d: dict) -> "OrderResult":
        oid = d.get("orderId")
        return cls(
            order_id=int(oid) if oid is not None else None,
            success=bool(d.get("success", False)),
            error_code=int(d.get("errorCode", -1)),
            error_message=d.get("errorMessage"),
        )


@dataclass
class StraddleLeg:
    """One side of the opening-range straddle."""

    side: OrderSide
    stop_price: float          # the breakout entry trigger
    size: int
    stop_loss_ticks: int       # protective stop distance, in ticks from fill
    take_profit_ticks: int     # target distance, in ticks from fill
    custom_tag: str
    order_id: Optional[int] = None  # filled in after placement


@dataclass
class StraddlePlan:
    """The full set of orders the bot intends to place at fire time."""

    contract: Contract
    reference_price: float     # price captured at the fire moment
    long_leg: StraddleLeg
    short_leg: StraddleLeg

    @property
    def legs(self) -> List[StraddleLeg]:
        return [self.long_leg, self.short_leg]


def round_to_tick(price: float, tick_size: float) -> float:
    """Round a price to the nearest valid tick.

    Uses Decimal so e.g. 21000.07 with tick 0.25 lands cleanly on 21000.0,
    never 21000.000000001. Exchanges reject prices that aren't on a tick.
    """
    if tick_size <= 0:
        return price
    tick = Decimal(str(tick_size))
    steps = (Decimal(str(price)) / tick).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return float(steps * tick)


def points_to_ticks(points: float, tick_size: float) -> int:
    """Convert a point distance to a whole number of ticks (>= 1)."""
    if tick_size <= 0:
        return max(1, int(round(points)))
    return max(1, int(round(points / tick_size)))
