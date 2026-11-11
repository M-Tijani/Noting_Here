from dataclasses import dataclass

from DBDofusUnity.datas.protos.non_obf.game.common_pb2 import Character
from DBDofusUnity.datas.protos.non_obf.game.party_pb2 import (
    FightAutoJoinActivationRequest,
    FightAutoReadyActivationRequest,
    PartyDeletedEvent,
    PartyInvitationCancelledForGuestEvent,
    PartyInvitationEvent,
    PartyJoinErrorEvent,
    PartyJoinEvent,
    PartyLeaderUpdateEvent,
    PartyLeaveEvent,
    PartyMemberRemoveEvent,
    PartyMemberUpdateEvent,
    PartyNameUpdateEvent,
    PartyNewMemberEvent,
)

from src.core.events_manager.event_manager import EventManager
from src.core.frames.frame import Frame
from src.core.states.party_state import PartyMember


def send_fight_auto_activation(event_manager: EventManager) -> None:
    event_manager.send(FightAutoJoinActivationRequest())
    event_manager.send(FightAutoReadyActivationRequest())


def member_from_character(character: Character) -> PartyMember:
    name = ""
    level: int | None = None
    if character.HasField("character_basic_information"):
        basic_information = character.character_basic_information
        name = basic_information.name
        if basic_information.HasField("level"):
            level = basic_information.level
    return PartyMember(player_id=character.id, name=name, level=level)


@dataclass
class PartyFrame(Frame):
    def __post_init__(self) -> None:
        super().__post_init__()
        self.event_manager.on(
            PartyInvitationEvent, self.on_party_invitation, originator=self, priority=self.priority
        )
        self.event_manager.before(PartyInvitationEvent, self.before_party_invitation, originator=self)
        self.event_manager.before(PartyJoinErrorEvent, self.before_party_join_error, originator=self)
        self.event_manager.on(
            PartyInvitationCancelledForGuestEvent,
            self.on_party_invitation_cancelled,
            originator=self,
            priority=self.priority,
        )
        self.event_manager.on(PartyJoinEvent, self.on_party_join, originator=self, priority=self.priority)
        self.event_manager.on(
            PartyNewMemberEvent, self.on_party_member, originator=self, priority=self.priority
        )
        self.event_manager.on(
            PartyMemberUpdateEvent, self.on_party_member, originator=self, priority=self.priority
        )
        self.event_manager.on(
            PartyMemberRemoveEvent, self.on_party_member_remove, originator=self, priority=self.priority
        )
        self.event_manager.on(
            PartyLeaderUpdateEvent, self.on_party_leader_update, originator=self, priority=self.priority
        )
        self.event_manager.on(
            PartyNameUpdateEvent, self.on_party_name_update, originator=self, priority=self.priority
        )
        self.event_manager.on(PartyLeaveEvent, self.on_party_left, originator=self, priority=self.priority)
        self.event_manager.on(
            PartyDeletedEvent, self.on_party_left, originator=self, priority=self.priority
        )

    def on_party_invitation(self, msg: PartyInvitationEvent) -> None:
        self.logger.info(f"Party invitation received from {msg.from_player_name} (party {msg.party_id})")
        self.game_state.party.pending_invite = msg

    def before_party_invitation(self, msg: PartyInvitationEvent) -> PartyInvitationEvent | None:
        # The Dofus client pops a modal that only closes on its own accept/refuse,
        # so an accept injected by the bot leaves it open. While automating, hide the
        # invitation from the client; the framework still receives it and can accept.
        if self.is_playing_event.is_set():
            self.logger.info("Hiding party invitation popup during automation")
            return None
        return msg

    def before_party_join_error(self, msg: PartyJoinErrorEvent) -> PartyJoinErrorEvent | None:
        if (
            self.is_playing_event.is_set()
            and msg.reason == PartyJoinErrorEvent.PLAYER_ALREADY_INVITED
        ):
            self.logger.info("Hiding duplicate party invitation error popup during automation")
            return None
        return msg

    def on_party_invitation_cancelled(self, msg: PartyInvitationCancelledForGuestEvent) -> None:
        pending = self.game_state.party.pending_invite
        if pending is not None and pending.party_id == msg.party_id:
            self.game_state.party.pending_invite = None

    def on_party_join(self, msg: PartyJoinEvent) -> None:
        party = self.game_state.party
        party.party_id = msg.party_id
        party.leader_id = msg.leader_id
        party.name = msg.party_name
        party.pending_invite = None
        party.set_members(member_from_character(member) for member in msg.members)
        self.logger.info(f"Joined party {msg.party_id} ({len(party.members_by_id)} members)")
        # The game client only enables these party options in its own UI callback and can
        # miss it right after login (mules then never join the leader's fights).
        if self.is_playing_event.is_set():
            send_fight_auto_activation(self.event_manager)

    def on_party_member(self, msg: PartyNewMemberEvent | PartyMemberUpdateEvent) -> None:
        self.game_state.party.add_member(member_from_character(msg.member))

    def on_party_member_remove(self, msg: PartyMemberRemoveEvent) -> None:
        self.game_state.party.remove_member(msg.leaving_player_id)

    def on_party_leader_update(self, msg: PartyLeaderUpdateEvent) -> None:
        self.game_state.party.leader_id = msg.party_leader_id

    def on_party_name_update(self, msg: PartyNameUpdateEvent) -> None:
        self.game_state.party.name = msg.name

    def on_party_left(self, _: PartyLeaveEvent | PartyDeletedEvent) -> None:
        self.game_state.party.clear_state()
