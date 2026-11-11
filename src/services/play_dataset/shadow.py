"""Phase 7 shadow evaluation: score recorded decisions without acting.

The evaluator replays Phase 4 split rows, rebuilds the Phase 5 candidates and asks
the Phase 6 model to rank them. It never sends a game action. Each decision is
written as one JSON line with the model and heuristic rankings, the human-chosen
candidate and the fight outcome. The report aggregates those lines per split and
per fight.

An offline shadow replay cannot observe the counterfactual outcome of the model's
choices. Outcome summaries are therefore observational context for the decisions,
not a causal win-rate estimate.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast

import lightgbm as lgb
import pyarrow.parquet as pq

from src.services.play_dataset.features import (
    FEATURES,
    FEATURE_SPEC_VERSION,
    feature_spec_hash,
)
from src.services.play_dataset.reranker import (
    RERANKER_SCHEMA_VERSION,
    CandidateExample,
    CandidateGroup,
    FeatureEncoder,
    candidate_group_from_row,
    predict_group_scores,
    softmax_probabilities,
)

SHADOW_SCHEMA_VERSION = 1
SHADOW_SPLITS = ("train", "val", "test")


class ShadowBlocked(RuntimeError):
    """Raised when a shadow evaluation cannot produce trustworthy output."""


@dataclass(frozen=True, slots=True)
class ShadowCandidate:
    action_type: str
    spell_id: int
    cell_id: int
    target_actor_id: int
    chosen: bool
    model_score: float
    model_probability: float
    heuristic_score: float

    @property
    def key(self) -> tuple[str, int, int, int]:
        return (self.action_type, self.spell_id, self.cell_id, self.target_actor_id)

    def to_dict(self) -> dict[str, Any]:
        return {
            "action_type": self.action_type,
            "spell_id": self.spell_id,
            "cell_id": self.cell_id,
            "target_actor_id": self.target_actor_id,
            "chosen": self.chosen,
            "model_score": self.model_score,
            "model_probability": self.model_probability,
            "heuristic_score": self.heuristic_score,
        }


@dataclass(frozen=True, slots=True)
class ShadowDecision:
    decision_id: str
    session: str
    fight_id: int
    turn: int
    breed_id: int
    actual: ShadowCandidate
    model_top: ShadowCandidate
    heuristic_top: ShadowCandidate
    candidates: tuple[ShadowCandidate, ...]
    outcome_result: str | None
    outcome_turns: int | None
    outcome_kamas: int | None
    outcome_experience: int | None
    outcome_alive: bool | None

    @property
    def model_matches_actual(self) -> bool:
        return self.model_top.key == self.actual.key

    @property
    def heuristic_matches_actual(self) -> bool:
        return self.heuristic_top.key == self.actual.key

    @property
    def model_heuristic_agree(self) -> bool:
        return self.model_top.key == self.heuristic_top.key

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision_id": self.decision_id,
            "session": self.session,
            "fight_id": self.fight_id,
            "turn": self.turn,
            "breed_id": self.breed_id,
            "actual": self.actual.to_dict(),
            "model_top": self.model_top.to_dict(),
            "heuristic_top": self.heuristic_top.to_dict(),
            "model_matches_actual": self.model_matches_actual,
            "heuristic_matches_actual": self.heuristic_matches_actual,
            "model_heuristic_agree": self.model_heuristic_agree,
            "outcome": {
                "result": self.outcome_result,
                "turns": self.outcome_turns,
                "kamas": self.outcome_kamas,
                "experience": self.outcome_experience,
                "alive": self.outcome_alive,
            },
            "candidates": [candidate.to_dict() for candidate in self.candidates],
        }


@dataclass(frozen=True, slots=True)
class ShadowGroup:
    group: CandidateGroup
    outcome_result: str | None
    outcome_turns: int | None
    outcome_kamas: int | None
    outcome_experience: int | None
    outcome_alive: bool | None


@dataclass(frozen=True, slots=True)
class ShadowModel:
    model: lgb.Booster
    encoder: FeatureEncoder
    manifest: dict[str, Any]

    @classmethod
    def load(cls, model_dir: Path) -> ShadowModel:
        manifest = _read_json_object(model_dir / "manifest.json", "model manifest")
        if manifest.get("reranker_schema_version") != RERANKER_SCHEMA_VERSION:
            raise ShadowBlocked(
                "model manifest has an unsupported reranker_schema_version"
            )
        feature_spec = manifest.get("feature_spec")
        if not isinstance(feature_spec, dict):
            raise ShadowBlocked("model manifest is missing feature_spec")
        feature_spec = cast(dict[str, Any], feature_spec)
        if feature_spec.get("version") != FEATURE_SPEC_VERSION:
            raise ShadowBlocked("model feature spec version does not match the runtime")
        if feature_spec.get("hash") != feature_spec_hash():
            raise ShadowBlocked("model feature spec hash does not match the runtime")

        encoder_payload = _read_json_object(model_dir / "encoder.json", "encoder")
        encoder = FeatureEncoder.from_dict(encoder_payload)
        expected_names = tuple(feature.name for feature in FEATURES)
        if encoder.feature_names != expected_names:
            raise ShadowBlocked("model encoder feature names do not match the runtime")

        model_path = model_dir / "model.txt"
        if not model_path.is_file():
            raise ShadowBlocked(f"model file not found: {model_path}")
        booster_factory = cast("Callable[..., lgb.Booster]", lgb.Booster)
        booster = booster_factory(model_file=str(model_path))
        return cls(model=booster, encoder=encoder, manifest=manifest)


@dataclass(frozen=True, slots=True)
class ShadowResult:
    output: Path
    report: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return self.report


def load_shadow_groups(path: Path) -> list[ShadowGroup]:
    if not path.is_file():
        return []
    rows = cast("list[dict[str, object]]", pq.read_table(path).to_pylist())
    groups: list[ShadowGroup] = []
    for row in rows:
        groups.append(
            ShadowGroup(
                group=candidate_group_from_row(row),
                outcome_result=_optional_str(row.get("outcome_result")),
                outcome_turns=_optional_int(row.get("fight_turns"))
                or _optional_int(row.get("outcome_turns")),
                outcome_kamas=_optional_int(row.get("outcome_kamas")),
                outcome_experience=_optional_int(row.get("outcome_experience")),
                outcome_alive=_optional_bool(row.get("outcome_alive")),
            )
        )
    return groups


def evaluate_shadow_group(model: ShadowModel, shadow_group: ShadowGroup) -> ShadowDecision:
    group = shadow_group.group
    scores = predict_group_scores(model.model, model.encoder, [group])
    if len(scores) != 1 or len(scores[0]) != len(group.examples):
        raise ValueError(f"model returned scores that do not match group {group.decision_id!r}")

    heuristic_scores = [example.heuristic_score for example in group.examples]
    probabilities = softmax_probabilities(scores[0])
    candidates = tuple(
        _shadow_candidate(example, score, probability)
        for example, score, probability in zip(
            group.examples, scores[0], probabilities, strict=True
        )
    )
    chosen_indices = [
        index for index, example in enumerate(group.examples) if example.chosen
    ]
    if len(chosen_indices) != 1:
        raise ValueError(
            f"group {group.decision_id!r} must have exactly one chosen candidate"
        )
    actual_index = chosen_indices[0]
    model_index = _best_index(scores[0])
    heuristic_index = _best_index(heuristic_scores)
    ranked = tuple(sorted(candidates, key=lambda candidate: (-candidate.model_score, candidate.key)))
    return ShadowDecision(
        decision_id=group.decision_id,
        session=group.session,
        fight_id=group.fight_id,
        turn=group.turn,
        breed_id=group.breed_id,
        actual=candidates[actual_index],
        model_top=candidates[model_index],
        heuristic_top=candidates[heuristic_index],
        candidates=ranked,
        outcome_result=shadow_group.outcome_result,
        outcome_turns=shadow_group.outcome_turns,
        outcome_kamas=shadow_group.outcome_kamas,
        outcome_experience=shadow_group.outcome_experience,
        outcome_alive=shadow_group.outcome_alive,
    )


def evaluate_shadow_groups(
    model: ShadowModel, groups: Sequence[ShadowGroup]
) -> list[ShadowDecision]:
    return [evaluate_shadow_group(model, group) for group in groups]


def summarize_shadow(decisions: Sequence[ShadowDecision]) -> dict[str, Any]:
    fight_rows = _fight_rows(decisions)
    return {
        "counts": {
            "decisions": len(decisions),
            "fights": len(fight_rows),
            "sessions": len({decision.session for decision in decisions}),
        },
        "ranking": {
            "model_top1_accuracy": _mean(
                [1.0 if decision.model_matches_actual else 0.0 for decision in decisions]
            ),
            "heuristic_top1_accuracy": _mean(
                [1.0 if decision.heuristic_matches_actual else 0.0 for decision in decisions]
            ),
            "model_heuristic_agreement": _mean(
                [1.0 if decision.model_heuristic_agree else 0.0 for decision in decisions]
            ),
            "model_top_actual": sum(
                1 for decision in decisions if decision.model_matches_actual
            ),
            "heuristic_top_actual": sum(
                1 for decision in decisions if decision.heuristic_matches_actual
            ),
            "model_heuristic_disagreements": sum(
                1 for decision in decisions if not decision.model_heuristic_agree
            ),
        },
        "outcomes": _outcome_summary(fight_rows),
        "outcomes_by_model_heuristic_agreement": _outcome_by_flag(
            decisions, "model_heuristic_agree"
        ),
        "outcomes_by_model_match": _outcome_by_flag(decisions, "model_matches_actual"),
        "fights": fight_rows,
        "caveat": (
            "Offline shadow replay cannot observe the counterfactual outcome of the "
            "model's choices; outcome summaries are observational, not causal."
        ),
    }


def evaluate_shadow(
    dataset_dir: Path,
    model_dir: Path,
    output: Path,
    *,
    split: str = "test",
) -> ShadowResult:
    if split not in SHADOW_SPLITS:
        raise ValueError(f"split must be one of {SHADOW_SPLITS}")
    model = ShadowModel.load(model_dir)
    groups = load_shadow_groups(dataset_dir / "splits" / f"{split}.parquet")
    if not groups:
        raise ShadowBlocked(f"the {split} split has no usable decisions")

    decisions = evaluate_shadow_groups(model, groups)
    report = summarize_shadow(decisions)
    report.update(
        {
            "shadow_schema_version": SHADOW_SCHEMA_VERSION,
            "generated_at": _now(),
            "dataset_dir": str(dataset_dir),
            "model_dir": str(model_dir),
            "split": split,
            "feature_spec": {
                "version": FEATURE_SPEC_VERSION,
                "hash": feature_spec_hash(),
                "count": len(FEATURES),
            },
            "model_gate": model.manifest.get("gate"),
        }
    )
    output.mkdir(parents=True, exist_ok=True)
    _write_shadow_jsonl(output / "shadow.jsonl", decisions, split=split)
    (output / "report.json").write_text(
        json.dumps(report, indent=2, sort_keys=True), encoding="utf-8"
    )
    return ShadowResult(output=output, report=report)


def _shadow_candidate(
    example: CandidateExample, model_score: float, model_probability: float
) -> ShadowCandidate:
    return ShadowCandidate(
        action_type=example.action_type,
        spell_id=example.spell_id,
        cell_id=example.cell_id,
        target_actor_id=example.target_actor_id,
        chosen=example.chosen,
        model_score=model_score,
        model_probability=model_probability,
        heuristic_score=example.heuristic_score,
    )


def _best_index(scores: Sequence[float]) -> int:
    if not scores:
        raise ValueError("cannot rank an empty candidate list")
    return max(range(len(scores)), key=lambda index: (scores[index], -index))


def _fight_rows(decisions: Sequence[ShadowDecision]) -> list[dict[str, Any]]:
    by_fight: dict[tuple[str, int], list[ShadowDecision]] = {}
    for decision in decisions:
        by_fight.setdefault((decision.session, decision.fight_id), []).append(decision)

    rows: list[dict[str, Any]] = []
    for (session, fight_id), fight_decisions in sorted(by_fight.items()):
        first = fight_decisions[0]
        rows.append(
            {
                "session": session,
                "fight_id": fight_id,
                "result": first.outcome_result,
                "turns": first.outcome_turns,
                "kamas": first.outcome_kamas,
                "experience": first.outcome_experience,
                "alive": first.outcome_alive,
                "decisions": len(fight_decisions),
                "model_heuristic_agreements": sum(
                    1 for decision in fight_decisions if decision.model_heuristic_agree
                ),
                "model_matches_actual": sum(
                    1 for decision in fight_decisions if decision.model_matches_actual
                ),
                "heuristic_matches_actual": sum(
                    1 for decision in fight_decisions if decision.heuristic_matches_actual
                ),
            }
        )
    return rows


def _outcome_summary(fight_rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    results: dict[str, int] = {}
    known_results = 0
    victories = 0
    known_alive = 0
    alive = 0
    turns: list[int] = []
    kamas: list[int] = []
    experience: list[int] = []
    for row in fight_rows:
        result = row.get("result")
        if isinstance(result, str):
            known_results += 1
            results[result] = results.get(result, 0) + 1
            if result == "victory":
                victories += 1
        row_alive = row.get("alive")
        if isinstance(row_alive, bool):
            known_alive += 1
            alive += int(row_alive)
        row_turns = row.get("turns")
        if isinstance(row_turns, int) and not isinstance(row_turns, bool):
            turns.append(row_turns)
        row_kamas = row.get("kamas")
        if isinstance(row_kamas, int) and not isinstance(row_kamas, bool):
            kamas.append(row_kamas)
        row_experience = row.get("experience")
        if isinstance(row_experience, int) and not isinstance(row_experience, bool):
            experience.append(row_experience)

    return {
        "fights": len(fight_rows),
        "results": dict(sorted(results.items())),
        "known_results": known_results,
        "victories": victories,
        "win_rate": victories / known_results if known_results else None,
        "alive_rate": alive / known_alive if known_alive else None,
        "avg_turns": _mean(turns),
        "avg_kamas": _mean(kamas),
        "avg_experience": _mean(experience),
    }


def _outcome_by_flag(
    decisions: Sequence[ShadowDecision], flag: str
) -> dict[str, dict[str, Any]]:
    buckets: dict[str, list[ShadowDecision]] = {"true": [], "false": []}
    for decision in decisions:
        value = getattr(decision, flag)
        buckets["true" if value else "false"].append(decision)
    return {
        name: {
            "decisions": len(bucket),
            "results": _result_counts(bucket),
        }
        for name, bucket in buckets.items()
    }


def _result_counts(decisions: Sequence[ShadowDecision]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for decision in decisions:
        if decision.outcome_result is not None:
            counts[decision.outcome_result] = counts.get(decision.outcome_result, 0) + 1
    return dict(sorted(counts.items()))


def _write_shadow_jsonl(
    path: Path, decisions: Sequence[ShadowDecision], *, split: str
) -> None:
    generated_at = _now()
    with path.open("w", encoding="utf-8") as file:
        for decision in decisions:
            record: dict[str, Any] = {
                "shadow_schema_version": SHADOW_SCHEMA_VERSION,
                "generated_at": generated_at,
                "split": split,
                **decision.to_dict(),
            }
            file.write(json.dumps(record, sort_keys=True, ensure_ascii=False) + "\n")


def _read_json_object(path: Path, label: str) -> dict[str, Any]:
    if not path.is_file():
        raise ShadowBlocked(f"{label} not found: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ShadowBlocked(f"could not read {label}: {path}") from error
    if not isinstance(payload, dict):
        raise ShadowBlocked(f"{label} must contain a JSON object: {path}")
    return cast(dict[str, Any], payload)


def _optional_str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _optional_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    return value if isinstance(value, int) else None


def _optional_bool(value: object) -> bool | None:
    return value if isinstance(value, bool) else None


def _mean(values: Sequence[int | float]) -> float | None:
    return sum(values) / len(values) if values else None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


__all__ = [
    "SHADOW_SCHEMA_VERSION",
    "SHADOW_SPLITS",
    "ShadowBlocked",
    "ShadowCandidate",
    "ShadowDecision",
    "ShadowGroup",
    "ShadowModel",
    "ShadowResult",
    "evaluate_shadow",
    "evaluate_shadow_group",
    "evaluate_shadow_groups",
    "load_shadow_groups",
    "summarize_shadow",
]
