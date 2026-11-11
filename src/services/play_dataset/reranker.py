"""Train and evaluate the Phase 6 candidate re-ranker.

Each recorded decision becomes one ranking group: Phase 5 extracts the candidates,
the recorded action identifies the chosen candidate, and every candidate carries the
frozen 41-feature vector. The model is a LightGBM LambdaRank re-ranker; the heuristic
baseline is a damage-first ordering over the same candidates.

The pipeline refuses to train on degenerate labels (all decisions choosing the same
action) or without a held-out session unless explicitly overridden. A successful run
still writes a manifest whose gate can remain closed.
"""

from __future__ import annotations

import json
import math
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast

import lightgbm as lgb
import numpy as np
import numpy.typing as npt
import pyarrow.parquet as pq

from src.services.play_dataset.candidates import (
    Candidate,
    attack_context_from_observation,
    extract_candidates,
    feature_vector,
    match_action,
)
from src.services.play_dataset.features import (
    FEATURES,
    FEATURE_SPEC_VERSION,
    Feature,
    feature_spec_hash,
)

RERANKER_SCHEMA_VERSION = 1
SPLIT_NAMES = ("train", "val", "test")
DEFAULT_MIN_HOLDOUT_GROUPS = 100
_REPO_ROOT = Path(__file__).resolve().parents[3]

FeatureValue = int | float | bool | str


class TrainingBlocked(RuntimeError):
    """Raised when the dataset cannot produce a trustworthy re-ranker."""


@dataclass(frozen=True, slots=True)
class CandidateExample:
    action_type: str
    features: tuple[FeatureValue, ...]
    chosen: bool
    heuristic_score: float
    spell_id: int = 0
    cell_id: int = 0
    target_actor_id: int = 0


@dataclass(frozen=True, slots=True)
class CandidateGroup:
    decision_id: str
    session: str
    fight_id: int
    turn: int
    breed_id: int
    examples: tuple[CandidateExample, ...]


@dataclass(frozen=True, slots=True)
class TrainingConfig:
    seed: int = 42
    n_estimators: int = 200
    learning_rate: float = 0.05
    num_leaves: int = 15
    min_child_samples: int = 10
    min_holdout_groups: int = DEFAULT_MIN_HOLDOUT_GROUPS
    allow_degenerate_labels: bool = False
    allow_no_holdout: bool = False


@dataclass(frozen=True, slots=True)
class RankingMetrics:
    groups: int
    candidates: int
    top1_accuracy: float
    ndcg_at_1: float
    ndcg_at_3: float
    ndcg_at_5: float
    log_loss: float
    expected_calibration_error: float

    def to_dict(self) -> dict[str, float | int]:
        return {
            "groups": self.groups,
            "candidates": self.candidates,
            "top1_accuracy": self.top1_accuracy,
            "ndcg_at_1": self.ndcg_at_1,
            "ndcg_at_3": self.ndcg_at_3,
            "ndcg_at_5": self.ndcg_at_5,
            "log_loss": self.log_loss,
            "expected_calibration_error": self.expected_calibration_error,
        }


@dataclass(slots=True)
class TrainingResult:
    output: Path
    manifest: dict[str, Any] = field(default_factory=dict[str, Any])

    def to_dict(self) -> dict[str, Any]:
        return self.manifest


@dataclass(slots=True)
class FeatureEncoder:
    feature_names: tuple[str, ...]
    categorical_indices: tuple[int, ...]
    categories: dict[str, dict[str, int]]

    @classmethod
    def fit(cls, groups: list[CandidateGroup]) -> FeatureEncoder:
        categories: dict[str, dict[str, int]] = {
            feature.name: {} for feature in FEATURES if feature.dtype == "categorical"
        }
        for group in groups:
            for example in group.examples:
                for index, feature in enumerate(FEATURES):
                    if feature.dtype != "categorical":
                        continue
                    mapping = categories[feature.name]
                    key = _category_key(example.features[index])
                    if key not in mapping:
                        mapping[key] = len(mapping) + 1
        return cls(
            feature_names=tuple(feature.name for feature in FEATURES),
            categorical_indices=tuple(
                index for index, feature in enumerate(FEATURES) if feature.dtype == "categorical"
            ),
            categories=categories,
        )

    def transform(self, groups: list[CandidateGroup]) -> npt.NDArray[np.float64]:
        rows: list[list[float]] = []
        for group in groups:
            for example in group.examples:
                rows.append(
                    [
                        self._encode(feature, example.features[index])
                        for index, feature in enumerate(FEATURES)
                    ]
                )
        return np.asarray(rows, dtype=np.float64)

    def _encode(self, feature: Feature, value: FeatureValue) -> float:
        if feature.dtype == "categorical":
            return float(self.categories[feature.name].get(_category_key(value), 0))
        if feature.dtype == "bool":
            return 1.0 if bool(value) else 0.0
        if isinstance(value, bool):
            return 1.0 if value else 0.0
        if isinstance(value, (int, float)):
            return float(value)
        raise ValueError(f"feature {feature.name} expected a number, got {value!r}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "feature_names": list(self.feature_names),
            "categorical_indices": list(self.categorical_indices),
            "categories": self.categories,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> FeatureEncoder:
        feature_names = payload.get("feature_names")
        categorical_indices = payload.get("categorical_indices")
        categories = payload.get("categories")
        if not isinstance(feature_names, list) or not all(
            isinstance(name, str) for name in cast(list[object], feature_names)
        ):
            raise ValueError("encoder feature_names must be a list of strings")
        if not isinstance(categorical_indices, list) or not all(
            isinstance(index, int) for index in cast(list[object], categorical_indices)
        ):
            raise ValueError("encoder categorical_indices must be a list of integers")
        if not isinstance(categories, dict):
            raise ValueError("encoder categories must be an object")
        parsed_categories: dict[str, dict[str, int]] = {}
        for name, mapping in cast(dict[object, object], categories).items():
            if not isinstance(name, str) or not isinstance(mapping, dict):
                raise ValueError("encoder categories must map names to objects")
            parsed_mapping: dict[str, int] = {}
            for key, code in cast(dict[object, object], mapping).items():
                if not isinstance(key, str) or not isinstance(code, int):
                    raise ValueError("encoder category codes must be integers")
                parsed_mapping[key] = code
            parsed_categories[name] = parsed_mapping
        return cls(
            feature_names=tuple(cast(list[str], feature_names)),
            categorical_indices=tuple(cast(list[int], categorical_indices)),
            categories=parsed_categories,
        )


def load_candidate_groups(path: Path) -> list[CandidateGroup]:
    if not path.is_file():
        return []
    table = pq.read_table(path)
    rows = cast("list[dict[str, object]]", table.to_pylist())
    return [candidate_group_from_row(row) for row in rows]


def candidate_group_from_row(row: Mapping[str, object]) -> CandidateGroup:
    observation = _json_object(row.get("observation_json"), "observation_json")
    action = _json_object(row.get("action_json"), "action_json")
    context = attack_context_from_observation(observation)
    candidates = extract_candidates(context)
    chosen = match_action(candidates, action)
    if chosen is None:
        raise ValueError(
            f"recorded action did not match a candidate for decision {row.get('decision_id')!r}"
        )
    chosen_index = candidates.index(chosen)
    examples = tuple(
        CandidateExample(
            action_type=candidate.action_type,
            features=tuple(feature_vector(context, candidate)),
            chosen=index == chosen_index,
            heuristic_score=heuristic_score(candidate),
            spell_id=candidate.spell_id,
            cell_id=candidate.cell_id,
            target_actor_id=candidate.target_actor_id,
        )
        for index, candidate in enumerate(candidates)
    )
    return CandidateGroup(
        decision_id=_as_str(row.get("decision_id")),
        session=_as_str(row.get("session")),
        fight_id=_as_int(row.get("fight_id")),
        turn=_as_int(row.get("turn")),
        breed_id=context.breed_id,
        examples=examples,
    )


def heuristic_score(candidate: Candidate) -> float:
    """Damage-first baseline: prefer legal kills, then damage, then cheaper actions."""
    if candidate.challenge_violation:
        return -1_000_000.0
    score = 0.0
    if candidate.is_kill:
        score += 1_000_000.0
    score += candidate.kill_probability * 100_000.0
    score += candidate.damage_resist_adjusted
    score -= candidate.ap_cost * 10.0
    score -= candidate.mp_cost
    score -= abs(candidate.positioning_delta) * 0.1
    score -= candidate.target_distance * 0.01
    return score


def train_reranker(
    dataset_dir: Path,
    output: Path,
    *,
    config: TrainingConfig | None = None,
) -> TrainingResult:
    config = config or TrainingConfig()
    split_paths = {name: dataset_dir / "splits" / f"{name}.parquet" for name in SPLIT_NAMES}
    groups = {name: load_candidate_groups(path) for name, path in split_paths.items()}
    return train_reranker_groups(
        groups["train"],
        groups["val"],
        groups["test"],
        output,
        config=config,
        dataset_dir=dataset_dir,
    )


def train_reranker_groups(
    train_groups: list[CandidateGroup],
    val_groups: list[CandidateGroup],
    test_groups: list[CandidateGroup],
    output: Path,
    *,
    config: TrainingConfig,
    dataset_dir: Path | None = None,
) -> TrainingResult:
    if not train_groups:
        raise TrainingBlocked("the train split has no usable decisions")
    _validate_groups(train_groups, "train")
    _validate_groups(val_groups, "val")
    _validate_groups(test_groups, "test")

    chosen_actions = _chosen_action_counts(train_groups)
    degenerate = len(chosen_actions) <= 1
    if degenerate and not config.allow_degenerate_labels:
        raise TrainingBlocked(
            "training labels are degenerate "
            f"({chosen_actions or {'none': 0}}); collect decisions with more than one action"
        )
    if not val_groups and not test_groups and not config.allow_no_holdout:
        raise TrainingBlocked(
            "the dataset has no held-out sessions; rebuild it with more sessions "
            "or pass --allow-no-holdout for a pipeline smoke run"
        )

    encoder = FeatureEncoder.fit(train_groups)
    model = _fit_model(train_groups, val_groups, encoder, config)
    split_scores = {
        "train": predict_group_scores(model, encoder, train_groups),
        "val": predict_group_scores(model, encoder, val_groups),
        "test": predict_group_scores(model, encoder, test_groups),
    }
    model_metrics = {
        name: evaluate_ranking(groups, scores)
        for name, groups, scores in (
            ("train", train_groups, split_scores["train"]),
            ("val", val_groups, split_scores["val"]),
            ("test", test_groups, split_scores["test"]),
        )
    }
    heuristic_metrics = {
        name: evaluate_ranking(
            split_groups, [[example.heuristic_score for example in group.examples] for group in split_groups]
        )
        for name, split_groups in (
            ("train", train_groups),
            ("val", val_groups),
            ("test", test_groups),
        )
    }

    gate = _gate(
        train_groups,
        test_groups,
        model_metrics["test"],
        heuristic_metrics["test"],
        chosen_actions,
        config,
    )
    output.mkdir(parents=True, exist_ok=True)
    model.save_model(str(output / "model.txt"))
    _write_json(output / "encoder.json", encoder.to_dict())
    _write_json(
        output / "metrics.json",
        {
            "model": {name: metrics.to_dict() for name, metrics in model_metrics.items()},
            "heuristic": {name: metrics.to_dict() for name, metrics in heuristic_metrics.items()},
        },
    )
    manifest = _manifest(
        train_groups,
        val_groups,
        test_groups,
        config,
        model,
        model_metrics,
        heuristic_metrics,
        split_scores,
        chosen_actions,
        gate,
        dataset_dir,
    )
    _write_json(output / "manifest.json", manifest)
    return TrainingResult(output=output, manifest=manifest)


def evaluate_ranking(
    groups: list[CandidateGroup], scores: list[list[float]]
) -> RankingMetrics:
    if len(groups) != len(scores):
        raise ValueError("groups and scores must have the same length")
    group_count = 0
    candidate_count = 0
    top1 = 0.0
    ndcg_1 = 0.0
    ndcg_3 = 0.0
    ndcg_5 = 0.0
    log_loss = 0.0
    calibration: list[tuple[float, float]] = []

    for group, group_scores in zip(groups, scores, strict=True):
        if len(group_scores) != len(group.examples):
            raise ValueError("each score list must match its group's candidate count")
        chosen_index = next(
            (index for index, example in enumerate(group.examples) if example.chosen), None
        )
        if chosen_index is None:
            raise ValueError(f"group {group.decision_id!r} has no chosen candidate")
        if not group_scores:
            continue
        group_count += 1
        candidate_count += len(group_scores)
        ranked = _rank_descending(group_scores)
        chosen_rank = ranked.index(chosen_index)
        top1 += 1.0 if chosen_rank == 0 else 0.0
        ndcg_1 += _ndcg(chosen_rank, 1)
        ndcg_3 += _ndcg(chosen_rank, 3)
        ndcg_5 += _ndcg(chosen_rank, 5)
        probability = _softmax_probability(group_scores, chosen_index)
        log_loss -= math.log(max(probability, 1e-15))
        calibration.extend(
            (
                _softmax_probability(group_scores, index),
                1.0 if example.chosen else 0.0,
            )
            for index, example in enumerate(group.examples)
        )

    if group_count == 0:
        return RankingMetrics(0, 0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
    return RankingMetrics(
        groups=group_count,
        candidates=candidate_count,
        top1_accuracy=top1 / group_count,
        ndcg_at_1=ndcg_1 / group_count,
        ndcg_at_3=ndcg_3 / group_count,
        ndcg_at_5=ndcg_5 / group_count,
        log_loss=log_loss / group_count,
        expected_calibration_error=_expected_calibration_error(calibration),
    )


def _fit_model(
    train_groups: list[CandidateGroup],
    val_groups: list[CandidateGroup],
    encoder: FeatureEncoder,
    config: TrainingConfig,
) -> lgb.Booster:
    params: dict[str, object] = {
        "objective": "lambdarank",
        "metric": "ndcg",
        "n_estimators": config.n_estimators,
        "learning_rate": config.learning_rate,
        "num_leaves": config.num_leaves,
        "min_data_in_leaf": config.min_child_samples,
        "seed": config.seed,
        "num_threads": 1,
        "deterministic": True,
        "force_col_wise": True,
        "verbosity": -1,
    }
    x_train = encoder.transform(train_groups)
    train_data = lgb.Dataset(
        x_train,
        label=_labels(train_groups),
        group=_group_sizes(train_groups),
        feature_name=list(encoder.feature_names),
        categorical_feature=list(encoder.categorical_indices),
    )
    valid_sets: list[lgb.Dataset] = []
    if val_groups:
        valid_sets.append(
            lgb.Dataset(
                encoder.transform(val_groups),
                label=_labels(val_groups),
                group=_group_sizes(val_groups),
                feature_name=list(encoder.feature_names),
                categorical_feature=list(encoder.categorical_indices),
            )
        )
    return lgb.train(
        params,
        train_data,
        num_boost_round=config.n_estimators,
        valid_sets=valid_sets,
        callbacks=[lgb.early_stopping(20, verbose=False)] if valid_sets else None,
    )


def predict_group_scores(
    model: lgb.Booster, encoder: FeatureEncoder, groups: list[CandidateGroup]
) -> list[list[float]]:
    if not groups:
        return []
    raw_scores = model.predict(encoder.transform(groups))
    if not isinstance(raw_scores, np.ndarray):
        raise TypeError("LightGBM returned a non-array prediction")
    flat_scores = [float(score) for score in raw_scores.tolist()]
    grouped: list[list[float]] = []
    offset = 0
    for group in groups:
        size = len(group.examples)
        grouped.append(flat_scores[offset : offset + size])
        offset += size
    return grouped


def softmax_probabilities(scores: list[float]) -> list[float]:
    if not scores:
        return []
    max_score = max(scores)
    weights = [math.exp(score - max_score) for score in scores]
    total = sum(weights)
    return [weight / total for weight in weights] if total else [0.0 for _ in scores]


def _labels(groups: list[CandidateGroup]) -> list[int]:
    return [1 if example.chosen else 0 for group in groups for example in group.examples]


def _group_sizes(groups: list[CandidateGroup]) -> list[int]:
    return [len(group.examples) for group in groups]


def _rank_descending(scores: list[float]) -> list[int]:
    return sorted(range(len(scores)), key=lambda index: (-scores[index], index))


def _ndcg(chosen_rank: int, k: int) -> float:
    if chosen_rank >= k:
        return 0.0
    return 1.0 / math.log2(chosen_rank + 2)


def _softmax_probability(scores: list[float], index: int) -> float:
    max_score = max(scores)
    weights = [math.exp(score - max_score) for score in scores]
    total = sum(weights)
    return weights[index] / total if total else 0.0


def _expected_calibration_error(pairs: list[tuple[float, float]]) -> float:
    if not pairs:
        return 0.0
    bins: list[list[tuple[float, float]]] = [[] for _ in range(10)]
    for probability, outcome in pairs:
        bins[min(int(probability * 10), 9)].append((probability, outcome))
    error = 0.0
    for bucket in bins:
        if not bucket:
            continue
        mean_probability = sum(probability for probability, _ in bucket) / len(bucket)
        mean_outcome = sum(outcome for _, outcome in bucket) / len(bucket)
        error += len(bucket) / len(pairs) * abs(mean_probability - mean_outcome)
    return error


def _gate(
    train_groups: list[CandidateGroup],
    test_groups: list[CandidateGroup],
    model_metrics: RankingMetrics,
    heuristic_metrics: RankingMetrics,
    chosen_actions: dict[str, int],
    config: TrainingConfig,
) -> dict[str, Any]:
    reasons: list[str] = []
    if len(chosen_actions) <= 1:
        reasons.append("degenerate training labels")
    if not test_groups:
        reasons.append("no test split")
    else:
        if len(test_groups) < config.min_holdout_groups:
            reasons.append(
                f"test split has {len(test_groups)} groups, needs {config.min_holdout_groups}"
            )
        if model_metrics.top1_accuracy <= heuristic_metrics.top1_accuracy:
            reasons.append("model does not beat the heuristic on top-1 accuracy")
    return {
        "ready_for_shadow": not reasons,
        "reasons": reasons,
        "min_holdout_groups": config.min_holdout_groups,
        "model_top1": model_metrics.top1_accuracy,
        "heuristic_top1": heuristic_metrics.top1_accuracy,
        "train_groups": len(train_groups),
        "test_groups": len(test_groups),
    }


def _manifest(
    train_groups: list[CandidateGroup],
    val_groups: list[CandidateGroup],
    test_groups: list[CandidateGroup],
    config: TrainingConfig,
    model: lgb.Booster,
    model_metrics: dict[str, RankingMetrics],
    heuristic_metrics: dict[str, RankingMetrics],
    model_scores: dict[str, list[list[float]]],
    chosen_actions: dict[str, int],
    gate: dict[str, Any],
    dataset_dir: Path | None,
) -> dict[str, Any]:
    return {
        "reranker_schema_version": RERANKER_SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "git_sha": _git_sha(),
        "dataset_dir": str(dataset_dir) if dataset_dir is not None else None,
        "feature_spec": {
            "version": FEATURE_SPEC_VERSION,
            "hash": feature_spec_hash(),
            "count": len(FEATURES),
        },
        "config": {
            "seed": config.seed,
            "n_estimators": config.n_estimators,
            "learning_rate": config.learning_rate,
            "num_leaves": config.num_leaves,
            "min_child_samples": config.min_child_samples,
            "min_holdout_groups": config.min_holdout_groups,
            "allow_degenerate_labels": config.allow_degenerate_labels,
            "allow_no_holdout": config.allow_no_holdout,
        },
        "counts": {
            "train": _split_counts(train_groups),
            "val": _split_counts(val_groups),
            "test": _split_counts(test_groups),
            "chosen_actions": chosen_actions,
        },
        "model": {
            "best_iteration": _best_iteration(model, config.n_estimators),
            "feature_importance": sorted(
                zip(
                    (feature.name for feature in FEATURES),
                    (float(value) for value in model.feature_importance(importance_type="split")),
                    strict=True,
                ),
                key=lambda item: (-item[1], item[0]),
            )[:10],
        },
        "metrics": {
            "model": {name: metrics.to_dict() for name, metrics in model_metrics.items()},
            "heuristic": {name: metrics.to_dict() for name, metrics in heuristic_metrics.items()},
            "by_breed": _by_breed_metrics(
                {
                    "train": train_groups,
                    "val": val_groups,
                    "test": test_groups,
                },
                model_scores,
            ),
        },
        "gate": gate,
        "artifacts": {
            "model": "model.txt",
            "encoder": "encoder.json",
            "metrics": "metrics.json",
        },
    }


def _by_breed_metrics(
    groups_by_split: dict[str, list[CandidateGroup]],
    model_scores: dict[str, list[list[float]]],
) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for name, groups in groups_by_split.items():
        scores = model_scores[name]
        if len(groups) != len(scores):
            raise ValueError("model scores must align with groups")
        breed_groups: dict[int, list[CandidateGroup]] = {}
        breed_scores: dict[int, list[list[float]]] = {}
        for group, group_scores in zip(groups, scores, strict=True):
            breed_groups.setdefault(group.breed_id, []).append(group)
            breed_scores.setdefault(group.breed_id, []).append(group_scores)
        for breed_id in sorted(breed_groups):
            split_groups = breed_groups[breed_id]
            heuristic_scores = [
                [example.heuristic_score for example in group.examples]
                for group in split_groups
            ]
            result.setdefault(str(breed_id), {})[name] = {
                "groups": len(split_groups),
                "candidates": sum(len(group.examples) for group in split_groups),
                "model": evaluate_ranking(split_groups, breed_scores[breed_id]).to_dict(),
                "heuristic": evaluate_ranking(split_groups, heuristic_scores).to_dict(),
            }
    return result


def _best_iteration(model: lgb.Booster, fallback: int) -> int:
    best = int(model.best_iteration)
    return best if best > 0 else fallback


def _validate_groups(groups: list[CandidateGroup], split_name: str) -> None:
    feature_count = len(FEATURES)
    for group in groups:
        if not group.examples:
            raise ValueError(f"{split_name} group {group.decision_id!r} has no candidates")
        chosen = [example for example in group.examples if example.chosen]
        if len(chosen) != 1:
            raise ValueError(
                f"{split_name} group {group.decision_id!r} must have exactly one chosen candidate"
            )
        for example in group.examples:
            if len(example.features) != feature_count:
                raise ValueError(
                    f"{split_name} group {group.decision_id!r} has "
                    f"{len(example.features)} features, expected {feature_count}"
                )


def _split_counts(groups: list[CandidateGroup]) -> dict[str, int]:
    return {
        "groups": len(groups),
        "candidates": sum(len(group.examples) for group in groups),
        "sessions": len({group.session for group in groups}),
        "fights": len({(group.session, group.fight_id) for group in groups}),
    }


def _chosen_action_counts(groups: list[CandidateGroup]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for group in groups:
        chosen = next(example.action_type for example in group.examples if example.chosen)
        counts[chosen] = counts.get(chosen, 0) + 1
    return dict(sorted(counts.items()))


def _category_key(value: FeatureValue) -> str:
    if isinstance(value, bool):
        return str(int(value))
    return str(value)


def _json_object(raw: object, field_name: str) -> dict[str, object]:
    if not isinstance(raw, str) or not raw:
        raise ValueError(f"{field_name} must be a non-empty JSON string")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError(f"{field_name} must contain a JSON object")
    return cast("dict[str, object]", value)


def _as_str(value: object) -> str:
    return value if isinstance(value, str) else ""


def _as_int(value: object) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return 0
    return 0


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def _git_sha() -> str | None:
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=_REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    sha = completed.stdout.strip()
    return sha if completed.returncode == 0 and sha else None


__all__ = [
    "CandidateExample",
    "CandidateGroup",
    "FeatureEncoder",
    "RankingMetrics",
    "TrainingBlocked",
    "TrainingConfig",
    "TrainingResult",
    "candidate_group_from_row",
    "evaluate_ranking",
    "heuristic_score",
    "load_candidate_groups",
    "predict_group_scores",
    "softmax_probabilities",
    "train_reranker",
    "train_reranker_groups",
]
