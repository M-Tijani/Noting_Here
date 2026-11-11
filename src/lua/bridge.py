from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from functools import partial
from threading import Event
from typing import TYPE_CHECKING, Any, Protocol, cast

from DBDofusUnity.datas.protos.non_obf.game.common_pb2 import ActorPositionInformation
from DBDofusUnity.dofus_unity_reader.data_center.data_reader import DataReader
from DBDofusUnity.dofus_unity_reader.data_center.i18n import I18N
from DBDofusUnity.dofus_unity_reader.data_center.map_reader import MapReader
from DBDofusUnity.dofus_unity_reader.game_constants.sale_hotel import (
    SALE_HOTEL_LISTING_FEE,
    QuantityEnum,
)
from DBDofusUnity.dofus_unity_reader.grid.map_point import MapPoint

from src.consts import FAKE_INFINITY_VALUE
from src.controller.game_data import GameDataController
from src.core.behaviors.behavior import Behavior
from src.core.behaviors.farms.fight.attacker_behavior import AttackerBehavior
from src.core.behaviors.interactives.collect_behavior import CollectBehavior
from src.core.behaviors.movements.auto_trip.auto_trip_behavior import AutoTripBehavior
from src.core.behaviors.movements.map_move_behavior import MapMoveBehavior
from src.core.bot.controller_hub import BotCommand, command_to_payload, controller_hub
from src.core.bot.team_controller import team_controller
from src.core.engine.economy.hdv_sell_bounds import HdvRepriceConfig, HdvSellTarget
from src.core.engine.monsters.monster_group import get_monster_group_size
from src.core.engine.movements.map.path_finding.movement_path import MovementPath
from src.core.engine.movements.map.path_finding.path_finding import Pathfinding
from src.core.engine.movements.world.map_position import get_dist_to_maps, get_map_id_by_coords
from src.core.engine.npcs.dialog_texts import get_question_text, get_reply_text
from src.core.frames.sale_hotel_frame import SELLABLE_ITEMS
from src.core.states.challenge_state import ChallengeSelectionError
from src.core.states.dialog_state import OpenDialogKind
from src.core.states.game_state import GameState
from src.lua.behavior_runner import run_behavior_blocking, wait_for_defense
from src.lua.errors import ScriptCancelled, ScriptError
from src.lua.runtime import ScriptBinding, ScriptBindings
from src.services.logging_utils.loggers import BotLogger

if TYPE_CHECKING:
    from src.core.bot.bot import Bot


def _as_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ScriptError(f"invalid {label}: {value!r}")
    try:
        return int(value)
    except ValueError as error:
        raise ScriptError(f"invalid {label}: {value!r}") from error


ScriptBehavior = MapMoveBehavior | CollectBehavior | AttackerBehavior | AutoTripBehavior


class ScriptActions(Protocol):
    """Declarative ``move()`` engine actions implemented by the script behavior."""

    def map_id(self) -> int: ...

    def map_coords(self) -> str: ...

    def resolve_map(self, reference: object) -> int: ...

    def goto_map(self, map_id: int) -> None: ...

    def gather(self, force: bool = False, spec: object = None) -> None: ...

    def fight(self, force: bool = False, spec: object = None, config: dict[str, object] | None = None) -> None: ...

    def start_fight(
        self,
        count: object = None,
        monster_ids: object = None,
        options: object = None,
    ) -> bool: ...

    def wait_fight(self, timeout: object = None) -> bool: ...

    def fight_is_in_fight(self) -> bool: ...

    def fight_is_my_turn(self) -> bool: ...

    def fight_state(self) -> dict[str, object]: ...

    def fight_placement_cells(self) -> list[int]: ...

    def fight_set_placement_cell(self, cell_id: object, timeout: object = None) -> bool: ...

    def fight_wait_for_placement(self, timeout: object = None) -> dict[str, object] | None: ...

    def fight_wait_for_my_turn(self, timeout: object = None) -> dict[str, object] | None: ...

    def fight_cast_spell(self, spell_id: object, cell_id: object) -> bool: ...

    def fight_move_to_cell(self, cell_id: object) -> bool: ...

    def fight_end_turn(self) -> bool: ...

    def fight_set_auto(self) -> bool: ...

    def use_door(self, cell: int) -> None: ...

    def use_path(self, path: str, fallback_map: object = None) -> None: ...

    def use_zaap(self, map_id: object) -> None: ...

    def change_map(self, direction: str) -> bool: ...

    def move_toward(self, pos_x: int, pos_y: int) -> bool: ...

    def move_toward_map(self, map_id: int) -> bool: ...

    def wait_for_map_change(self, timeout: float | None = None) -> bool: ...

    def wait_for_map_data(self) -> None: ...

    def use_interactive(self, cell_id: int, skill_index: int | None = None) -> None: ...

    def npc_talk(self, npc_id: int) -> None: ...

    def npc_reply(self, reply_id: int) -> None: ...

    def npc_reply_index(self, index: int) -> None: ...

    def deposit(self) -> None: ...

    def exchange_with_mule(self, player_id: int) -> None: ...

    def job_level(self, job_id: int) -> int: ...

    def can_use_bank(self) -> bool: ...

    def dialog_leave(self) -> None: ...

    def finish_script(self) -> None: ...

    def sale_auto(self) -> None: ...

    def hdv_auto_sell(self) -> bool: ...

    def hdv_open_sell(self, category: object = None) -> bool: ...

    def hdv_close(self) -> bool: ...

    def hdv_reprice(self, config: HdvRepriceConfig) -> dict[str, object]: ...

    def hdv_watch_prices(self, config: HdvRepriceConfig) -> dict[str, object]: ...

    def hdv_sell_items(self, targets: dict[int, HdvSellTarget]) -> bool: ...

    def hdv_sell_inventory_items(self, targets: dict[int, HdvSellTarget]) -> bool: ...

    def invite_to_group(self, name: object) -> bool: ...

    def leave_group(self) -> bool: ...

    def accept_group_invite(self) -> bool: ...

    def decline_group_invite(self) -> bool: ...

    def wait_for_group_invite(self, timeout: object = None) -> dict[str, object] | None: ...

    def wait_for_group_command(self, timeout: object = None) -> dict[str, object] | None: ...

    def inventory_items(self) -> list[dict[str, object]]: ...

    def destroy_item(self, uid: object, quantity: object = None) -> bool: ...

    def equip_item(self, uid: object, position: object = None) -> bool: ...

@dataclass
class BehaviorScriptBridge:
    """Exposes the bot's behavior primitives to a Lua script as blocking calls."""

    game_state: GameState
    logger: BotLogger
    path_finding: Pathfinding
    map_move_behavior: MapMoveBehavior
    collect_behavior: CollectBehavior
    attacker_behavior: AttackerBehavior
    auto_trip_behavior: AutoTripBehavior
    parent: Behavior
    cancel_event: Event
    defense_event: Event | None = None
    actions: ScriptActions | None = None
    _last_seen_map_id: int | None = field(init=False, default=None)

    def bindings(self) -> ScriptBindings:
        bindings: dict[str, ScriptBinding] = {
            "log": self.log,
            "sleep": self.sleep,
            "game.map_id": self.map_id,
            "game.cell_id": self.cell_id,
            "game.character_id": self.character_id,
            "game.in_fight": self.in_fight,
            "game.full_pods": self.full_pods,
            "game.inventory_weight": self.inventory_weight,
            "game.inventory_weight_max": self.inventory_weight_max,
            "move.to_cell": self.move_to_cell,
            "move.to_map": self.move_to_map,
            "move.to_door": self.move_to_door,
            "gather.on_map": self.gather_on_map,
            "fight.monsters": self.fight_monsters,
            "fight.setAllowedSpells": self.fight_set_allowed_spells,
        }
        bindings.update(self._map_bindings())
        bindings.update(self._fight_bindings())
        bindings.update(self._challenge_bindings())
        bindings.update(self._npc_bindings())
        bindings.update(self._hdv_bindings())
        bindings.update(self._group_bindings())
        bindings.update(self._team_bindings())
        bindings.update(self._controller_bindings())
        if self.actions is not None:
            bindings.update(
                {
                    "game.map_coords": self.actions.map_coords,
                    "game.job_level": self.actions.job_level,
                    "game.can_use_bank": self.actions.can_use_bank,
                    "dialog.leave": self.actions.dialog_leave,
                    "script.finish": self.actions.finish_script,
                    "bank.deposit": self.actions.deposit,
                    "mule.exchange": self.actions.exchange_with_mule,
                    "__map_id": self.actions.map_id,
                    "__resolve_map": self.actions.resolve_map,
                    "__action_goto": self.actions.goto_map,
                    "__action_gather": self.actions.gather,
                    "__action_fight": self.actions.fight,
                    "__action_door": self.actions.use_door,
                    "__action_path": self.actions.use_path,
                    "__action_deposit": self.actions.deposit,
                    "__action_sale": self.actions.sale_auto,
                    "__full_pods": self.full_pods,
                    "npc.talk": self.actions.npc_talk,
                    "inventory.items": self.actions.inventory_items,
                    "inventory.destroy": self.actions.destroy_item,
                    "inventory.equip": self.actions.equip_item,
                }
            )
        return bindings

    def _map_bindings(self) -> dict[str, ScriptBinding]:
        bindings: dict[str, ScriptBinding] = {
            "map.currentMap": self.map_current_map,
            "map.currentMapId": self.map_id,
            "map.id": self.map_id,
            "map.x": self.map_x,
            "map.y": self.map_y,
            "map.currentCell": self.cell_id,
            "map.cellId": self.cell_id,
            "map.currentArea": self.map_current_area,
            "map.currentSubArea": self.map_current_sub_area,
            "map.onMap": self.map_on_map,
            "map.worldDistance": self.map_world_distance,
            "map.worldDistanceToMap": self.map_world_distance_to_map,
            "map.getX": self.map_get_x,
            "map.getY": self.map_get_y,
            "map.neighbourId": self.map_neighbour_id,
            "map.monsterGroups": self.map_monster_groups,
            "map.players": self.map_players,
            "map.npcs": self.map_npcs,
            "map.getActors": self.map_get_actors,
            "map.getActorById": self.map_get_actor_by_id,
            "map.getResources": self.map_get_resources,
            "map.interactiveObjects": self.map_interactive_objects,
            "map.isWalkable": self.map_is_walkable,
            "map.getWalkableCells": self.map_get_walkable_cells,
            "map.hasChanged": self.map_has_changed,
            "map.zoneInfo": self.map_zone_info,
            "map.moveToCell": self.move_to_cell,
            "map.moveTo": self.move_to_cell,
            "map.gather": self.map_gather,
            "map.gatherResource": self.map_gather_resource,
            "map.fight": self.map_fight,
            "map.tryFight": self.map_fight,
            "map.fightGroup": self.map_fight_group,
            "map.tryFightGroup": self.map_fight_group,
            "map.isInParty": self.map_is_in_party,
            "map.getPartyCount": self.map_party_count,
            "map.getPartyLeaderId": self.map_party_leader_id,
            "map.isPartyLeader": self.map_is_party_leader,
            "map.getPartyMembers": self.map_party_members,
            "map.getPendingGroupInvite": self.map_pending_group_invite,
        }
        unsupported = (
            "dimensions",
            "width",
            "height",
            "tryFightGroupOnCell",
            "houseInstances",
            "enterHouse",
            "openHouseChest",
            "openZaap",
            "openZaapi",
            "useZaapi",
            "getZaapDestinations",
            "saveZaap",
            "closeZaap",
            "getPrisms",
            "openPrism",
            "getPrismDestinations",
            "usePrism",
            "closePrism",
            "sendEmote",
            "sit",
            "slap",
            "challengePlayer",
            "acceptChallenge",
            "declineChallenge",
            "cancelChallenge",
            "aggressPlayer",
            "getPendingChallenge",
            "waitForChallenge",
            "joinableFights",
            "joinFight",
            "exchangePlayer",
        )
        for name in unsupported:
            bindings[f"map.{name}"] = partial(self._unsupported, f"map.{name}")
        if self.actions is not None:
            bindings.update(
                {
                    "map.changeMap": self.actions.change_map,
                    "map.moveToward": self.actions.move_toward,
                    "map.moveTowardMap": self.actions.move_toward_map,
                    "map.waitForMapChange": self.actions.wait_for_map_change,
                    "map.useInteractive": self.actions.use_interactive,
                    "map.useZaap": self.actions.use_zaap,
                    "map.zap": self.actions.use_zaap,
                    "map.inviteToGroup": self.actions.invite_to_group,
                    "map.leaveGroup": self.actions.leave_group,
                    "map.acceptGroupInvite": self.actions.accept_group_invite,
                    "map.declineGroupInvite": self.actions.decline_group_invite,
                    "map.waitForGroupInvite": self.actions.wait_for_group_invite,
                }
            )
        return bindings

    def _fight_bindings(self) -> dict[str, ScriptBinding]:
        if self.actions is None:
            return {}
        return {
            "fight.start": self.actions.start_fight,
            "fight.wait": self.actions.wait_fight,
            "fight.isInFight": self.actions.fight_is_in_fight,
            "fight.isMyTurn": self.actions.fight_is_my_turn,
            "fight.getState": self.actions.fight_state,
            "fight.getPlacementCells": self.actions.fight_placement_cells,
            "fight.setPlacementCell": self.actions.fight_set_placement_cell,
            "fight.placeOnCell": self.actions.fight_set_placement_cell,
            "fight.waitForPlacement": self.actions.fight_wait_for_placement,
            "fight.waitForMyTurn": self.actions.fight_wait_for_my_turn,
            "fight.castSpell": self.actions.fight_cast_spell,
            "fight.moveToCell": self.actions.fight_move_to_cell,
            "fight.moveTo": self.actions.fight_move_to_cell,
            "fight.moveTowardCell": self.actions.fight_move_to_cell,
            "fight.move": self.actions.fight_move_to_cell,
            "fight.endTurn": self.actions.fight_end_turn,
            "fight.passTurn": self.actions.fight_end_turn,
            "fight.setAuto": self.actions.fight_set_auto,
        }

    def _challenge_bindings(self) -> dict[str, ScriptBinding]:
        return {
            "challenge.list": self.challenge_list,
            "challenge.easyList": self.challenge_easy_list,
            "challenge.getList": self.challenge_get_list,
            "challenge.setList": self.challenge_set_list,
            "challenge.setEasyList": self.challenge_set_easy_list,
            "challenge.getSelected": self.challenge_get_selected,
            "challenge.getProposalIds": self.challenge_get_proposal_ids,
        }

    def challenge_list(self, easy_only: object = False) -> list[dict[str, object]]:
        if not isinstance(easy_only, bool):
            raise ScriptError(f"invalid easy_only flag: {easy_only!r}")
        return [
            definition.to_dict()
            for definition in self.game_state.challenge.list_definitions(easy_only=easy_only)
        ]

    def challenge_easy_list(self) -> list[dict[str, object]]:
        return [
            definition.to_dict()
            for definition in self.game_state.challenge.list_definitions(easy_only=True)
        ]

    def challenge_get_list(self) -> list[int]:
        return list(self.game_state.challenge.preferred_ids())

    def challenge_set_list(self, challenge_ids: object = None) -> list[int]:
        if challenge_ids is None:
            raise ScriptError("challenge.setList expects a table of challenge ids or names")
        if not isinstance(challenge_ids, (list, tuple)):
            raise ScriptError(f"invalid challenge list: {challenge_ids!r}")
        entries = cast("list[object] | tuple[object, ...]", challenge_ids)

        state = self.game_state.challenge
        definitions = state.list_definitions()
        by_name = {definition.name.casefold(): definition.id for definition in definitions}
        resolved_ids: list[int] = []
        for value in entries:
            if isinstance(value, bool):
                raise ScriptError(f"invalid challenge entry: {value!r}")
            if isinstance(value, int):
                resolved_ids.append(value)
                continue
            if isinstance(value, str):
                challenge_id = by_name.get(value.casefold())
                if challenge_id is None:
                    raise ScriptError(f"unknown challenge name: {value}")
                resolved_ids.append(challenge_id)
                continue
            raise ScriptError(f"invalid challenge entry: {value!r}")

        try:
            selected_ids = state.set_preferred_ids(resolved_ids)
        except ChallengeSelectionError as error:
            raise ScriptError(str(error)) from error
        self.logger.info("Lua challenge list: %s", list(selected_ids))
        return list(selected_ids)

    def challenge_set_easy_list(self) -> list[int]:
        easy_ids = self.game_state.challenge.default_preferred_ids()
        try:
            selected_ids = self.game_state.challenge.set_preferred_ids(easy_ids)
        except ChallengeSelectionError as error:
            raise ScriptError(str(error)) from error
        self.logger.info("Lua challenge list reset to easy challenges: %s", list(selected_ids))
        return list(selected_ids)

    def challenge_get_selected(self) -> dict[str, object] | None:
        selected = self.game_state.challenge.selected()
        return None if selected is None else selected.to_dict()

    def challenge_get_proposal_ids(self) -> list[int]:
        return list(self.game_state.challenge.proposal_ids())

    def _npc_bindings(self) -> dict[str, ScriptBinding]:
        bindings: dict[str, ScriptBinding] = {
            "npc.getQuestionId": self.npc_get_question_id,
            "npc.getResponses": self.npc_get_responses,
            "npc.getResponseTexts": self.npc_get_response_texts,
            "npc.getQuestionText": self.npc_get_question_text,
            "npc.isInDialog": self.npc_is_in_dialog,
            "npc.getNpcIdOnCell": self.npc_get_npc_id_on_cell,
            "npc.getNpcId": self.npc_get_npc_id,
        }
        for name in ("npcBank", "exchange", "openCoffre"):
            bindings[f"npc.{name}"] = partial(self._unsupported, f"npc.{name}")
        if self.actions is not None:
            bindings.update(
                {
                    "npc.npc": self.actions.npc_talk,
                    "npc.reply": self.actions.npc_reply_index,
                    "npc.respond": self.actions.npc_reply,
                    "npc.SendReply": self.actions.npc_reply,
                    "npc.leaveDialog": self.actions.dialog_leave,
                    "npc.closeDialog": self.actions.dialog_leave,
                }
            )
        return bindings

    def _hdv_bindings(self) -> dict[str, ScriptBinding]:
        bindings: dict[str, ScriptBinding] = {
            "hdv.getTax": self.hdv_get_tax,
            "hdv.getLotSizes": self.hdv_get_lot_sizes,
            "hdv.getMiddlePrice": self.hdv_get_middle_price,
            "hdv.getLowestPrice": self.hdv_get_lowest_price,
            "hdv.itemPrices": self.hdv_item_prices,
            "hdv.myListingCount": self.hdv_my_listing_count,
            "hdv.myListings": self.hdv_my_listings,
            "hdv.myListingUid": self.hdv_my_listing_uid,
            "hdv.myListingItemId": self.hdv_my_listing_item_id,
            "hdv.myListingPrice": self.hdv_my_listing_price,
            "hdv.myListingQty": self.hdv_my_listing_qty,
            "hdv.myFreeSlots": self.hdv_my_free_slots,
            "hdv.sellableItems": self.hdv_sellable_items,
            "hdv.canSellItem": self.hdv_can_sell_item,
            "hdv.undercutAll": self.hdv_undercut_all,
            "hdv.updateAllItems": self.hdv_update_all_items,
            "hdv.watchPrices": self.hdv_watch_prices,
            "hdv.sellItems": self.hdv_sell_items,
            "hdv.sellInventoryItems": self.hdv_sell_inventory_items,
            "hdv.sellItem": self.hdv_sell_item,
        }
        unsupported = (
            "openBuy",
            "listItem",
            "refreshListings",
            "offers",
            "listingsWithJets",
            "inspectMount",
            "buyItem",
            "buyListing",
            "buyOffer",
            "buyByUid",
            "updatePrice",
            "removeListing",
            "listCategory",
            "getCategoryItems",
            "getAllowedTypes",
            "maxItems",
            "hdvType",
            "isTypeAllowed",
        )
        for name in unsupported:
            bindings[f"hdv.{name}"] = partial(self._unsupported, f"hdv.{name}")
        if self.actions is not None:
            bindings.update(
                {
                    "hdv.autoSell": self.actions.hdv_auto_sell,
                    "hdv.openSell": self.actions.hdv_open_sell,
                    "hdv.close": self.actions.hdv_close,
                }
            )
        return bindings

    def _group_bindings(self) -> dict[str, ScriptBinding]:
        bindings: dict[str, ScriptBinding] = {
            "group.isInGroup": self.group_is_in_group,
            "group.isLeader": self.group_is_leader,
            "group.isMule": self.group_is_mule,
            "group.getId": self.group_id,
            "group.getName": self.group_name,
            "group.getLeaderId": self.group_leader_id,
            "group.getMemberCount": self.map_party_count,
            "group.getMemberIds": self.group_member_ids,
            "group.getMembers": self.map_party_members,
            "group.allOnSameMap": self.group_all_on_same_map,
            "group.getCommand": self.group_get_command,
            "group.sendCommandToMules": self.group_send_command_to_mules,
            "group.sendCommandToLeader": self.group_send_command_to_leader,
            "group.sendCommandToAll": self.group_send_command_to_all,
        }
        unsupported = (
            "list",
            "get",
            "create",
            "update",
            "remove",
            "delete",
            "moveTo",
            "stopMove",
            "isWaitingForMembers",
            "getMoveMode",
            "getActionMode",
            "getActionDelay",
            "getLeaderState",
            "getMuleStates",
            "getMemberState",
            "getTotalKamas",
        )
        for name in unsupported:
            bindings[f"group.{name}"] = partial(self._unsupported, f"group.{name}")
        if self.actions is not None:
            bindings["group.waitForCommand"] = self.actions.wait_for_group_command
        else:
            bindings["group.waitForCommand"] = partial(self._unsupported, "group.waitForCommand")
        return bindings

    def _team_bindings(self) -> dict[str, ScriptBinding]:
        bindings: dict[str, ScriptBinding] = {
            "team.isInTeam": self.team_is_in_team,
            "team.isLeader": self.team_is_leader,
            "team.isMember": self.team_is_member,
            "team.getLeader": self.team_get_leader,
            "team.getMembers": self.team_get_members,
            "team.getMemberCount": self.team_get_member_count,
            "team.getCommand": self.group_get_command,
            "team.sendCommandToMembers": self.team_send_command_to_members,
            "team.sendCommandToLeader": self.team_send_command_to_leader,
        }
        if self.actions is not None:
            bindings["team.waitForCommand"] = self.actions.wait_for_group_command
        else:
            bindings["team.waitForCommand"] = partial(self._unsupported, "team.waitForCommand")
        return bindings

    def _controller_bindings(self) -> dict[str, ScriptBinding]:
        return {
            "LavasmithController.getAllAccountControllers": self.controller_all,
            "LavasmithController.getConnectedAccountControllers": self.controller_connected,
            "LavasmithController.getAccountControllerByAlias": self.controller_by_username,
            "LavasmithController.getAccountControllerByUsername": self.controller_by_username,
            "LavasmithController.accountCount": self.controller_account_count,
            "LavasmithController.connectedAccountCount": self.controller_connected_count,
            "LavasmithController.myAccountId": self.current_login,
            "LavasmithController.sendCommand": self.controller_send_command,
            "LavasmithController.characterState": self.controller_character_state,
            "LavasmithController.inventory": self.controller_inventory,
        }

    def _my_listings(self) -> list[dict[str, int]]:
        listings = (
            GameDataController()
            .get_hdv_by_uid_by_player(self.game_state.player.server_id)
            .get(self.game_state.player.character_id, {})
        )
        return [
            {"uid": uid, "id": gid, "quantity": quantity, "price": price}
            for uid, (gid, quantity, price) in sorted(listings.items())
        ]

    def _my_listing_at(self, index: object) -> dict[str, int] | None:
        position = _as_int(index, "listing index")
        listings = self._my_listings()
        if position < 1 or position > len(listings):
            return None
        return listings[position - 1]

    def _known_min_price(self, gid: int, lot: int) -> int:
        return (
            GameDataController()
            .get_minimal_price_by_gid_and_quantity(self.game_state.player.server_id)
            .get((gid, lot), 0)
        )

    def _lot_size(self, lot_size: object) -> int:
        if lot_size is None:
            return int(QuantityEnum.VALUE_1)
        try:
            return int(QuantityEnum(_as_int(lot_size, "lot size")))
        except ValueError as error:
            raise ScriptError(f"invalid lot size: {lot_size!r}") from error

    def hdv_get_tax(self) -> float:
        return SALE_HOTEL_LISTING_FEE

    def hdv_get_lot_sizes(self) -> list[int]:
        return [int(quantity) for quantity in QuantityEnum]

    def hdv_get_middle_price(self, template_id: object) -> float:
        gid = _as_int(template_id, "item id")
        return GameDataController().get_avg_price_by_gid(self.game_state.player.server_id).get(gid, 0.0)

    def hdv_get_lowest_price(self, template_id: object, lot_size: object = None) -> int:
        return self._known_min_price(_as_int(template_id, "item id"), self._lot_size(lot_size))

    def hdv_item_prices(self, template_id: object) -> dict[int, int]:
        gid = _as_int(template_id, "item id")
        return {int(quantity): self._known_min_price(gid, int(quantity)) for quantity in QuantityEnum}

    def hdv_my_listing_count(self) -> int:
        return len(self._my_listings())

    def hdv_my_listings(self) -> list[dict[str, int]]:
        return self._my_listings()

    def hdv_my_listing_uid(self, index: object) -> int:
        listing = self._my_listing_at(index)
        return listing["uid"] if listing else 0

    def hdv_my_listing_item_id(self, index: object) -> int:
        listing = self._my_listing_at(index)
        return listing["id"] if listing else 0

    def hdv_my_listing_price(self, index: object) -> int:
        listing = self._my_listing_at(index)
        return listing["price"] if listing else 0

    def hdv_my_listing_qty(self, index: object) -> int:
        listing = self._my_listing_at(index)
        return listing["quantity"] if listing else 0

    def hdv_my_free_slots(self) -> int:
        condition = self.game_state.sale_hotel.bid_seller_condition
        if condition is None:
            return 0
        return max(0, int(condition.max_item_per_account) - len(self._my_listings()))

    def hdv_sellable_items(self) -> list[dict[str, object]]:
        sellable: list[dict[str, object]] = []
        for origin, container in (
            ("bag", self.game_state.inventory.objects_by_uid),
            ("bank", self.game_state.inventory.bank_objects_by_uid),
        ):
            for object_item in container.values():
                gid = object_item.item.gid
                if not self.hdv_can_sell_item(gid):
                    continue
                sellable.append(
                    {"id": gid, "quantity": object_item.item.quantity, "origin": origin}
                )
        return sellable

    def hdv_can_sell_item(self, template_id: object) -> bool:
        gid = _as_int(template_id, "item id")
        item_data = DataReader().item_by_id.get(gid)
        if item_data is None or not item_data.typeId:
            return False
        if self.game_state.player.is_sub:
            return True
        return gid in SELLABLE_ITEMS

    def hdv_sell_items(self, items: object) -> bool:
        if self.actions is None:
            raise ScriptError("the script engine is not attached, cannot sell items")
        return self.actions.hdv_sell_items(self._sell_targets(items))

    def hdv_sell_inventory_items(self, items: object) -> bool:
        if self.actions is None:
            raise ScriptError("the script engine is not attached, cannot sell items")
        return self.actions.hdv_sell_inventory_items(self._sell_targets(items))

    def hdv_sell_item(self, template_id: object, lot_size: object = None, price: object = None) -> bool:
        gid = _as_int(template_id, "item id")
        item_price = _as_int(price, "price")
        entry: dict[str, object] = {"id": gid, "lot": lot_size, "min": item_price, "max": item_price}
        return self.hdv_sell_items([entry])

    def _sell_targets(self, items: object) -> dict[int, HdvSellTarget]:
        if not isinstance(items, (list, tuple)):
            raise ScriptError("hdv.sellItems expects a list of { id, lot, min, max } tables")
        targets: dict[int, HdvSellTarget] = {}
        for entry in cast("list[object]", items):
            if not isinstance(entry, dict):
                raise ScriptError(f"invalid sell item: {entry!r}")
            typed = cast("dict[object, object]", entry)
            gid = _as_int(typed.get("id"), "item id")
            minimum = _as_int(typed.get("min", 1), "min price")
            maximum = _as_int(typed.get("max", minimum), "max price")
            max_lots_raw = typed.get("maxLots")
            max_lots = None
            if max_lots_raw is not None:
                max_lots = _as_int(max_lots_raw, "max lots count")
                if max_lots < 1:
                    raise ScriptError(f"invalid max lots count: {max_lots!r}")
            targets[gid] = HdvSellTarget(
                minimum=minimum,
                maximum=maximum,
                quantity=QuantityEnum(self._lot_size(typed.get("lot"))),
                max_below_average=self._percent(typed.get("maxBelowAverage")),
                max_above_average=self._percent(typed.get("maxAboveAverage")),
                max_lots=max_lots,
            )
        return targets

    def _floors(self, floors: object) -> tuple[int | None, dict[int, int]]:
        if floors is None:
            return None, {}
        if isinstance(floors, dict):
            typed = cast("dict[object, object]", floors)
            return (
                None,
                {
                    _as_int(gid, "item id"): _as_int(price, "floor price")
                    for gid, price in typed.items()
                },
            )
        return _as_int(floors, "floor price"), {}

    def _percent(self, value: object) -> float | None:
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ScriptError(f"invalid percentage: {value!r}")
        return float(value)

    def hdv_undercut_all(
        self,
        lot_size: object,
        max_drop_percent: object = None,
        floors: object = None,
        max_below_average: object = None,
        max_above_average: object = None,
    ) -> dict[str, object]:
        if self.actions is None:
            raise ScriptError("the script engine is not attached, cannot reprice the sale hotel")
        default_floor, floor_by_gid = self._floors(floors)
        config = HdvRepriceConfig(
            offset=-1,
            maximum_drop_percent=self._percent(max_drop_percent),
            max_below_average=self._percent(max_below_average),
            max_above_average=self._percent(max_above_average),
            lot_size=QuantityEnum(self._lot_size(lot_size)),
            default_floor=default_floor,
            floor_by_gid=floor_by_gid,
        )
        return self.actions.hdv_reprice(config)

    def hdv_update_all_items(
        self, offset: object = None, delay: object = None, minimums: object = None
    ) -> dict[str, object]:
        if self.actions is None:
            raise ScriptError("the script engine is not attached, cannot reprice the sale hotel")
        default_floor, floor_by_gid = self._floors(minimums)
        config = HdvRepriceConfig(
            offset=_as_int(offset, "offset") if offset is not None else -1,
            default_floor=default_floor,
            floor_by_gid=floor_by_gid,
        )
        return self.actions.hdv_reprice(config)

    def hdv_watch_prices(self, lot_size: object = None) -> dict[str, object]:
        if self.actions is None:
            raise ScriptError("the script engine is not attached, cannot watch the sale hotel")
        config = HdvRepriceConfig(
            lot_size=None if lot_size is None else QuantityEnum(self._lot_size(lot_size))
        )
        return self.actions.hdv_watch_prices(config)

    def _unsupported(self, name: str, *_args: object) -> Any:
        raise ScriptError(f"{name} is not supported by this bot yet")

    def log(self, message: object) -> None:
        self.logger.info(f"[lua] {message}")

    def sleep(self, seconds: float) -> None:
        wait_for_defense(self.defense_event, self.cancel_event)
        if self.cancel_event.wait(max(0.0, float(seconds))):
            raise ScriptCancelled()
        wait_for_defense(self.defense_event, self.cancel_event)

    def map_id(self) -> int:
        return self.game_state.map.map_id

    def cell_id(self) -> int:
        return self.game_state.map.map_point.cell_id

    def character_id(self) -> int:
        return self.game_state.player.character_id

    def in_fight(self) -> bool:
        return self.game_state.fight.in_fight

    def full_pods(self) -> bool:
        return self.game_state.inventory.is_full_pods

    def inventory_weight(self) -> int:
        return self.game_state.inventory.inventory_weight

    def inventory_weight_max(self) -> int:
        return self.game_state.inventory.weight_max

    def move_to_cell(self, cell_id: int) -> None:
        self.run_behavior(
            self.map_move_behavior,
            rebuild=lambda: {"move_path": self._compute_move_path(cell_id)},
            move_path=self._compute_move_path(cell_id),
        )

    def _compute_move_path(self, cell_id: int) -> MovementPath:
        target = MapPoint.from_cell_id(cell_id)
        move_path = self.path_finding.find_path(
            self.game_state.get_map_movement_context(),
            self.game_state.map.map_point,
            {target},
        )
        if move_path.end.cell_id != cell_id:
            raise ScriptError(f"cell {cell_id} is unreachable from the current position")
        return move_path

    def move_to_map(self, map_id: int) -> None:
        self.run_behavior(self.auto_trip_behavior, map_ids={map_id})

    def move_to_door(self, cell: object) -> None:
        if self.actions is None:
            raise ScriptError("the script engine is not attached, cannot use a door")
        self.actions.use_door(_as_int(cell, "door cell"))

    def gather_on_map(self, resource_item_ids: Iterable[int] | None = None) -> None:
        if self.actions is not None:
            self.actions.wait_for_map_data()
        target_ids = {int(item_id) for item_id in resource_item_ids} if resource_item_ids else None
        self.run_behavior(self.collect_behavior, target_resource_item_ids=target_ids)

    def fight_monsters(self, count: int, monster_ids: Iterable[int] | None = None) -> None:
        target_ids = {int(monster_id) for monster_id in monster_ids} if monster_ids else None
        self.run_behavior(
            self.attacker_behavior,
            count_fight_limit=int(count),
            monster_ids=target_ids,
        )

    def fight_set_allowed_spells(self, spell_ids: object = None) -> None:
        spell_list = cast(
            "list[object] | tuple[object, ...] | set[object] | frozenset[object] | None",
            spell_ids,
        )
        if spell_list is None:
            self.game_state.set_fight_allowed_spell_ids(None)
            self.logger.info("Lua fight spell whitelist: cleared (settings whitelist applies)")
            return
        if not isinstance(spell_list, (list, tuple, set, frozenset)):
            raise ScriptError(f"invalid spell list: {spell_list!r}")
        allowed = frozenset(_as_int(value, "spell id") for value in spell_list)
        self.game_state.set_fight_allowed_spell_ids(allowed or None)
        self.logger.info(
            "Lua fight spell whitelist: %s",
            ", ".join(str(spell_id) for spell_id in sorted(allowed))
            if allowed
            else "cleared (settings whitelist applies)",
        )

    def run_behavior(
        self,
        behavior: ScriptBehavior,
        *,
        rebuild: Callable[[], dict[str, Any]] | None = None,
        **run_kwargs: Any,
    ) -> dict[str, object]:
        outcome = run_behavior_blocking(
            behavior,
            parent=self.parent,
            cancel_event=self.cancel_event,
            defense_event=self.defense_event,
            rebuild=rebuild,
            **run_kwargs,
        )
        error_code = outcome.get("error")
        if error_code is not None:
            raise ScriptError(str(error_code))
        return outcome

    def map_current_map(self) -> str:
        return self.actions.map_coords() if self.actions is not None else self._coords()

    def _coords(self) -> str:
        pos = self.game_state.map.map_pos
        return f"{pos.posX},{pos.posY}"

    def map_x(self) -> int:
        return self.game_state.map.map_pos.posX

    def map_y(self) -> int:
        return self.game_state.map.map_pos.posY

    def map_current_area(self) -> str:
        sub_area = DataReader().sub_area_by_id[self.game_state.map.sub_area_id]
        area = DataReader().area_by_id[sub_area.areaId]
        return I18N().name_by_id.get(area.nameId, "")

    def map_current_sub_area(self) -> str:
        sub_area = DataReader().sub_area_by_id[self.game_state.map.sub_area_id]
        return I18N().name_by_id.get(sub_area.nameId, "")

    def map_zone_info(self) -> dict[str, object]:
        return {
            "mapId": self.game_state.map.map_id,
            "x": self.map_x(),
            "y": self.map_y(),
            "area": self.map_current_area(),
            "subArea": self.map_current_sub_area(),
        }

    def map_on_map(self, reference: object) -> bool:
        return self.game_state.map.map_id == self._resolve_map_ref(reference)

    def map_get_x(self, map_id: object) -> int:
        return self._map_info(_as_int(map_id, "map id")).posX

    def map_get_y(self, map_id: object) -> int:
        return self._map_info(_as_int(map_id, "map id")).posY

    def map_neighbour_id(self, direction: object) -> int:
        return 0

    def map_world_distance(self, pos_x: object, pos_y: object) -> int:
        target_map_id = get_map_id_by_coords(
            _as_int(pos_x, "x"), _as_int(pos_y, "y"), self.game_state.map.map_id
        )
        if target_map_id is None:
            return -1
        return self._world_distance_to(target_map_id)

    def map_world_distance_to_map(self, map_id: object) -> int:
        return self._world_distance_to(_as_int(map_id, "map id"))

    def _world_distance_to(self, target_map_id: int) -> int:
        target_info = DataReader().map_info_by_map_id.get(target_map_id)
        if target_info is None:
            return -1
        distance = get_dist_to_maps(self.game_state.map.map_pos, [target_info])
        if distance >= FAKE_INFINITY_VALUE:
            return -1
        return int(distance)

    def map_monster_groups(self) -> list[dict[str, object]]:
        groups: list[dict[str, object]] = []
        for actor_id, mp_group, monster_group in self.game_state.entity.get_monster_groups():
            groups.append(self._monster_group_info(actor_id, mp_group.cell_id, monster_group))
        return groups

    def _monster_group_info(
        self,
        actor_id: int,
        cell_id: int,
        monster_group: ActorPositionInformation.ActorInformation.RolePlayActor.MonsterGroupActor,
    ) -> dict[str, object]:
        creatures = [monster_group.identification.main_creature, *monster_group.identification.underlings]
        monsters: list[dict[str, object]] = [
            {"id": creature.gid, "name": self._monster_name(creature.gid), "level": creature.level}
            for creature in creatures
        ]
        return {
            "id": actor_id,
            "cellId": cell_id,
            "name": ", ".join(str(monster["name"]) for monster in monsters),
            "level": self.game_state.entity.get_level_monster_group(monster_group),
            "count": get_monster_group_size(monster_group),
            "monsters": monsters,
        }

    def map_players(self) -> list[dict[str, object]]:
        players: list[dict[str, object]] = []
        for actor in self.game_state.entity.actor_by_id.values():
            role_play = self._role_play_actor(actor)
            if role_play is None or not role_play.HasField("named_actor"):
                continue
            named = role_play.named_actor
            if not named.HasField("humanoid_information"):
                continue
            players.append(
                {
                    "id": actor.actor_id,
                    "cellId": actor.disposition.cell_id,
                    "name": named.name,
                    "level": self._player_level(named.humanoid_information),
                }
            )
        return players

    def map_npcs(self) -> list[dict[str, object]]:
        npcs: list[dict[str, object]] = []
        for actor in self.game_state.entity.actor_by_id.values():
            role_play = self._role_play_actor(actor)
            if role_play is None or not role_play.npc_actor.npc_id:
                continue
            template_id = role_play.npc_actor.npc_id
            npcs.append(
                {
                    "id": actor.actor_id,
                    "cellId": actor.disposition.cell_id,
                    "name": self._npc_name(template_id),
                    "templateId": template_id,
                }
            )
        return npcs

    def map_get_actors(self) -> list[dict[str, object]]:
        actors: list[dict[str, object]] = []
        for actor in self.game_state.entity.actor_by_id.values():
            info = self._actor_info(actor)
            if info is not None:
                actors.append(info)
        return actors

    def map_get_actor_by_id(self, actor_id: object) -> dict[str, object] | None:
        actor = self.game_state.entity.actor_by_id.get(_as_int(actor_id, "actor id"))
        return self._actor_info(actor) if actor is not None else None

    def _actor_info(self, actor: ActorPositionInformation) -> dict[str, object] | None:
        cell_id = actor.disposition.cell_id
        role_play = self._role_play_actor(actor)
        if role_play is not None and role_play.HasField("monster_group_actor"):
            group = self._monster_group_info(actor.actor_id, cell_id, role_play.monster_group_actor)
            return {"id": actor.actor_id, "cellId": cell_id, "name": group["name"], "type": "monster"}
        if role_play is not None and role_play.npc_actor.npc_id:
            return {
                "id": actor.actor_id,
                "cellId": cell_id,
                "name": self._npc_name(role_play.npc_actor.npc_id),
                "type": "npc",
            }
        if role_play is not None and role_play.HasField("named_actor"):
            named = role_play.named_actor
            if named.HasField("humanoid_information"):
                return {"id": actor.actor_id, "cellId": cell_id, "name": named.name, "type": "player"}
        return None

    def map_get_resources(self) -> list[dict[str, object]]:
        resources: list[dict[str, object]] = []
        for collectable in self.game_state.interactive.get_farmable_collectables(set()):
            resources.append(
                {
                    "cellId": collectable.mp.cell_id,
                    "gfxId": collectable.interactive_element.element_type_id,
                    "name": self._item_name(collectable.resource_item_id),
                }
            )
        return resources

    def map_interactive_objects(self) -> list[dict[str, object]]:
        objects: list[dict[str, object]] = []
        for stated_element, _ in self.game_state.interactive.stated_element_by_id.values():
            element = self.game_state.interactive.interactive_element_by_id.get(stated_element.element_id)
            objects.append(
                {
                    "cellId": stated_element.cell_id,
                    "gfxId": element.element_type_id if element is not None else 0,
                }
            )
        return objects

    def map_is_walkable(self, cell_id: object) -> bool:
        cells = self._cells_by_number()
        cell = cells.get(_as_int(cell_id, "cell id"))
        return bool(cell.movDuringRP) if cell is not None else False

    def map_get_walkable_cells(self) -> list[int]:
        return [cell.cellNumber for cell in self._map_data().cellsData if cell.movDuringRP]

    def _cells_by_number(self) -> dict[int, Any]:
        return {cell.cellNumber: cell for cell in self._map_data().cellsData}

    def _map_data(self) -> Any:
        return MapReader().map_by_id(self.game_state.map.map_id).mapData

    def map_has_changed(self) -> bool:
        current_map_id = self.game_state.map.map_id
        if self._last_seen_map_id is None:
            self._last_seen_map_id = current_map_id
            return False
        changed = current_map_id != self._last_seen_map_id
        self._last_seen_map_id = current_map_id
        return changed

    def map_gather(self) -> bool:
        if self.actions is not None:
            self.actions.wait_for_map_data()
        self.run_behavior(self.collect_behavior)
        return True

    def map_gather_resource(self, cell_id: object) -> bool:
        if self.actions is not None:
            self.actions.wait_for_map_data()
        self.run_behavior(
            self.collect_behavior,
            specific_cell_ids={_as_int(cell_id, "cell id")},
        )
        return True

    def map_fight(self) -> bool:
        self.run_behavior(self.attacker_behavior, count_fight_limit=1)
        return True

    def map_fight_group(self, group_actor_id: object) -> bool:
        outcome = self.run_behavior(
            self.attacker_behavior,
            count_fight_limit=1,
            only_group_actor_id=_as_int(group_actor_id, "group actor id"),
        )
        return int(str(outcome.get("count_fighted_on_map", 0))) > 0

    def map_is_in_party(self) -> bool:
        return self.game_state.party.is_in_party

    def map_party_count(self) -> int:
        return len(self.game_state.party.members_by_id)

    def map_party_leader_id(self) -> int:
        return self.game_state.party.leader_id

    def map_is_party_leader(self) -> bool:
        return self.game_state.party.is_leader(self.game_state.player.character_id)

    def map_party_members(self) -> list[dict[str, object]]:
        return [
            {"id": member.player_id, "name": member.name, "level": member.level or 0}
            for member in self.game_state.party.members_by_id.values()
        ]

    def map_pending_group_invite(self) -> dict[str, object] | None:
        pending = self.game_state.party.pending_invite
        if pending is None:
            return None
        return {"id": pending.party_id, "name": pending.from_player_name}

    def current_login(self) -> str:
        return self.game_state.player.login

    def current_name(self) -> str:
        return self.game_state.player.character_name

    def _party_member_bots(self) -> list[Bot]:
        bots = controller_hub.all_bots()
        bots_by_character_id = {
            bot.game_state.player.character_id: bot for bot in bots
        }
        return [
            bots_by_character_id[member.player_id]
            for member in self.game_state.party.members_by_id.values()
            if member.player_id in bots_by_character_id
        ]

    def _party_member_logins(self, *, exclude_leader: bool) -> list[str]:
        self_login = self.current_login()
        leader_id = self.game_state.party.leader_id
        logins: list[str] = []
        for bot in self._party_member_bots():
            login = bot.account.apikey.login
            if login == self_login:
                continue
            if exclude_leader and bot.game_state.player.character_id == leader_id:
                continue
            logins.append(login)
        return logins

    def _command_from_self(self, command: object) -> BotCommand:
        return BotCommand(
            sender_login=self.current_login(),
            sender_name=self.current_name(),
            text=str(command),
        )

    def group_is_in_group(self) -> bool:
        return self.game_state.party.is_in_party

    def group_is_leader(self) -> bool:
        return self.game_state.party.is_leader(self.game_state.player.character_id)

    def group_is_mule(self) -> bool:
        party = self.game_state.party
        return party.is_in_party and not party.is_leader(self.game_state.player.character_id)

    def group_id(self) -> int:
        return self.game_state.party.party_id

    def group_name(self) -> str:
        return self.game_state.party.name

    def group_leader_id(self) -> int:
        return self.game_state.party.leader_id

    def group_member_ids(self) -> list[int]:
        return list(self.game_state.party.members_by_id.keys())

    def group_all_on_same_map(self) -> bool:
        party = self.game_state.party
        if not party.is_in_party:
            return False
        member_bots = self._party_member_bots()
        if len(member_bots) != len(party.members_by_id):
            return False
        return len({bot.game_state.map.map_id for bot in member_bots}) == 1

    def group_get_command(self) -> dict[str, object] | None:
        command = controller_hub.poll(self.current_login())
        return command_to_payload(command) if command is not None else None

    def group_send_command_to_mules(self, command: object) -> int:
        logins = self._party_member_logins(exclude_leader=True)
        return controller_hub.broadcast(logins, self._command_from_self(command))

    def group_send_command_to_all(self, command: object) -> int:
        logins = self._party_member_logins(exclude_leader=False)
        return controller_hub.broadcast(logins, self._command_from_self(command))

    def group_send_command_to_leader(self, command: object) -> bool:
        party = self.game_state.party
        if not party.is_in_party or party.leader_id == self.game_state.player.character_id:
            return False
        leader = party.members_by_id.get(party.leader_id)
        if leader is None:
            return False
        return controller_hub.send(leader.name, self._command_from_self(command))

    def team_is_in_team(self) -> bool:
        return team_controller.is_member(self.current_login())

    def team_is_leader(self) -> bool:
        return team_controller.is_leader(self.current_login())

    def team_is_member(self) -> bool:
        return team_controller.is_member(self.current_login()) and not team_controller.is_leader(
            self.current_login()
        )

    def team_get_leader(self) -> str:
        return team_controller.leader or ""

    def team_get_member_count(self) -> int:
        return team_controller.size()

    def team_get_members(self) -> list[dict[str, object]]:
        records: list[dict[str, object]] = []
        for login in team_controller.members:
            bot = controller_hub.get_bot(login)
            if bot is None:
                records.append({"login": login, "name": "", "id": 0, "leader": team_controller.is_leader(login)})
                continue
            player = bot.game_state.player
            records.append(
                {
                    "login": login,
                    "name": player.character_name,
                    "id": player.character_id,
                    "leader": team_controller.is_leader(login),
                }
            )
        return records

    def _team_member_logins(self, *, exclude_self: bool, only_leader: bool) -> list[str]:
        self_login = self.current_login()
        logins: list[str] = []
        for login in team_controller.members:
            if exclude_self and login == self_login:
                continue
            if only_leader and not team_controller.is_leader(login):
                continue
            logins.append(login)
        return logins

    def team_send_command_to_members(self, command: object) -> int:
        return controller_hub.broadcast(
            self._team_member_logins(exclude_self=True, only_leader=False),
            self._command_from_self(command),
        )

    def team_send_command_to_leader(self, command: object) -> bool:
        leader = team_controller.leader
        if leader is None or leader == self.current_login():
            return False
        return controller_hub.send(leader, self._command_from_self(command))

    def _controller_record(self, bot: Bot) -> dict[str, object]:
        player = bot.game_state.player
        return {
            "id": bot.account.apikey.login,
            "accountId": bot.account.apikey.accountId,
            "alias": player.character_name,
            "username": player.character_name,
            "level": player.level,
            "connected": bot.is_connected_event.is_set(),
            "scriptPlaying": bot.is_playing_event.is_set(),
            "serverId": player.server_id,
        }

    def controller_all(self) -> list[dict[str, object]]:
        return [self._controller_record(bot) for bot in controller_hub.all_bots()]

    def controller_connected(self) -> list[dict[str, object]]:
        return [
            self._controller_record(bot)
            for bot in controller_hub.all_bots()
            if bot.is_connected_event.is_set()
        ]

    def controller_by_username(self, username: object) -> dict[str, object] | None:
        bot = controller_hub.resolve(username)
        return self._controller_record(bot) if bot is not None else None

    def controller_account_count(self) -> int:
        return len(controller_hub.all_bots())

    def controller_connected_count(self) -> int:
        return sum(1 for bot in controller_hub.all_bots() if bot.is_connected_event.is_set())

    def controller_send_command(self, target: object, command: object) -> bool:
        return controller_hub.send(target, self._command_from_self(command))

    def controller_character_state(self, target: object) -> dict[str, object] | None:
        bot = controller_hub.resolve(target)
        if bot is None:
            return None
        player = bot.game_state.player
        return {
            "id": bot.account.apikey.login,
            "characterId": player.character_id,
            "name": player.character_name,
            "level": player.level,
            "mapId": bot.game_state.map.map_id,
            "cellId": bot.game_state.map.map_point.cell_id,
            "inFight": bot.game_state.fight.in_fight,
            "connected": bot.is_connected_event.is_set(),
        }

    def controller_inventory(self, target: object) -> dict[int, int]:
        bot = controller_hub.resolve(target)
        if bot is None:
            return {}
        inventory: dict[int, int] = {}
        for object_item in bot.game_state.inventory.objects_by_uid.values():
            gid = object_item.item.gid
            inventory[gid] = inventory.get(gid, 0) + object_item.item.quantity
        return inventory

    def npc_get_question_id(self) -> int:
        question = self.game_state.dialog.npc_question
        return question.message_id if question is not None else 0

    def npc_get_responses(self) -> list[int]:
        question = self.game_state.dialog.npc_question
        if question is None:
            return []
        return [reply.reply_id for reply in question.visible_replies]

    def npc_get_response_texts(self) -> list[list[object]]:
        question = self.game_state.dialog.npc_question
        if question is None:
            return []
        return [[reply.reply_id, get_reply_text(reply.reply_id) or ""] for reply in question.visible_replies]

    def npc_get_question_text(self) -> str:
        question = self.game_state.dialog.npc_question
        if question is None:
            return ""
        return get_question_text(question.message_id) or ""

    def npc_is_in_dialog(self) -> bool:
        return self.game_state.dialog.is_any_open

    def npc_get_npc_id(self) -> int:
        if self.game_state.dialog.is_open(OpenDialogKind.NPC_DIALOG):
            return self.game_state.dialog.npc_id or 0
        return 0

    def npc_get_npc_id_on_cell(self, cell_id: object) -> int | None:
        target_cell_id = _as_int(cell_id, "cell id")
        for actor in self.game_state.entity.actor_by_id.values():
            role_play = self._role_play_actor(actor)
            if (
                role_play is not None
                and role_play.npc_actor.npc_id
                and actor.disposition.cell_id == target_cell_id
            ):
                return actor.actor_id
        return None

    def _resolve_map_ref(self, reference: object) -> int:
        if self.actions is not None:
            return self.actions.resolve_map(reference)
        if isinstance(reference, str) and "," in reference:
            pos_x, pos_y = (int(part) for part in reference.split(",", 1))
            resolved = get_map_id_by_coords(pos_x, pos_y, self.game_state.map.map_id)
            if resolved is None:
                raise ScriptError(f"unknown map coordinates: {reference!r}")
            return resolved
        return _as_int(reference, "map reference")

    def _map_info(self, map_id: int) -> Any:
        info = DataReader().map_info_by_map_id.get(map_id)
        if info is None:
            raise ScriptError(f"unknown map id: {map_id}")
        return info

    def _role_play_actor(
        self, actor: ActorPositionInformation
    ) -> ActorPositionInformation.ActorInformation.RolePlayActor | None:
        if actor.actor_information.HasField("role_play_actor"):
            return actor.actor_information.role_play_actor
        return None

    def _player_level(self, humanoid: Any) -> int:
        for option in humanoid.options:
            if option.HasField("ornament_option"):
                return option.ornament_option.player_level
        return humanoid.alignment_information.character_level

    def _monster_name(self, monster_id: int) -> str:
        monster = DataReader().monsters_by_id.get(monster_id)
        if monster is None:
            return f"Monster {monster_id}"
        return I18N().name_by_id.get(monster.nameId, f"Monster {monster_id}")

    def _npc_name(self, template_id: int) -> str:
        npc = DataReader().npc_by_id.get(template_id)
        if npc is None:
            return f"NPC {template_id}"
        return I18N().name_by_id.get(npc.nameId, f"NPC {template_id}")

    def _item_name(self, item_id: int) -> str:
        item = DataReader().item_by_id.get(item_id)
        if item is None:
            return f"Item {item_id}"
        return I18N().name_by_id.get(item.nameId, f"Item {item_id}")
