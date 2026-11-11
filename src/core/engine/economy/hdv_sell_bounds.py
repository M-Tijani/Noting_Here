import math
from dataclasses import dataclass, field

from DBDofusUnity.dofus_unity_reader.game_constants.sale_hotel import QuantityEnum
from pydantic import BaseModel, ConfigDict, model_validator


class HdvPriceBounds(BaseModel):
    """Per-item price range the HDV tab clamps the automatic undercut price into."""

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    minimum: int
    maximum: int
    max_below_average: float | None = None
    max_above_average: float | None = None

    @model_validator(mode="after")
    def _validate_range(self) -> "HdvPriceBounds":
        if self.minimum < 1:
            raise ValueError("minimum price must be at least 1")
        if self.maximum < self.minimum:
            raise ValueError("maximum price must be greater than or equal to the minimum")
        if self.max_below_average is not None and not 0.0 <= self.max_below_average <= 1.0:
            raise ValueError("max_below_average must be between 0 and 1")
        if self.max_above_average is not None and self.max_above_average < 0.0:
            raise ValueError("max_above_average must be non-negative")
        return self


class HdvSellTarget(HdvPriceBounds):
    """A selected bank item: its price range plus the lot size to list it in (1/10/100/1000).

    ``max_lots`` optionally caps how many lots of this item are listed per run:
    with 1000 units, ``lot=100`` and ``max_lots=2``, only two lots of 100 are
    listed and the rest stays in the bag.
    """

    quantity: QuantityEnum = QuantityEnum.VALUE_1
    max_lots: int | None = None

    @model_validator(mode="after")
    def _validate_max_lots(self) -> "HdvSellTarget":
        if self.max_lots is not None and self.max_lots < 1:
            raise ValueError("max_lots must be at least 1")
        return self


def clamp_price_to_bounds(
    price: int, bounds: HdvPriceBounds, average_price: float | None = None
) -> int | None:
    """Clamp an undercut price into the configured range.

    Returns ``None`` when the price is below the minimum, meaning the item is not
    worth listing and should be skipped. When ``max_below_average`` is set and the
    market average is known, a price more than that fraction below the average is
    also skipped: this refuses to follow a crash (someone dropping the price hard).
    When ``max_above_average`` is set and the market average is known, a price more
    than that fraction above the average is also skipped: this refuses to follow a
    price spike (someone listing far above the average to bait bots).
    """
    if price < bounds.minimum:
        return None
    if average_price is not None and average_price > 0:
        if bounds.max_below_average is not None:
            if price < math.ceil(average_price * (1.0 - bounds.max_below_average)):
                return None
        if bounds.max_above_average is not None:
            if price > math.ceil(average_price * (1.0 + bounds.max_above_average)):
                return None
    return min(price, bounds.maximum)


@dataclass
class HdvRepriceConfig:
    """Settings for a MoonBot-style reprice pass over the current listings.

    ``offset`` is added to the cheapest market price of the listing's lot
    (``-1`` = one kama under). ``maximum_drop_percent`` caps how far a listing may
    drop in a single pass (``0.10`` = 10%). ``max_below_average`` refuses to reprice
    more than that fraction below the item's market average (``0.5`` = never below
    half the average), which stops the pass from following a price crash.
    ``default_floor``/``floor_by_gid`` set the per-lot price below which the bot
    refuses to go. ``lot_size`` restricts the pass to a single lot size, ``None``
    reprices every lot.
    """

    offset: int = -1
    maximum_drop_percent: float | None = None
    max_below_average: float | None = None
    max_above_average: float | None = None
    default_floor: int | None = None
    floor_by_gid: dict[int, int] = field(default_factory=dict[int, int])
    lot_size: QuantityEnum | None = None

    def floor_for(self, gid: int) -> int | None:
        return self.floor_by_gid.get(gid, self.default_floor)

    def new_price(
        self,
        gid: int,
        current_price: int,
        market_price: int,
        average_price: float | None = None,
        lot_size: int = 1,
    ) -> int | None:
        """Return the reprice target for one listing, or ``None`` when it stays put."""
        if market_price <= 0:
            return None

        candidate = market_price + self.offset

        floor = self.floor_for(gid)
        if floor is not None:
            candidate = max(candidate, floor)

        if self.maximum_drop_percent is not None and current_price > 0:
            lowest_allowed = math.ceil(current_price * (1.0 - self.maximum_drop_percent))
            candidate = max(candidate, lowest_allowed)

        if average_price and average_price > 0:
            # average_price from game data is per unit; market/current prices are for the lot.
            lot_average = average_price * lot_size
            if self.max_below_average is not None:
                if candidate < math.ceil(lot_average * (1.0 - self.max_below_average)):
                    return None
            if self.max_above_average is not None:
                if candidate > math.ceil(lot_average * (1.0 + self.max_above_average)):
                    return None

        if candidate < 1 or candidate == current_price:
            return None
        return candidate
