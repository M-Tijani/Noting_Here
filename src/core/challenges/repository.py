from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from src.core.challenges.models import CONFLICTING_RULES, ChallengeDefinition, ChallengeRule
from src.utils.project_paths import BUNDLE_ROOT, USER_DATA_ROOT

DEFAULT_CHALLENGES_PATH = USER_DATA_ROOT / "resources" / "challenges.json"
BUNDLED_CHALLENGES_PATH = BUNDLE_ROOT / "resources" / "challenges.json"


class ChallengeDataError(ValueError):
    pass


class _ChallengeEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: int = Field(gt=0)
    name: str = Field(min_length=1)
    description: str = ""
    rules: tuple[ChallengeRule, ...] = ()
    priority: int = 0
    enabled: bool = True
    incompatible_with: tuple[int, ...] = ()
    easy: bool = False


class _ChallengeFile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    challenges: tuple[_ChallengeEntry, ...]


class ChallengeRepository:
    def __init__(self, definitions: tuple[ChallengeDefinition, ...]) -> None:
        self.definitions = definitions
        self._by_id = {definition.id: definition for definition in definitions}

    @classmethod
    def from_file(cls, path: Path | None = None) -> ChallengeRepository:
        if path is None:
            path = DEFAULT_CHALLENGES_PATH
            if not path.is_file():
                path = BUNDLED_CHALLENGES_PATH
        try:
            payload = _ChallengeFile.model_validate_json(path.read_text(encoding="utf-8"))
        except ValidationError as error:
            raise ChallengeDataError(f"invalid challenge file {path}: {error}") from error

        # The game data re-issues challenges under new ids, and the extra rows
        # carry no rules. Rules are a property of the challenge name, so reuse
        # the first rule-bearing row for every same-named duplicate.
        rules_by_name: dict[str, tuple[ChallengeRule, ...]] = {}
        for entry in payload.challenges:
            if entry.rules:
                rules_by_name.setdefault(entry.name, entry.rules)

        definitions = tuple(
            ChallengeDefinition(
                id=entry.id,
                name=entry.name,
                description=entry.description,
                rules=entry.rules or rules_by_name.get(entry.name, ()),
                priority=entry.priority,
                enabled=entry.enabled,
                incompatible_with=entry.incompatible_with,
                easy=entry.easy,
            )
            for entry in payload.challenges
        )
        cls._validate(definitions)
        return cls(definitions)

    @staticmethod
    def _validate(definitions: tuple[ChallengeDefinition, ...]) -> None:
        seen_ids: set[int] = set()
        for definition in definitions:
            if definition.id in seen_ids:
                raise ChallengeDataError(f"duplicate challenge id: {definition.id}")
            seen_ids.add(definition.id)

            if definition.id in definition.incompatible_with:
                raise ChallengeDataError(
                    f"challenge {definition.id} cannot be incompatible with itself"
                )
            if len(set(definition.incompatible_with)) != len(definition.incompatible_with):
                raise ChallengeDataError(
                    f"challenge {definition.id} has duplicate incompatible_with ids"
                )

            rules = set(definition.rules)
            for conflict in CONFLICTING_RULES:
                if conflict.issubset(rules):
                    names = ", ".join(sorted(rule.value for rule in conflict))
                    raise ChallengeDataError(
                        f"challenge {definition.id} has conflicting rules: {names}"
                    )

    def get(self, challenge_id: int) -> ChallengeDefinition | None:
        return self._by_id.get(challenge_id)

    def list_definitions(self, *, easy_only: bool = False) -> tuple[ChallengeDefinition, ...]:
        if not easy_only:
            return self.definitions
        return tuple(definition for definition in self.definitions if definition.easy)

    def default_preferred_ids(self) -> tuple[int, ...]:
        easy = [definition for definition in self.definitions if definition.easy and definition.enabled]
        easy.sort(key=lambda definition: (-definition.priority, self.definitions.index(definition)))
        return tuple(definition.id for definition in easy)


@lru_cache(maxsize=1)
def get_default_challenge_repository() -> ChallengeRepository:
    return ChallengeRepository.from_file()
