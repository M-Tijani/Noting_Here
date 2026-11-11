from dataclasses import dataclass, field

from DBDofusUnity.datas.protos.non_obf.game.dialog_pb2 import DialogLeaveRequest
from DBDofusUnity.datas.protos.non_obf.game.exchange_pb2 import (
    ExchangeLeaveEvent,
    ExchangeObjectTransferAllFromInventoryRequest,
    ExchangeObjectsAddedEvent,
    ExchangePlayerRequest,
    ExchangeReadyRequest,
    ExchangeStartedWithPodsEvent,
)

from src.core.behaviors.behavior import Behavior
from src.core.states.dialog_state import OpenDialogKind
from src.services.human_timings import HumanTimingsService

EXCHANGE_START_TIMEOUT_SECONDS = 10.0
EXCHANGE_COMPLETE_TIMEOUT_SECONDS = 20.0

MULE_EXCHANGE_START_TIMEOUT = "MULE_EXCHANGE_START_TIMEOUT"
MULE_EXCHANGE_INCOMPLETE = "MULE_EXCHANGE_INCOMPLETE"


@dataclass
class MuleExchangeBehavior(Behavior):
    """Farmer side of a trade: opens an exchange with a player and gives the whole bag.

    Mirrors the bank deposit (``ExchangeObjectTransferAllFromInventoryRequest``) but
    against another player instead of a chest, so the receiving side must accept the
    trade for it to complete.
    """

    _step: int = field(init=False, default=0)
    _ready_scheduled: bool = field(init=False, default=False)

    def run(self, target_id: int) -> None:
        self._step = 0
        self._ready_scheduled = False
        if not self.game_state.inventory.get_unlinked_objects():
            self.logger.info("Nothing transferable in the bag, skipping the mule exchange")
            return self.finish()
        self.logger.info(f"Opening a trade with player {target_id} to give the inventory")
        self.event_manager.on(
            ExchangeStartedWithPodsEvent,
            self.on_exchange_started,
            originator=self,
            once=True,
            override_on_self=True,
            timeout=EXCHANGE_START_TIMEOUT_SECONDS,
            on_timeout=lambda: self.finish(MULE_EXCHANGE_START_TIMEOUT),
        )
        self.send_message_delayed(
            ExchangePlayerRequest(target_id=int(target_id)),
            HumanTimingsService().get_timing_base_action(),
        )

    def on_exchange_started(self, msg: ExchangeStartedWithPodsEvent) -> None:
        self.event_manager.on(
            ExchangeObjectsAddedEvent,
            self.on_objects_added,
            originator=self,
            override_on_self=True,
        )
        self.send_message_delayed(
            ExchangeObjectTransferAllFromInventoryRequest(),
            HumanTimingsService().get_timing_base_action(),
        )

    def on_objects_added(self, msg: ExchangeObjectsAddedEvent) -> None:
        self._step += 1
        if self._ready_scheduled:
            return
        self._ready_scheduled = True
        self.run_timer(HumanTimingsService().get_timing_base_action(), self.ready_exchange)

    def ready_exchange(self) -> None:
        self.unregister_listener(ExchangeObjectsAddedEvent)
        self.event_manager.on(
            ExchangeLeaveEvent,
            lambda _: self.finish(),
            originator=self,
            once=True,
            override_on_self=True,
            timeout=EXCHANGE_COMPLETE_TIMEOUT_SECONDS,
            on_timeout=lambda: self.finish(MULE_EXCHANGE_INCOMPLETE),
        )
        step = max(1, self._step)
        self.send_message_delayed(
            ExchangeReadyRequest(ready=True, step=step),
            HumanTimingsService().get_timing_base_action(),
        )

    def clear_behavior(self) -> None:
        if self.game_state.dialog.is_open(OpenDialogKind.PLAYER_EXCHANGE):
            self.event_manager.send(DialogLeaveRequest())
        super().clear_behavior()
