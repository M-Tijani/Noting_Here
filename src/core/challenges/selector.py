from __future__ import annotations

from collections.abc import Iterable

from src.core.challenges.models import ChallengeDefinition, ChallengeRule, rules_conflict
from src.core.challenges.repository import ChallengeRepository


def select_challenge(
    proposal_ids: Iterable[int],
    repository: ChallengeRepository,
    preferred_ids: Iterable[int],
    active_ids: Iterable[int] = (),
) -> ChallengeDefinition | None:
    proposed = set(proposal_ids)
    active_id_set = set(active_ids)
    active_rules: set[ChallengeRule] = set()
    active_definitions: list[ChallengeDefinition] = []
    for challenge_id in active_id_set:
        definition = repository.get(challenge_id)
        if definition is None:
            continue
        active_definitions.append(definition)
        active_rules.update(definition.rules)

    for challenge_id in preferred_ids:
        definition = repository.get(challenge_id)
        if (
            definition is None
            or not definition.enabled
            or definition.id not in proposed
            or definition.id in active_id_set
        ):
            continue
        if any(
            definition.id in active.incompatible_with
            or active.id in definition.incompatible_with
            for active in active_definitions
        ):
            continue
        if rules_conflict(definition.rules, active_rules):
            continue
        return definition
    return None
