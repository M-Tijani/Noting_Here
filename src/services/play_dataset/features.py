"""Frozen feature contract shared by dataset building, training and inference.

Phase 5 implements the extractor that produces these features. Changing an entry is
a dataset-breaking change: bump :data:`FEATURE_SPEC_VERSION` and rebuild the dataset
so the manifest hash changes too.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Any, Literal

FEATURE_SPEC_VERSION = 1

Source = Literal["observation", "candidate"]
DType = Literal["int", "float", "bool", "categorical"]


@dataclass(frozen=True, slots=True)
class Feature:
    name: str
    source: Source
    dtype: DType
    description: str


FEATURES: tuple[Feature, ...] = (
    Feature("obs.fight_turn", "observation", "int", "Turn index within the fight."),
    Feature("obs.map_id", "observation", "int", "Fight map id."),
    Feature("obs.player.level", "observation", "int", "Player level."),
    Feature("obs.player.breed_id", "observation", "categorical", "Player breed."),
    Feature("obs.player.action_points", "observation", "int", "Current action points."),
    Feature("obs.player.movement_points", "observation", "int", "Current movement points."),
    Feature("obs.player.range", "observation", "int", "Current spell range bonus."),
    Feature("obs.player.life_point", "observation", "int", "Current life points."),
    Feature("obs.player.max_life_point", "observation", "int", "Maximum life points."),
    Feature("obs.player.life_fraction", "observation", "float", "Current life divided by maximum."),
    Feature("obs.player.primary_elem", "observation", "categorical", "Primary damage element."),
    Feature("obs.player.cell_id", "observation", "int", "Player cell on the fight map."),
    Feature("obs.own.state_count", "observation", "int", "Number of active states on the player."),
    Feature("obs.own.summon_count", "observation", "int", "Number of active summons."),
    Feature("obs.own.max_summon_count", "observation", "int", "Maximum allowed summons."),
    Feature("obs.spells.count", "observation", "int", "Number of spells available to the player."),
    Feature("obs.enemies.count", "observation", "int", "Number of enemy actors."),
    Feature("obs.enemies.alive_count", "observation", "int", "Number of alive enemy actors."),
    Feature("obs.enemies.total_life", "observation", "int", "Sum of enemy current life."),
    Feature("obs.enemies.min_life_fraction", "observation", "float", "Lowest enemy life fraction."),
    Feature("obs.enemies.max_spell_range", "observation", "int", "Maximum enemy spell range."),
    Feature("obs.enemies.invisible_count", "observation", "int", "Number of invisible enemy cells."),
    Feature("cand.action_type", "candidate", "categorical", "cast, cast_on_target, move, pass_turn, ready."),
    Feature("cand.spell_id", "candidate", "categorical", "Spell id for cast candidates."),
    Feature("cand.cell_id", "candidate", "int", "Target or destination cell."),
    Feature("cand.target_actor_id", "candidate", "categorical", "Target actor id when applicable."),
    Feature("cand.element", "candidate", "categorical", "Spell damage element."),
    Feature("cand.ap_cost", "candidate", "int", "Action points required by the candidate."),
    Feature("cand.mp_cost", "candidate", "int", "Movement points required by the candidate."),
    Feature("cand.range", "candidate", "int", "Candidate spell or movement range."),
    Feature("cand.target_distance", "candidate", "int", "Distance from the player to the target."),
    Feature("cand.damage_raw", "candidate", "float", "Unadjusted expected damage."),
    Feature("cand.damage_resist_adjusted", "candidate", "float", "Damage after enemy resistances."),
    Feature("cand.kill_probability", "candidate", "float", "Probability the candidate kills its target."),
    Feature("cand.is_kill", "candidate", "bool", "Whether the candidate is expected to kill."),
    Feature("cand.cooldown_remaining", "candidate", "int", "Turns until the spell is castable again."),
    Feature("cand.casts_this_turn", "candidate", "int", "Times the spell was already cast this turn."),
    Feature("cand.move_distance", "candidate", "int", "Cells travelled by a movement candidate."),
    Feature("cand.positioning_delta", "candidate", "int", "Change in distance to the nearest enemy."),
    Feature("cand.target_life_fraction", "candidate", "float", "Target current life divided by maximum."),
    Feature("cand.challenge_violation", "candidate", "bool", "Whether the candidate breaks a challenge."),
)


def feature_spec() -> dict[str, Any]:
    return {
        "version": FEATURE_SPEC_VERSION,
        "hash": feature_spec_hash(),
        "features": [asdict(feature) for feature in FEATURES],
    }


def feature_spec_hash() -> str:
    payload = json.dumps(
        [asdict(feature) for feature in FEATURES],
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


__all__ = [
    "FEATURES",
    "FEATURE_SPEC_VERSION",
    "Feature",
    "feature_spec",
    "feature_spec_hash",
]
