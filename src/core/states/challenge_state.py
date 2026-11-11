from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from threading import RLock

from DBDofusUnity.dofus_unity_reader.game_constants.characteristic import EffectElement
from src.core.challenges.models import ChallengeDefinition, ChallengeRule, FightConstraints
from src.core.challenges.repository import ChallengeRepository
from src.core.challenges.selector import select_challenge


class ChallengeSelectionError(ValueError):
    pass


def _resolve_active_rules(
    definitions: Iterable[ChallengeDefinition],
) -> frozenset[ChallengeRule]:
    rules = {rule for definition in definitions for rule in definition.rules}
    # Duplicate names re-issued under new ids can inherit a rule that the server
    # activates alongside its explicit opposite. Prefer the avoid rule so the
    # bot does not spend every turn oscillating between impossible positions.
    if {ChallengeRule.END_NEXT_TO_ALLY, ChallengeRule.AVOID_ALLY_ADJACENT} <= rules:
        rules.discard(ChallengeRule.END_NEXT_TO_ALLY)
    if {ChallengeRule.END_IN_LINE_WITH_ALLY, ChallengeRule.AVOID_ALLY_LINE} <= rules:
        rules.discard(ChallengeRule.END_IN_LINE_WITH_ALLY)
    return frozenset(rules)


@dataclass
class ChallengeState:
    repository: ChallengeRepository | None = None
    _preferred_ids: list[int] = field(init=False, default_factory=list[int])
    _selected: ChallengeDefinition | None = field(init=False, default=None)
    _proposal_ids: tuple[int, ...] = field(init=False, default=())
    _known_ids: frozenset[int] = field(init=False, default=frozenset[int]())
    _active_by_id: dict[int, ChallengeDefinition] = field(init=False, default_factory=dict[int, ChallengeDefinition])
    _target_ids_by_challenge_id: dict[int, frozenset[int]] = field(
        init=False, default_factory=dict[int, frozenset[int]]
    )
    _results_by_challenge_id: dict[int, bool] = field(init=False, default_factory=dict[int, bool])
    _damage_element: EffectElement | None = field(init=False, default=None)
    _attacked_enemy_ids: set[int] = field(init=False, default_factory=set[int])
    _first_attacked_enemy_id: int | None = field(init=False, default=None)
    _player_attacked_enemy_ids: set[int] = field(init=False, default_factory=set[int])
    _ally_attacked_enemy_ids: set[int] = field(init=False, default_factory=set[int])
    _lock: RLock = field(init=False, default_factory=RLock)

    def __post_init__(self) -> None:
        if self.repository is None:
            self.repository = ChallengeRepository(())
        self._preferred_ids = list(self.repository.default_preferred_ids())

    def list_definitions(self, *, easy_only: bool = False) -> tuple[ChallengeDefinition, ...]:
        assert self.repository is not None
        return self.repository.list_definitions(easy_only=easy_only)

    def get_definition(self, challenge_id: int) -> ChallengeDefinition | None:
        assert self.repository is not None
        return self.repository.get(challenge_id)

    def default_preferred_ids(self) -> tuple[int, ...]:
        assert self.repository is not None
        return self.repository.default_preferred_ids()

    def preferred_ids(self) -> tuple[int, ...]:
        with self._lock:
            return tuple(self._preferred_ids)

    def set_preferred_ids(self, challenge_ids: Iterable[int]) -> tuple[int, ...]:
        values = list(challenge_ids)
        seen: set[int] = set()
        for challenge_id in values:
            if challenge_id in seen:
                raise ChallengeSelectionError(f"duplicate challenge id in selection: {challenge_id}")
            definition = self.get_definition(challenge_id)
            if definition is None:
                raise ChallengeSelectionError(f"unknown challenge id: {challenge_id}")
            if not definition.enabled:
                raise ChallengeSelectionError(f"challenge {challenge_id} is disabled")
            seen.add(challenge_id)
        with self._lock:
            self._preferred_ids = values
        return tuple(values)

    def set_preferred_names(self, names: Iterable[str]) -> tuple[int, ...]:
        definitions = self.list_definitions()
        by_name = {definition.name.casefold(): definition for definition in definitions}
        challenge_ids: list[int] = []
        for name in names:
            definition = by_name.get(name.casefold())
            if definition is None:
                raise ChallengeSelectionError(f"unknown challenge name: {name}")
            challenge_ids.append(definition.id)
        return self.set_preferred_ids(challenge_ids)

    def selected(self) -> ChallengeDefinition | None:
        with self._lock:
            return self._selected

    def proposal_ids(self) -> tuple[int, ...]:
        with self._lock:
            return self._proposal_ids

    def observe_challenge_list(self, challenge_ids: Iterable[int]) -> None:
        with self._lock:
            self._known_ids = frozenset(challenge_ids)

    def known_ids(self) -> frozenset[int]:
        with self._lock:
            return self._known_ids

    def select(
        self, proposal_ids: Iterable[int], *, allow_fallback: bool = False
    ) -> ChallengeDefinition | None:
        values = tuple(proposal_ids)
        assert self.repository is not None
        active_ids = self.active_ids()
        selected = select_challenge(values, self.repository, self.preferred_ids(), active_ids)
        if selected is None and allow_fallback:
            selected = select_challenge(values, self.repository, values, active_ids)
        with self._lock:
            self._proposal_ids = values
            self._selected = selected
        return selected

    def active_ids(self) -> tuple[int, ...]:
        with self._lock:
            return tuple(self._active_by_id)

    def active_definitions(self) -> tuple[ChallengeDefinition, ...]:
        with self._lock:
            return tuple(self._active_by_id.values())

    def active_rules(self) -> frozenset[ChallengeRule]:
        with self._lock:
            return _resolve_active_rules(self._active_by_id.values())

    def target_ids(self) -> frozenset[int]:
        with self._lock:
            return frozenset(target_id for ids in self._target_ids_by_challenge_id.values() for target_id in ids)

    def results(self) -> dict[int, bool]:
        with self._lock:
            return dict(self._results_by_challenge_id)

    def damage_element(self) -> EffectElement | None:
        with self._lock:
            return self._damage_element

    def attacked_enemy_ids(self) -> frozenset[int]:
        with self._lock:
            return frozenset(self._attacked_enemy_ids)

    def constraints(self) -> FightConstraints:
        with self._lock:
            return FightConstraints(
                rules=_resolve_active_rules(self._active_by_id.values()),
                target_ids=frozenset(
                    target_id for ids in self._target_ids_by_challenge_id.values() for target_id in ids
                ),
                damage_element=self._damage_element,
                attacked_enemy_ids=frozenset(self._attacked_enemy_ids),
                first_attacked_enemy_id=self._first_attacked_enemy_id,
                player_attacked_enemy_ids=frozenset(self._player_attacked_enemy_ids),
                ally_attacked_enemy_ids=frozenset(self._ally_attacked_enemy_ids),
            )

    def activate(self, challenge_id: int, target_ids: Iterable[int] = ()) -> ChallengeDefinition:
        definition = self.get_definition(challenge_id)
        if definition is None:
            definition = ChallengeDefinition(id=challenge_id, name=f"Unknown challenge {challenge_id}")
        with self._lock:
            self._active_by_id[challenge_id] = definition
            self._target_ids_by_challenge_id[challenge_id] = frozenset(target_ids)
        return definition

    def update_targets(self, challenge_id: int, target_ids: Iterable[int]) -> None:
        with self._lock:
            if challenge_id not in self._active_by_id:
                return
            self._target_ids_by_challenge_id[challenge_id] = frozenset(target_ids)

    def record_damage(
        self,
        element_id: int,
        target_id: int,
        *,
        from_player: bool = False,
        from_ally: bool = False,
    ) -> None:
        with self._lock:
            if from_player and self._damage_element is None:
                try:
                    self._damage_element = EffectElement(element_id)
                except ValueError:
                    pass
            if from_player or from_ally:
                self._attacked_enemy_ids.add(target_id)
                if self._first_attacked_enemy_id is None:
                    self._first_attacked_enemy_id = target_id
                if from_player:
                    self._player_attacked_enemy_ids.add(target_id)
                if from_ally:
                    self._ally_attacked_enemy_ids.add(target_id)

    def mark_result(self, challenge_id: int, *, success: bool) -> None:
        with self._lock:
            self._results_by_challenge_id[challenge_id] = success
            self._active_by_id.pop(challenge_id, None)
            self._target_ids_by_challenge_id.pop(challenge_id, None)

    def clear_selection(self) -> None:
        with self._lock:
            self._proposal_ids = ()
            self._selected = None

    def clear_fight(self) -> None:
        with self._lock:
            self._proposal_ids = ()
            self._selected = None
            self._known_ids = frozenset()
            self._active_by_id.clear()
            self._target_ids_by_challenge_id.clear()
            self._results_by_challenge_id.clear()
            self._damage_element = None
            self._attacked_enemy_ids.clear()
            self._first_attacked_enemy_id = None
            self._player_attacked_enemy_ids.clear()
            self._ally_attacked_enemy_ids.clear()
