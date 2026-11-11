from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import StrEnum

from DBDofusUnity.dofus_unity_reader.game_constants.characteristic import EffectElement


class ChallengeRule(StrEnum):
    NO_ALLY_DEATH = "no_ally_death"
    NO_HEAL = "no_heal"
    NO_PUSH = "no_push"
    NO_AP_REMOVAL = "no_ap_removal"
    NO_MP_REMOVAL = "no_mp_removal"
    NO_AOE = "no_aoe"
    SINGLE_TARGET = "single_target"
    MELEE_ONLY = "melee_only"
    RANGED_ONLY = "ranged_only"
    STAY_STILL = "stay_still"
    TARGET_ONLY = "target_only"
    KILL_TARGET_FIRST = "kill_target_first"
    KILL_TARGET_LAST = "kill_target_last"
    NO_SUMMON = "no_summon"
    ONE_KILL_PER_TURN = "one_kill_per_turn"
    SINGLE_ELEMENT = "single_element"
    FOCUS_FIRE = "focus_fire"
    KILL_LOW_TO_HIGH = "kill_low_to_high"
    KILL_HIGH_TO_LOW = "kill_high_to_low"
    KILL_BEFORE_TURN = "kill_before_turn"
    DUEL = "duel"
    END_NEXT_TO_ALLY = "end_next_to_ally"
    END_IN_LINE_WITH_ALLY = "end_in_line_with_ally"
    AVOID_ALLY_ADJACENT = "avoid_ally_adjacent"
    AVOID_ENEMY_ADJACENT = "avoid_enemy_adjacent"
    AVOID_ALLY_LINE = "avoid_ally_line"
    AVOID_ENEMY_LINE = "avoid_enemy_line"


@dataclass(frozen=True)
class ChallengeDefinition:
    id: int
    name: str
    description: str = ""
    rules: tuple[ChallengeRule, ...] = ()
    priority: int = 0
    enabled: bool = True
    incompatible_with: tuple[int, ...] = ()
    easy: bool = False

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "rules": [rule.value for rule in self.rules],
            "priority": self.priority,
            "enabled": self.enabled,
            "incompatible_with": list(self.incompatible_with),
            "easy": self.easy,
        }


CONFLICTING_RULES: tuple[frozenset[ChallengeRule], ...] = (
    frozenset({ChallengeRule.MELEE_ONLY, ChallengeRule.RANGED_ONLY}),
    frozenset({ChallengeRule.KILL_TARGET_FIRST, ChallengeRule.KILL_TARGET_LAST}),
    frozenset({ChallengeRule.KILL_LOW_TO_HIGH, ChallengeRule.KILL_HIGH_TO_LOW}),
    frozenset({ChallengeRule.END_NEXT_TO_ALLY, ChallengeRule.AVOID_ALLY_ADJACENT}),
    frozenset({ChallengeRule.END_IN_LINE_WITH_ALLY, ChallengeRule.AVOID_ALLY_LINE}),
    frozenset({ChallengeRule.MELEE_ONLY, ChallengeRule.AVOID_ENEMY_ADJACENT}),
)


def rules_conflict(left: Iterable[ChallengeRule], right: Iterable[ChallengeRule]) -> bool:
    left_rules = set(left)
    right_rules = set(right)
    return any(
        conflict <= left_rules and conflict <= right_rules for conflict in CONFLICTING_RULES
    )


@dataclass(frozen=True)
class FightConstraints:
    rules: frozenset[ChallengeRule] = field(default_factory=frozenset[ChallengeRule])
    target_ids: frozenset[int] = field(default_factory=frozenset[int])
    damage_element: EffectElement | None = None
    attacked_enemy_ids: frozenset[int] = field(default_factory=frozenset[int])
    first_attacked_enemy_id: int | None = None
    player_attacked_enemy_ids: frozenset[int] = field(default_factory=frozenset[int])
    ally_attacked_enemy_ids: frozenset[int] = field(default_factory=frozenset[int])

    def has(self, rule: ChallengeRule) -> bool:
        return rule in self.rules

    @property
    def has_targets(self) -> bool:
        return bool(self.target_ids)

    @property
    def restricts_targets(self) -> bool:
        return self.has(ChallengeRule.TARGET_ONLY) or self.has(ChallengeRule.KILL_TARGET_FIRST)

    @property
    def restricts_target_order(self) -> bool:
        return (
            self.has(ChallengeRule.KILL_TARGET_LAST)
            or self.has(ChallengeRule.KILL_LOW_TO_HIGH)
            or self.has(ChallengeRule.KILL_HIGH_TO_LOW)
            or self.has(ChallengeRule.FOCUS_FIRE)
            or self.has(ChallengeRule.KILL_BEFORE_TURN)
        )
