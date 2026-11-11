from collections.abc import Iterable
from dataclasses import dataclass, field

from DBDofusUnity.datas.protos.non_obf.game.party_pb2 import PartyInvitationEvent

from src.core.states.state import State


@dataclass
class PartyMember:
    player_id: int
    name: str
    level: int | None = None


@dataclass
class PartyState(State):
    party_id: int = field(init=False, default=0)
    name: str = field(init=False, default="")
    leader_id: int = field(init=False, default=0)
    members_by_id: dict[int, PartyMember] = field(init=False, default_factory=dict[int, PartyMember])
    pending_invite: PartyInvitationEvent | None = field(init=False, default=None)

    @property
    def is_in_party(self) -> bool:
        return self.party_id != 0

    def is_leader(self, player_id: int) -> bool:
        return self.is_in_party and self.leader_id == player_id

    def member_ids(self) -> set[int]:
        return set(self.members_by_id)

    def has_member(self, player_id: int) -> bool:
        return player_id in self.members_by_id

    def set_members(self, members: Iterable[PartyMember]) -> None:
        self.members_by_id = {member.player_id: member for member in members}

    def add_member(self, member: PartyMember) -> None:
        self.members_by_id[member.player_id] = member

    def remove_member(self, player_id: int) -> None:
        self.members_by_id.pop(player_id, None)

    def clear_state(self) -> None:
        self.party_id = 0
        self.name = ""
        self.leader_id = 0
        self.members_by_id.clear()
        self.pending_invite = None
