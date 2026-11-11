import datetime
import os
from pathlib import Path
from random import uniform
from typing import Literal

from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict

from DBDofusUnity.dofus_unity_reader.game_constants.job import JobEnum
from DBDofusUnity.dofus_unity_reader.game_constants.map_id import MapIdEnum
from src.utils.project_paths import ENV_PATH, IS_PACKAGED, USER_DATA_ROOT
from utils.env_config import get_bool_from_env, get_int_from_env

load_dotenv(ENV_PATH)
DEBUG = get_bool_from_env("DEBUG", default=True)
ENABLE_MSG_CAPTURE = not IS_PACKAGED

ENABLE_SESSION_CONTEXT = False

# Fast fights: shorter in-fight pacing, no defensive end-of-turn retreat and no
# AP reservation for summons. Combat still heals/buffs as usual.
FAST_FIGHT = get_bool_from_env("FAST_FIGHT", default=True)
FAST_FIGHT_TIMING_MULTIPLIER = 0.25

# Play traces power the "copy me" fight agent (docs/fight_ai_plan.md). The
# recorder is independent from DEBUG so datasets survive normal play sessions.
PLAY_RECORDING_ENABLED = get_bool_from_env("PLAY_RECORDING", default=True)
PLAY_RECORDING_DIR = Path(os.getenv("PLAY_RECORDING_DIR", str(USER_DATA_ROOT / "resources")))
PLAY_RECORDING_PROFILE = os.getenv("PLAY_RECORDING_PROFILE", "decisions").strip().lower()
PLAY_RECORDING_RETENTION_DAYS = get_int_from_env("PLAY_RECORDING_RETENTION_DAYS", default=180)


StorageTarget = Literal["bank", "guild_chest"]


class BehaviorSettings(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    do_fighter: bool = True
    do_sale_hotel: bool = True
    do_craft: bool = False
    storage_target: StorageTarget = "guild_chest"
    do_dungeon: bool = False
    do_idle: bool = False
    enable_auto_equipment_market_purchases: bool = False
    enable_auto_ogrine_subscriptions: bool = False
    enable_auto_paysafecard_subscriptions: bool = False
    fight_allowed_spell_ids: list[int] = []  # empty = every known spell


class GlobalSettings(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    behaviors: BehaviorSettings = BehaviorSettings()
    enable_account_automation: bool = False
    hide_fight_animations: bool = False
    sonji_api_key: str | None = None
    discord_bot_token: str | None = None
    discord_channel_id: int | None = None


FIGHT_GROUP_LVL_MULTIPLIER = 2
FIGHT_GROUP_LVL_OFFSET = 5


def get_default_fight_group_lvl_limit(level: int) -> float:
    return level * FIGHT_GROUP_LVL_MULTIPLIER + FIGHT_GROUP_LVL_OFFSET


USEFUL_UNLOAD = 0.15

OCCUPIED_MESSAGE_ID: int = 474
OCCUPIED_STUCK_LIMIT = 2

BOT_MINIMAL_KAMAS: int = 2_000_000
LOW_KAMAS_ALERT_THRESHOLD: int = get_int_from_env(
    "DISCORD_LOW_KAMAS_THRESHOLD", default=4_000
)
LOW_KAMAS_RECOVERY_THRESHOLD: int = get_int_from_env(
    "DISCORD_LOW_KAMAS_RECOVERY_THRESHOLD", default=100_000
)
MULE_BANK_MAP_ID = MapIdEnum.ASTRUB_BANK

# Bank used to unload when pods are full. ``None`` keeps the automatic pick
# (the allowed bank nearest a zaap). Set a ``MapIdEnum`` bank to force one,
# e.g. ``UNLOAD_BANK_MAP_ID = MapIdEnum.BONTA_BANK``. A forced bank that the
# account cannot reach is ignored and the automatic pick is used instead.
UNLOAD_BANK_MAP_ID: int | None = None

MAX_QUANTITY_ON_SELL = 10_000
MIN_KAMAS_TO_GO_SALE_HOTEL = 1_500


def get_time_beween_sale_hotel_prices():
    return datetime.timedelta(hours=4, minutes=0) * uniform(0.75, 1.25)


def get_time_beween_areas():
    return datetime.timedelta(hours=1) * uniform(0.75, 1.25)


def get_time_between_attacker(enabled: bool) -> datetime.timedelta:
    return (
        datetime.timedelta(minutes=20) * uniform(0.75, 1.25)
        if enabled
        else datetime.timedelta(datetime.MAXYEAR)
    )


WEIGHT_BY_JOB: dict[JobEnum, float] = {
    JobEnum.MINER: 5,
    JobEnum.WOODCUTTER: 5,
    JobEnum.ALCHEMIST: 5,
    JobEnum.FISHERMAN: 5,
    JobEnum.PEASANT: 2.5,
    JobEnum.BASE: 1,
}


BETWEEN_COLLECT_PAUSE_PROBABILITY = 0.1
FIRST_COLLECT_MOVEMENT_CANCEL_PROBABILITY = 1 / 3
SUBSEQUENT_COLLECT_MOVEMENT_CANCEL_PROBABILITY = 1 / 8
STATIC_INTERACTION_CANCEL_PROBABILITY = 1 / 8

PLACEMENT_REPOSITIONING_PROBABILITY = 0.08
PLACEMENT_NON_OPTIMAL_MOVE_PROBABILITY = 0.1
