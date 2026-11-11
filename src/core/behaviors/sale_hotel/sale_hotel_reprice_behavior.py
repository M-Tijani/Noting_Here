from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from functools import partial

from DBDofusUnity.datas.protos.non_obf.game.exchange_pb2 import (
    ExchangeBidHouseItemRemovedEvent,
    ExchangeBidHousePriceRequest,
    ExchangeBidPriceEvent,
    ExchangeBidSellerStartedEvent,
    ExchangeObjectModifyPricedRequest,
)
from DBDofusUnity.dofus_unity_reader.game_constants.item import CategoryItemEnum
from DBDofusUnity.dofus_unity_reader.game_constants.sale_hotel import (
    SALE_HOTEL_LISTING_FEE,
    QuantityEnum,
)

from src.controller.game_data import GameDataController
from src.core.behaviors.behavior import Behavior
from src.core.behaviors.sale_hotel.enter_sale_hotel_sell_behavior import (
    EnterSaleHotelSellBehavior,
)
from src.core.engine.economy.hdv_sell_bounds import HdvRepriceConfig
from src.core.engine.economy.sale_hotel import get_price_for_sale_hotel
from src.services.human_timings import HumanTimingsService

ItemToSellInBid = ExchangeBidSellerStartedEvent.ItemToSellInBid


@dataclass
class SaleHotelRepriceBehavior(Behavior):
    """Reprices the listings of the sale hotel currently open in sell mode.

    Places every listing one kama under the cheapest competing price of its lot,
    honouring a per-lot floor and an optional maximum drop per pass. Mirrors the
    MoonBot ``hdv.undercutAll`` / ``hdv.updateAllItems`` behaviour and finishes with
    a ``summary`` of ``{"updated", "skipped", "errors"}`` listings.

    In watch-only mode nothing is modified: each listing is polled once, the
    observed market/own/average prices are logged and returned in the summary
    under ``prices``, and the sale hotel dialog is left open.
    """

    enter_sale_hotel_sell_behavior: EnterSaleHotelSellBehavior

    _config: HdvRepriceConfig = field(init=False, default_factory=HdvRepriceConfig)
    _summary: dict[str, int] = field(
        init=False, default_factory=lambda: {"updated": 0, "skipped": 0, "errors": 0}
    )
    _items_by_gid: dict[int, list[ItemToSellInBid]] = field(
        init=False, default_factory=dict[int, list[ItemToSellInBid]]
    )
    _pending_gids: deque[int] = field(init=False, default_factory=deque[int])
    _current_gid: int = field(init=False, default=0)
    _sent_requests: int = field(init=False, default=0)
    _watch_only: bool = field(init=False, default=False)
    _watched: dict[int, dict[str, object]] = field(
        init=False, default_factory=dict[int, dict[str, object]]
    )

    def run(
        self,
        config: HdvRepriceConfig | None = None,
        category: CategoryItemEnum = CategoryItemEnum.RESOURCES,
        open_only: bool = False,
        watch_only: bool = False,
    ) -> None:
        self._summary = {"updated": 0, "skipped": 0, "errors": 0}
        self._watched = {}
        self._watch_only = watch_only
        if open_only:
            self.enter_sale_hotel_sell_behavior.start(
                callback=self._on_sale_hotel_opened,
                parent=self,
                category=category,
            )
            return

        self._config = config or HdvRepriceConfig()
        self._start_reprice_pass()

    def _on_sale_hotel_opened(self, error_code: str | None, **_kwargs: object) -> None:
        # EnterSaleHotelSellBehavior finishes with items=...; accept and ignore it.
        self.raise_if_error(error_code)
        self.finish(summary=self._summary)

    def _start_reprice_pass(self) -> None:
        self._items_by_gid = {}
        for listing in self.game_state.sale_hotel.items_in_sale:
            if (
                self._config.lot_size is not None
                and listing.item.quantity != int(self._config.lot_size)
            ):
                continue
            self._items_by_gid.setdefault(listing.item.gid, []).append(listing)

        self._pending_gids = deque(self._items_by_gid)
        if not self._pending_gids:
            self.logger.info("No listing to reprice, sale hotel not open in sell mode?")
            return self.finish(summary=self._summary)

        self._next_gid()

    def _next_gid(self) -> None:
        if not self._pending_gids:
            summary: dict[str, object] = dict(self._summary)
            if self._watch_only:
                summary["prices"] = self._watched
            self.logger.info(f"Reprice pass done: {summary}")
            return self.finish(summary=summary)

        self._current_gid = self._pending_gids.popleft()
        self.event_manager.on(
            ExchangeBidPriceEvent,
            self._on_exchange_bid_price_event,
            originator=self,
            once=True,
        )
        self.logger.info(f"Repricing item {self._current_gid}")
        req = ExchangeBidHousePriceRequest(object_gid=self._current_gid)
        self.send_message_delayed(req, HumanTimingsService().get_timing_sale_hotel_review())

    def _on_exchange_bid_price_event(self, msg: ExchangeBidPriceEvent) -> None:
        minimal_prices = list(msg.bid_price_for_seller.minimal_prices)
        if self._watch_only:
            return self._record_watched_prices(minimal_prices, msg.average_price)
        listings = self._items_by_gid.get(self._current_gid, [])
        own_listings = (
            GameDataController()
            .get_hdv_by_uid_by_player(self.game_state.player.server_id)
            .get(self.game_state.player.character_id, {})
        )

        requests_modify_price: list[ExchangeObjectModifyPricedRequest] = []
        price_cost: float = 0
        for listing in listings:
            lot = int(QuantityEnum(listing.item.quantity))
            market_price = get_price_for_sale_hotel(minimal_prices, QuantityEnum(lot))
            own_entry = own_listings.get(listing.item.uid)
            if own_entry is None:
                self.logger.info(
                    f"Item {self._current_gid} lot {lot}: market={market_price} not listed anymore"
                )
                self._summary["skipped"] += 1
                continue

            current_price = own_entry[2]
            new_price = self._config.new_price(
                self._current_gid,
                current_price,
                market_price,
                msg.average_price,
                lot_size=lot,
            )
            if new_price is None:
                self.logger.info(
                    f"Item {self._current_gid} lot {lot}: market={market_price} "
                    f"own={current_price} unchanged"
                )
                self._summary["skipped"] += 1
                continue

            self.logger.info(
                f"Item {self._current_gid} lot {lot}: market={market_price} "
                f"own={current_price} -> {new_price}"
            )
            price_cost += new_price * SALE_HOTEL_LISTING_FEE
            if price_cost > self.game_state.inventory.kamas:
                self._summary["skipped"] += 1
                break

            requests_modify_price.append(
                ExchangeObjectModifyPricedRequest(
                    object_uid=listing.item.uid,
                    quantity=listing.item.quantity,
                    price=new_price,
                )
            )

        if not requests_modify_price:
            self.logger.info(f"Item {self._current_gid}: nothing to update")
            return self._next_gid()

        self._sent_requests = len(requests_modify_price)
        self.event_manager.on(
            ExchangeBidHouseItemRemovedEvent,
            partial(
                self._on_item_removed_event,
                target_uid=requests_modify_price[-1].object_uid,
            ),
            originator=self,
        )

        pending_requests = deque(requests_modify_price)

        def send_next_modify_price_request() -> None:
            self.event_manager.send(pending_requests.popleft())
            if pending_requests:
                self.run_timer(
                    HumanTimingsService().get_timing_sale_hotel_price_change(),
                    send_next_modify_price_request,
                )

        self.run_timer(
            HumanTimingsService().get_timing_sale_hotel_price_change(),
            send_next_modify_price_request,
        )

    def _record_watched_prices(self, minimal_prices: list[int], average_price: float) -> None:
        gid = self._current_gid
        own_listings = (
            GameDataController()
            .get_hdv_by_uid_by_player(self.game_state.player.server_id)
            .get(self.game_state.player.character_id, {})
        )
        own_price_by_lot: dict[int, int] = {}
        for entry_gid, entry_quantity, entry_price in own_listings.values():
            if entry_gid != gid:
                continue
            lot = int(QuantityEnum(entry_quantity))
            current = own_price_by_lot.get(lot)
            if current is None or entry_price < current:
                own_price_by_lot[lot] = entry_price

        lots: dict[int, dict[str, int]] = {}
        for listing in self._items_by_gid.get(gid, []):
            lot = int(QuantityEnum(listing.item.quantity))
            lots[lot] = {
                "market": get_price_for_sale_hotel(minimal_prices, QuantityEnum(lot)),
                "own": own_price_by_lot.get(lot, 0),
            }

        for lot, info in sorted(lots.items()):
            self.logger.info(
                f"Watched {gid} lot {lot}: market={info['market']} own={info['own']} "
                f"avg={int(average_price)}"
            )
        self._watched[gid] = {"average": average_price, "lots": lots}
        self._next_gid()

    def _on_item_removed_event(self, msg: ExchangeBidHouseItemRemovedEvent, target_uid: int) -> None:
        if msg.sell_id != target_uid:
            return
        self.unregister_listener(
            ExchangeBidHouseItemRemovedEvent,
            reason="Repriced one item, proceeding to the next",
        )
        self._summary["updated"] += self._sent_requests
        self.run_timer(
            HumanTimingsService().get_timing_sale_hotel_review(),
            self._next_gid,
        )
