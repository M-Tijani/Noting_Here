from dataclasses import dataclass
from typing import Any

from google.protobuf.message import Message

from DBDofusUnity.datas.protos.non_obf.game.fight_pb2 import (
    FightEndEvent,
    FightTurnFinishRequest,
    FightTurnReadyRequest,
)
from DBDofusUnity.datas.protos.non_obf.game.fight_preparation_pb2 import (
    FightJoinRequest,
    FightPlacementPositionRequest,
    FightPlacementSwapPositionsAcceptRequest,
    FightPlacementSwapPositionsCancelRequest,
    FightReadyRequest,
    FightStartingEvent,
)
from DBDofusUnity.datas.protos.non_obf.game.game_action_pb2 import (
    GameActionFightCastOnTargetRequest,
    GameActionFightCastRequest,
)
from DBDofusUnity.datas.protos.non_obf.game.gamemap_pb2 import (
    MapComplementaryInformationEvent,
    MapMovementCancelRequest,
    MapMovementConfirmRequest,
    MapMovementRequest,
)


def extract_client_action(msg: Message) -> dict[str, Any] | None:
    if isinstance(msg, GameActionFightCastRequest):
        return {"type": "cast", "spell_id": msg.spell_id, "cell_id": msg.cell}
    if isinstance(msg, GameActionFightCastOnTargetRequest):
        return {"type": "cast_on_target", "spell_id": msg.spell_id, "target_id": msg.target_id}
    if isinstance(msg, MapMovementRequest):
        key_cells = list(msg.key_cells)
        return {
            "type": "move",
            "map_id": msg.map_id,
            "cautious": msg.cautious,
            "key_cells": key_cells,
            "destination_cell_id": key_cells[-1] if key_cells else None,
        }
    if isinstance(msg, MapMovementCancelRequest):
        return {"type": "move_cancel", "cell_id": msg.cell_id}
    if isinstance(msg, MapMovementConfirmRequest):
        return {"type": "move_confirm"}
    if isinstance(msg, FightTurnFinishRequest):
        return {"type": "pass_turn", "is_afk": msg.is_afk}
    if isinstance(msg, FightTurnReadyRequest):
        return {"type": "turn_ready", "is_ready": msg.is_ready}
    if isinstance(msg, FightReadyRequest):
        return {"type": "ready", "is_ready": msg.is_ready}
    if isinstance(msg, FightPlacementPositionRequest):
        return {"type": "placement", "cell_id": msg.cell_id, "entity_id": msg.entity_id}
    if isinstance(msg, FightPlacementSwapPositionsAcceptRequest):
        return {"type": "placement_swap_accept", "request_id": msg.request_id}
    if isinstance(msg, FightPlacementSwapPositionsCancelRequest):
        return {"type": "placement_swap_cancel", "request_id": msg.request_id}
    if isinstance(msg, FightJoinRequest):
        return {"type": "join", "fighter_id": msg.fighter_id, "fight_id": msg.fight_id}
    return None


def extract_fight_lifecycle(msg: Message, *, in_fight: bool) -> str | None:
    if isinstance(msg, FightStartingEvent):
        return "fight_start"
    if isinstance(msg, MapComplementaryInformationEvent) and in_fight:
        return "fight_end"
    return None


def extract_fight_id(msg: Message) -> int | None:
    if isinstance(msg, FightStartingEvent):
        return msg.fight_id
    return None


@dataclass(frozen=True, slots=True)
class FightOutcome:
    result: str
    duration_seconds: int
    turns: int
    kamas: int
    experience: int
    alive: bool | None
    loot: tuple[tuple[int, int], ...]


_OUTCOME_NAMES = {
    0: "lost",
    1: "draw",
    2: "victory",
    3: "tax",
    4: "defender_group",
}


def extract_fight_outcome(msg: Message, *, character_id: int, turns: int) -> FightOutcome | None:
    if not isinstance(msg, FightEndEvent) or character_id <= 0:
        return None

    own = next(
        (
            result
            for result in msg.results
            if result.HasField("fighter_list_entry")
            and result.fighter_list_entry.fighter_id == character_id
        ),
        None,
    )
    if own is None:
        return None

    fighter = own.fighter_list_entry
    experience = 0
    if fighter.HasField("player_list_entry"):
        for additional in fighter.player_list_entry.additional:
            if additional.HasField("experience_data"):
                experience = additional.experience_data.experience_fight_delta
                break

    return FightOutcome(
        result=_OUTCOME_NAMES.get(own.outcome, "unknown"),
        duration_seconds=msg.duration,
        turns=turns,
        kamas=own.rewards.kamas,
        experience=experience,
        alive=fighter.alive,
        loot=tuple((item.id, item.quantity) for item in own.rewards.objects),
    )
