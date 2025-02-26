"""Statistics helper."""
from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Iterable
import contextlib
import dataclasses
from datetime import datetime, timedelta
from itertools import chain, groupby
import json
import logging
import os
import re
from statistics import mean
from typing import TYPE_CHECKING, Any, Literal, overload

from sqlalchemy import bindparam, func, lambda_stmt, select
from sqlalchemy.engine.row import Row
from sqlalchemy.exc import SQLAlchemyError, StatementError
from sqlalchemy.orm.session import Session
from sqlalchemy.sql.expression import literal_column, true
from sqlalchemy.sql.lambdas import StatementLambdaElement
import voluptuous as vol

from homeassistant.const import (
    PRESSURE_PA,
    TEMP_CELSIUS,
    VOLUME_CUBIC_FEET,
    VOLUME_CUBIC_METERS,
)
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry
from homeassistant.helpers.json import JSONEncoder
from homeassistant.helpers.storage import STORAGE_DIR
import homeassistant.util.dt as dt_util
import homeassistant.util.pressure as pressure_util
import homeassistant.util.temperature as temperature_util
from homeassistant.util.unit_system import UnitSystem
import homeassistant.util.volume as volume_util

from .const import DATA_INSTANCE, DOMAIN, MAX_ROWS_TO_PURGE, SupportedDialect
from .models import (
    StatisticData,
    StatisticMetaData,
    StatisticResult,
    Statistics,
    StatisticsMeta,
    StatisticsRuns,
    StatisticsShortTerm,
    process_timestamp,
    process_timestamp_to_utc_isoformat,
)
from .util import (
    execute,
    execute_stmt_lambda_element,
    retryable_database_job,
    session_scope,
)

if TYPE_CHECKING:
    from . import Recorder

QUERY_STATISTICS = [
    Statistics.metadata_id,
    Statistics.start,
    Statistics.mean,
    Statistics.min,
    Statistics.max,
    Statistics.last_reset,
    Statistics.state,
    Statistics.sum,
]

QUERY_STATISTICS_SHORT_TERM = [
    StatisticsShortTerm.metadata_id,
    StatisticsShortTerm.start,
    StatisticsShortTerm.mean,
    StatisticsShortTerm.min,
    StatisticsShortTerm.max,
    StatisticsShortTerm.last_reset,
    StatisticsShortTerm.state,
    StatisticsShortTerm.sum,
]

QUERY_STATISTICS_SUMMARY_MEAN = [
    StatisticsShortTerm.metadata_id,
    func.avg(StatisticsShortTerm.mean),
    func.min(StatisticsShortTerm.min),
    func.max(StatisticsShortTerm.max),
]

QUERY_STATISTICS_SUMMARY_SUM = [
    StatisticsShortTerm.metadata_id,
    StatisticsShortTerm.start,
    StatisticsShortTerm.last_reset,
    StatisticsShortTerm.state,
    StatisticsShortTerm.sum,
    func.row_number()
    .over(
        partition_by=StatisticsShortTerm.metadata_id,
        order_by=StatisticsShortTerm.start.desc(),
    )
    .label("rownum"),
]

QUERY_STATISTICS_SUMMARY_SUM_LEGACY = [
    StatisticsShortTerm.metadata_id,
    StatisticsShortTerm.last_reset,
    StatisticsShortTerm.state,
    StatisticsShortTerm.sum,
]

QUERY_STATISTIC_META = [
    StatisticsMeta.id,
    StatisticsMeta.statistic_id,
    StatisticsMeta.source,
    StatisticsMeta.unit_of_measurement,
    StatisticsMeta.has_mean,
    StatisticsMeta.has_sum,
    StatisticsMeta.name,
]

QUERY_STATISTIC_META_ID = [
    StatisticsMeta.id,
    StatisticsMeta.statistic_id,
]


# Convert pressure, temperature and volume statistics from the normalized unit used for
# statistics to the unit configured by the user
STATISTIC_UNIT_TO_DISPLAY_UNIT_CONVERSIONS = {
    PRESSURE_PA: lambda x, units: pressure_util.convert(
        x, PRESSURE_PA, units.pressure_unit
    )
    if x is not None
    else None,
    TEMP_CELSIUS: lambda x, units: temperature_util.convert(
        x, TEMP_CELSIUS, units.temperature_unit
    )
    if x is not None
    else None,
    VOLUME_CUBIC_METERS: lambda x, units: volume_util.convert(
        x, VOLUME_CUBIC_METERS, _configured_unit(VOLUME_CUBIC_METERS, units)
    )
    if x is not None
    else None,
}

# Convert volume statistics from the display unit configured by the user
# to the normalized unit used for statistics
# This is used to support adjusting statistics in the display unit
DISPLAY_UNIT_TO_STATISTIC_UNIT_CONVERSIONS: dict[
    str, Callable[[float, UnitSystem], float]
] = {
    VOLUME_CUBIC_FEET: lambda x, units: volume_util.convert(
        x, _configured_unit(VOLUME_CUBIC_METERS, units), VOLUME_CUBIC_METERS
    ),
}

_LOGGER = logging.getLogger(__name__)


@dataclasses.dataclass
class PlatformCompiledStatistics:
    """Compiled Statistics from a platform."""

    platform_stats: list[StatisticResult]
    current_metadata: dict[str, tuple[int, StatisticMetaData]]


def split_statistic_id(entity_id: str) -> list[str]:
    """Split a state entity ID into domain and object ID."""
    return entity_id.split(":", 1)


VALID_STATISTIC_ID = re.compile(r"^(?!.+__)(?!_)[\da-z_]+(?<!_):(?!_)[\da-z_]+(?<!_)$")


def valid_statistic_id(statistic_id: str) -> bool:
    """Test if a statistic ID is a valid format.

    Format: <domain>:<statistic> where both are slugs.
    """
    return VALID_STATISTIC_ID.match(statistic_id) is not None


def validate_statistic_id(value: str) -> str:
    """Validate statistic ID."""
    if valid_statistic_id(value):
        return value

    raise vol.Invalid(f"Statistics ID {value} is an invalid statistic ID")


@dataclasses.dataclass
class ValidationIssue:
    """Error or warning message."""

    type: str
    data: dict[str, str | None] | None = None

    def as_dict(self) -> dict:
        """Return dictionary version."""
        return dataclasses.asdict(self)


def async_setup(hass: HomeAssistant) -> None:
    """Set up the history hooks."""

    def _entity_id_changed(event: Event) -> None:
        """Handle entity_id changed."""
        old_entity_id = event.data["old_entity_id"]
        entity_id = event.data["entity_id"]
        with session_scope(hass=hass) as session:
            session.query(StatisticsMeta).filter(
                (StatisticsMeta.statistic_id == old_entity_id)
                & (StatisticsMeta.source == DOMAIN)
            ).update({StatisticsMeta.statistic_id: entity_id})

    async def _async_entity_id_changed(event: Event) -> None:
        await hass.data[DATA_INSTANCE].async_add_executor_job(_entity_id_changed, event)

    @callback
    def entity_registry_changed_filter(event: Event) -> bool:
        """Handle entity_id changed filter."""
        if event.data["action"] != "update" or "old_entity_id" not in event.data:
            return False

        return True

    if hass.is_running:
        hass.bus.async_listen(
            entity_registry.EVENT_ENTITY_REGISTRY_UPDATED,
            _async_entity_id_changed,
            event_filter=entity_registry_changed_filter,
        )


def get_start_time() -> datetime:
    """Return start time."""
    now = dt_util.utcnow()
    current_period_minutes = now.minute - now.minute % 5
    current_period = now.replace(minute=current_period_minutes, second=0, microsecond=0)
    last_period = current_period - timedelta(minutes=5)
    return last_period


def _update_or_add_metadata(
    session: Session,
    new_metadata: StatisticMetaData,
    old_metadata_dict: dict[str, tuple[int, StatisticMetaData]],
) -> int:
    """Get metadata_id for a statistic_id.

    If the statistic_id is previously unknown, add it. If it's already known, update
    metadata if needed.

    Updating metadata source is not possible.
    """
    statistic_id = new_metadata["statistic_id"]
    if statistic_id not in old_metadata_dict:
        meta = StatisticsMeta.from_meta(new_metadata)
        session.add(meta)
        session.flush()  # Flush to get the metadata id assigned
        _LOGGER.debug(
            "Added new statistics metadata for %s, new_metadata: %s",
            statistic_id,
            new_metadata,
        )
        return meta.id  # type: ignore[no-any-return]

    metadata_id, old_metadata = old_metadata_dict[statistic_id]
    if (
        old_metadata["has_mean"] != new_metadata["has_mean"]
        or old_metadata["has_sum"] != new_metadata["has_sum"]
        or old_metadata["unit_of_measurement"] != new_metadata["unit_of_measurement"]
    ):
        session.query(StatisticsMeta).filter_by(statistic_id=statistic_id).update(
            {
                StatisticsMeta.has_mean: new_metadata["has_mean"],
                StatisticsMeta.has_sum: new_metadata["has_sum"],
                StatisticsMeta.unit_of_measurement: new_metadata["unit_of_measurement"],
            },
            synchronize_session=False,
        )
        _LOGGER.debug(
            "Updated statistics metadata for %s, old_metadata: %s, new_metadata: %s",
            statistic_id,
            old_metadata,
            new_metadata,
        )

    return metadata_id


def _find_duplicates(
    session: Session, table: type[Statistics | StatisticsShortTerm]
) -> tuple[list[int], list[dict]]:
    """Find duplicated statistics."""
    subquery = (
        session.query(
            table.start,
            table.metadata_id,
            literal_column("1").label("is_duplicate"),
        )
        .group_by(table.metadata_id, table.start)
        .having(func.count() > 1)
        .subquery()
    )
    query = (
        session.query(table)
        .outerjoin(
            subquery,
            (subquery.c.metadata_id == table.metadata_id)
            & (subquery.c.start == table.start),
        )
        .filter(subquery.c.is_duplicate == 1)
        .order_by(table.metadata_id, table.start, table.id.desc())
        .limit(1000 * MAX_ROWS_TO_PURGE)
    )
    duplicates = execute(query)
    original_as_dict = {}
    start = None
    metadata_id = None
    duplicate_ids: list[int] = []
    non_identical_duplicates_as_dict: list[dict] = []

    if not duplicates:
        return (duplicate_ids, non_identical_duplicates_as_dict)

    def columns_to_dict(duplicate: type[Statistics | StatisticsShortTerm]) -> dict:
        """Convert a SQLAlchemy row to dict."""
        dict_ = {}
        for key in duplicate.__mapper__.c.keys():
            dict_[key] = getattr(duplicate, key)
        return dict_

    def compare_statistic_rows(row1: dict, row2: dict) -> bool:
        """Compare two statistics rows, ignoring id and created."""
        ignore_keys = ["id", "created"]
        keys1 = set(row1).difference(ignore_keys)
        keys2 = set(row2).difference(ignore_keys)
        return keys1 == keys2 and all(row1[k] == row2[k] for k in keys1)

    for duplicate in duplicates:
        if start != duplicate.start or metadata_id != duplicate.metadata_id:
            original_as_dict = columns_to_dict(duplicate)
            start = duplicate.start
            metadata_id = duplicate.metadata_id
            continue
        duplicate_as_dict = columns_to_dict(duplicate)
        duplicate_ids.append(duplicate.id)
        if not compare_statistic_rows(original_as_dict, duplicate_as_dict):
            non_identical_duplicates_as_dict.append(
                {"duplicate": duplicate_as_dict, "original": original_as_dict}
            )

    return (duplicate_ids, non_identical_duplicates_as_dict)


def _delete_duplicates_from_table(
    session: Session, table: type[Statistics | StatisticsShortTerm]
) -> tuple[int, list[dict]]:
    """Identify and delete duplicated statistics from a specified table."""
    all_non_identical_duplicates: list[dict] = []
    total_deleted_rows = 0
    while True:
        duplicate_ids, non_identical_duplicates = _find_duplicates(session, table)
        if not duplicate_ids:
            break
        all_non_identical_duplicates.extend(non_identical_duplicates)
        for i in range(0, len(duplicate_ids), MAX_ROWS_TO_PURGE):
            deleted_rows = (
                session.query(table)
                .filter(table.id.in_(duplicate_ids[i : i + MAX_ROWS_TO_PURGE]))
                .delete(synchronize_session=False)
            )
            total_deleted_rows += deleted_rows
    return (total_deleted_rows, all_non_identical_duplicates)


def delete_duplicates(hass: HomeAssistant, session: Session) -> None:
    """Identify and delete duplicated statistics.

    A backup will be made of duplicated statistics before it is deleted.
    """
    deleted_statistics_rows, non_identical_duplicates = _delete_duplicates_from_table(
        session, Statistics
    )
    if deleted_statistics_rows:
        _LOGGER.info("Deleted %s duplicated statistics rows", deleted_statistics_rows)

    if non_identical_duplicates:
        isotime = dt_util.utcnow().isoformat()
        backup_file_name = f"deleted_statistics.{isotime}.json"
        backup_path = hass.config.path(STORAGE_DIR, backup_file_name)

        os.makedirs(os.path.dirname(backup_path), exist_ok=True)
        with open(backup_path, "w", encoding="utf8") as backup_file:
            json.dump(
                non_identical_duplicates,
                backup_file,
                indent=4,
                sort_keys=True,
                cls=JSONEncoder,
            )
        _LOGGER.warning(
            "Deleted %s non identical duplicated %s rows, a backup of the deleted rows "
            "has been saved to %s",
            len(non_identical_duplicates),
            Statistics.__tablename__,
            backup_path,
        )

    deleted_short_term_statistics_rows, _ = _delete_duplicates_from_table(
        session, StatisticsShortTerm
    )
    if deleted_short_term_statistics_rows:
        _LOGGER.warning(
            "Deleted duplicated short term statistic rows, please report at %s",
            "https://github.com/home-assistant/core/issues?q=is%3Aopen+is%3Aissue+label%3A%22integration%3A+recorder%22",
        )


def _compile_hourly_statistics_summary_mean_stmt(
    start_time: datetime, end_time: datetime
) -> StatementLambdaElement:
    """Generate the summary mean statement for hourly statistics."""
    stmt = lambda_stmt(lambda: select(*QUERY_STATISTICS_SUMMARY_MEAN))
    stmt += (
        lambda q: q.filter(StatisticsShortTerm.start >= start_time)
        .filter(StatisticsShortTerm.start < end_time)
        .group_by(StatisticsShortTerm.metadata_id)
        .order_by(StatisticsShortTerm.metadata_id)
    )
    return stmt


def compile_hourly_statistics(
    instance: Recorder, session: Session, start: datetime
) -> None:
    """Compile hourly statistics.

    This will summarize 5-minute statistics for one hour:
    - average, min max is computed by a database query
    - sum is taken from the last 5-minute entry during the hour
    """
    start_time = start.replace(minute=0)
    end_time = start_time + timedelta(hours=1)

    # Compute last hour's average, min, max
    summary: dict[str, StatisticData] = {}
    stmt = _compile_hourly_statistics_summary_mean_stmt(start_time, end_time)
    stats = execute_stmt_lambda_element(session, stmt)

    if stats:
        for stat in stats:
            metadata_id, _mean, _min, _max = stat
            summary[metadata_id] = {
                "start": start_time,
                "mean": _mean,
                "min": _min,
                "max": _max,
            }

    # Get last hour's last sum
    subquery = (
        session.query(*QUERY_STATISTICS_SUMMARY_SUM)
        .filter(StatisticsShortTerm.start >= bindparam("start_time"))
        .filter(StatisticsShortTerm.start < bindparam("end_time"))
        .subquery()
    )
    query = (
        session.query(subquery)
        .filter(subquery.c.rownum == 1)
        .order_by(subquery.c.metadata_id)
    )
    stats = execute(query.params(start_time=start_time, end_time=end_time))

    if stats:
        for stat in stats:
            metadata_id, start, last_reset, state, _sum, _ = stat
            if metadata_id in summary:
                summary[metadata_id].update(
                    {
                        "last_reset": process_timestamp(last_reset),
                        "state": state,
                        "sum": _sum,
                    }
                )
            else:
                summary[metadata_id] = {
                    "start": start_time,
                    "last_reset": process_timestamp(last_reset),
                    "state": state,
                    "sum": _sum,
                }

    # Insert compiled hourly statistics in the database
    for metadata_id, stat in summary.items():
        session.add(Statistics.from_stats(metadata_id, stat))


@retryable_database_job("statistics")
def compile_statistics(instance: Recorder, start: datetime) -> bool:
    """Compile 5-minute statistics for all integrations with a recorder platform.

    The actual calculation is delegated to the platforms.
    """
    start = dt_util.as_utc(start)
    end = start + timedelta(minutes=5)

    # Return if we already have 5-minute statistics for the requested period
    with session_scope(session=instance.get_session()) as session:
        if session.query(StatisticsRuns).filter_by(start=start).first():
            _LOGGER.debug("Statistics already compiled for %s-%s", start, end)
            return True

    _LOGGER.debug("Compiling statistics for %s-%s", start, end)
    platform_stats: list[StatisticResult] = []
    current_metadata: dict[str, tuple[int, StatisticMetaData]] = {}
    # Collect statistics from all platforms implementing support
    for domain, platform in instance.hass.data[DOMAIN].items():
        if not hasattr(platform, "compile_statistics"):
            continue
        compiled: PlatformCompiledStatistics = platform.compile_statistics(
            instance.hass, start, end
        )
        _LOGGER.debug(
            "Statistics for %s during %s-%s: %s",
            domain,
            start,
            end,
            compiled.platform_stats,
        )
        platform_stats.extend(compiled.platform_stats)
        current_metadata.update(compiled.current_metadata)

    # Insert collected statistics in the database
    with session_scope(
        session=instance.get_session(),
        exception_filter=_filter_unique_constraint_integrity_error(instance),
    ) as session:
        for stats in platform_stats:
            metadata_id = _update_or_add_metadata(
                session, stats["meta"], current_metadata
            )
            _insert_statistics(
                session,
                StatisticsShortTerm,
                metadata_id,
                stats["stat"],
            )

        if start.minute == 55:
            # A full hour is ready, summarize it
            compile_hourly_statistics(instance, session, start)

        session.add(StatisticsRuns(start=start))

    return True


def _adjust_sum_statistics(
    session: Session,
    table: type[Statistics | StatisticsShortTerm],
    metadata_id: int,
    start_time: datetime,
    adj: float,
) -> None:
    """Adjust statistics in the database."""
    try:
        session.query(table).filter_by(metadata_id=metadata_id).filter(
            table.start >= start_time
        ).update(
            {
                table.sum: table.sum + adj,
            },
            synchronize_session=False,
        )
    except SQLAlchemyError:
        _LOGGER.exception(
            "Unexpected exception when updating statistics %s",
            id,
        )


def _insert_statistics(
    session: Session,
    table: type[Statistics | StatisticsShortTerm],
    metadata_id: int,
    statistic: StatisticData,
) -> None:
    """Insert statistics in the database."""
    try:
        session.add(table.from_stats(metadata_id, statistic))
    except SQLAlchemyError:
        _LOGGER.exception(
            "Unexpected exception when inserting statistics %s:%s ",
            metadata_id,
            statistic,
        )


def _update_statistics(
    session: Session,
    table: type[Statistics | StatisticsShortTerm],
    stat_id: int,
    statistic: StatisticData,
) -> None:
    """Insert statistics in the database."""
    try:
        session.query(table).filter_by(id=stat_id).update(
            {
                table.mean: statistic.get("mean"),
                table.min: statistic.get("min"),
                table.max: statistic.get("max"),
                table.last_reset: statistic.get("last_reset"),
                table.state: statistic.get("state"),
                table.sum: statistic.get("sum"),
            },
            synchronize_session=False,
        )
    except SQLAlchemyError:
        _LOGGER.exception(
            "Unexpected exception when updating statistics %s:%s ",
            stat_id,
            statistic,
        )


def _generate_get_metadata_stmt(
    statistic_ids: list[str] | tuple[str] | None = None,
    statistic_type: Literal["mean"] | Literal["sum"] | None = None,
    statistic_source: str | None = None,
) -> StatementLambdaElement:
    """Generate a statement to fetch metadata."""
    stmt = lambda_stmt(lambda: select(*QUERY_STATISTIC_META))
    if statistic_ids is not None:
        stmt += lambda q: q.where(StatisticsMeta.statistic_id.in_(statistic_ids))
    if statistic_source is not None:
        stmt += lambda q: q.where(StatisticsMeta.source == statistic_source)
    if statistic_type == "mean":
        stmt += lambda q: q.where(StatisticsMeta.has_mean == true())
    elif statistic_type == "sum":
        stmt += lambda q: q.where(StatisticsMeta.has_sum == true())
    return stmt


def get_metadata_with_session(
    hass: HomeAssistant,
    session: Session,
    *,
    statistic_ids: list[str] | tuple[str] | None = None,
    statistic_type: Literal["mean"] | Literal["sum"] | None = None,
    statistic_source: str | None = None,
) -> dict[str, tuple[int, StatisticMetaData]]:
    """Fetch meta data.

    Returns a dict of (metadata_id, StatisticMetaData) tuples indexed by statistic_id.

    If statistic_ids is given, fetch metadata only for the listed statistics_ids.
    If statistic_type is given, fetch metadata only for statistic_ids supporting it.
    """

    # Fetch metatadata from the database
    stmt = _generate_get_metadata_stmt(statistic_ids, statistic_type, statistic_source)
    result = execute_stmt_lambda_element(session, stmt)
    if not result:
        return {}

    return {
        meta["statistic_id"]: (
            meta["id"],
            {
                "source": meta["source"],
                "statistic_id": meta["statistic_id"],
                "unit_of_measurement": meta["unit_of_measurement"],
                "has_mean": meta["has_mean"],
                "has_sum": meta["has_sum"],
                "name": meta["name"],
            },
        )
        for meta in result
    }


def get_metadata(
    hass: HomeAssistant,
    *,
    statistic_ids: list[str] | tuple[str] | None = None,
    statistic_type: Literal["mean"] | Literal["sum"] | None = None,
    statistic_source: str | None = None,
) -> dict[str, tuple[int, StatisticMetaData]]:
    """Return metadata for statistic_ids."""
    with session_scope(hass=hass) as session:
        return get_metadata_with_session(
            hass,
            session,
            statistic_ids=statistic_ids,
            statistic_type=statistic_type,
            statistic_source=statistic_source,
        )


@overload
def _configured_unit(unit: None, units: UnitSystem) -> None:
    ...


@overload
def _configured_unit(unit: str, units: UnitSystem) -> str:
    ...


def _configured_unit(unit: str | None, units: UnitSystem) -> str | None:
    """Return the pressure and temperature units configured by the user."""
    if unit == PRESSURE_PA:
        return units.pressure_unit
    if unit == TEMP_CELSIUS:
        return units.temperature_unit
    if unit == VOLUME_CUBIC_METERS:
        if units.is_metric:
            return VOLUME_CUBIC_METERS
        return VOLUME_CUBIC_FEET
    return unit


def clear_statistics(instance: Recorder, statistic_ids: list[str]) -> None:
    """Clear statistics for a list of statistic_ids."""
    with session_scope(session=instance.get_session()) as session:
        session.query(StatisticsMeta).filter(
            StatisticsMeta.statistic_id.in_(statistic_ids)
        ).delete(synchronize_session=False)


def update_statistics_metadata(
    instance: Recorder, statistic_id: str, unit_of_measurement: str | None
) -> None:
    """Update statistics metadata for a statistic_id."""
    with session_scope(session=instance.get_session()) as session:
        session.query(StatisticsMeta).filter(
            StatisticsMeta.statistic_id == statistic_id
        ).update({StatisticsMeta.unit_of_measurement: unit_of_measurement})


def list_statistic_ids(
    hass: HomeAssistant,
    statistic_ids: list[str] | tuple[str] | None = None,
    statistic_type: Literal["mean"] | Literal["sum"] | None = None,
) -> list[dict]:
    """Return all statistic_ids (or filtered one) and unit of measurement.

    Queries the database for existing statistic_ids, as well as integrations with
    a recorder platform for statistic_ids which will be added in the next statistics
    period.
    """
    units = hass.config.units
    result = {}

    # Query the database
    with session_scope(hass=hass) as session:
        metadata = get_metadata_with_session(
            hass, session, statistic_type=statistic_type, statistic_ids=statistic_ids
        )

        for _, meta in metadata.values():
            if (unit := meta["unit_of_measurement"]) is not None:
                # Display unit according to user settings
                unit = _configured_unit(unit, units)
            meta["unit_of_measurement"] = unit

        result = {
            meta["statistic_id"]: {
                "has_mean": meta["has_mean"],
                "has_sum": meta["has_sum"],
                "name": meta["name"],
                "source": meta["source"],
                "unit_of_measurement": meta["unit_of_measurement"],
            }
            for _, meta in metadata.values()
        }

    # Query all integrations with a registered recorder platform
    for platform in hass.data[DOMAIN].values():
        if not hasattr(platform, "list_statistic_ids"):
            continue
        platform_statistic_ids = platform.list_statistic_ids(
            hass, statistic_ids=statistic_ids, statistic_type=statistic_type
        )

        for statistic_id, info in platform_statistic_ids.items():
            if (unit := info["unit_of_measurement"]) is not None:
                # Display unit according to user settings
                unit = _configured_unit(unit, units)
            platform_statistic_ids[statistic_id]["unit_of_measurement"] = unit

        for key, value in platform_statistic_ids.items():
            result.setdefault(key, value)

    # Return a list of statistic_id + metadata
    return [
        {
            "statistic_id": _id,
            "has_mean": info["has_mean"],
            "has_sum": info["has_sum"],
            "name": info.get("name"),
            "source": info["source"],
            "unit_of_measurement": info["unit_of_measurement"],
        }
        for _id, info in result.items()
    ]


def _reduce_statistics(
    stats: dict[str, list[dict[str, Any]]],
    same_period: Callable[[datetime, datetime], bool],
    period_start_end: Callable[[datetime], tuple[datetime, datetime]],
    period: timedelta,
) -> dict[str, list[dict[str, Any]]]:
    """Reduce hourly statistics to daily or monthly statistics."""
    result: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for statistic_id, stat_list in stats.items():
        max_values: list[float] = []
        mean_values: list[float] = []
        min_values: list[float] = []
        prev_stat: dict[str, Any] = stat_list[0]

        # Loop over the hourly statistics + a fake entry to end the period
        for statistic in chain(
            stat_list, ({"start": stat_list[-1]["start"] + period},)
        ):
            if not same_period(prev_stat["start"], statistic["start"]):
                start, end = period_start_end(prev_stat["start"])
                # The previous statistic was the last entry of the period
                result[statistic_id].append(
                    {
                        "statistic_id": statistic_id,
                        "start": start.isoformat(),
                        "end": end.isoformat(),
                        "mean": mean(mean_values) if mean_values else None,
                        "min": min(min_values) if min_values else None,
                        "max": max(max_values) if max_values else None,
                        "last_reset": prev_stat.get("last_reset"),
                        "state": prev_stat.get("state"),
                        "sum": prev_stat["sum"],
                    }
                )
                max_values = []
                mean_values = []
                min_values = []
            if statistic.get("max") is not None:
                max_values.append(statistic["max"])
            if statistic.get("mean") is not None:
                mean_values.append(statistic["mean"])
            if statistic.get("min") is not None:
                min_values.append(statistic["min"])
            prev_stat = statistic

    return result


def same_day(time1: datetime, time2: datetime) -> bool:
    """Return True if time1 and time2 are in the same date."""
    date1 = dt_util.as_local(time1).date()
    date2 = dt_util.as_local(time2).date()
    return date1 == date2


def day_start_end(time: datetime) -> tuple[datetime, datetime]:
    """Return the start and end of the period (day) time is within."""
    start = dt_util.as_utc(
        dt_util.as_local(time).replace(hour=0, minute=0, second=0, microsecond=0)
    )
    end = start + timedelta(days=1)
    return (start, end)


def _reduce_statistics_per_day(
    stats: dict[str, list[dict[str, Any]]]
) -> dict[str, list[dict[str, Any]]]:
    """Reduce hourly statistics to daily statistics."""

    return _reduce_statistics(stats, same_day, day_start_end, timedelta(days=1))


def same_month(time1: datetime, time2: datetime) -> bool:
    """Return True if time1 and time2 are in the same year and month."""
    date1 = dt_util.as_local(time1).date()
    date2 = dt_util.as_local(time2).date()
    return (date1.year, date1.month) == (date2.year, date2.month)


def month_start_end(time: datetime) -> tuple[datetime, datetime]:
    """Return the start and end of the period (month) time is within."""
    start_local = dt_util.as_local(time).replace(
        day=1, hour=0, minute=0, second=0, microsecond=0
    )
    start = dt_util.as_utc(start_local)
    end_local = (start_local + timedelta(days=31)).replace(day=1)
    end = dt_util.as_utc(end_local)
    return (start, end)


def _reduce_statistics_per_month(
    stats: dict[str, list[dict[str, Any]]],
) -> dict[str, list[dict[str, Any]]]:
    """Reduce hourly statistics to monthly statistics."""

    return _reduce_statistics(stats, same_month, month_start_end, timedelta(days=31))


def _statistics_during_period_stmt(
    start_time: datetime,
    end_time: datetime | None,
    statistic_ids: list[str] | None,
    metadata_ids: list[int] | None,
    table: type[Statistics | StatisticsShortTerm],
) -> StatementLambdaElement:
    """Prepare a database query for statistics during a given period.

    This prepares a lambda_stmt query, so we don't insert the parameters yet.
    """
    if table == StatisticsShortTerm:
        stmt = lambda_stmt(lambda: select(*QUERY_STATISTICS_SHORT_TERM))
    else:
        stmt = lambda_stmt(lambda: select(*QUERY_STATISTICS))

    stmt += lambda q: q.filter(table.start >= start_time)

    if end_time is not None:
        stmt += lambda q: q.filter(table.start < end_time)

    if statistic_ids is not None:
        stmt += lambda q: q.filter(table.metadata_id.in_(metadata_ids))

    stmt += lambda q: q.order_by(table.metadata_id, table.start)
    return stmt


def statistics_during_period(
    hass: HomeAssistant,
    start_time: datetime,
    end_time: datetime | None = None,
    statistic_ids: list[str] | None = None,
    period: Literal["5minute", "day", "hour", "month"] = "hour",
    start_time_as_datetime: bool = False,
) -> dict[str, list[dict[str, Any]]]:
    """Return statistics during UTC period start_time - end_time for the statistic_ids.

    If end_time is omitted, returns statistics newer than or equal to start_time.
    If statistic_ids is omitted, returns statistics for all statistics ids.
    """
    metadata = None
    with session_scope(hass=hass) as session:
        # Fetch metadata for the given (or all) statistic_ids
        metadata = get_metadata_with_session(hass, session, statistic_ids=statistic_ids)
        if not metadata:
            return {}

        metadata_ids = None
        if statistic_ids is not None:
            metadata_ids = [metadata_id for metadata_id, _ in metadata.values()]

        if period == "5minute":
            table = StatisticsShortTerm
        else:
            table = Statistics

        stmt = _statistics_during_period_stmt(
            start_time, end_time, statistic_ids, metadata_ids, table
        )
        stats = execute_stmt_lambda_element(session, stmt)

        if not stats:
            return {}
        # Return statistics combined with metadata
        if period not in ("day", "month"):
            return _sorted_statistics_to_dict(
                hass,
                session,
                stats,
                statistic_ids,
                metadata,
                True,
                table,
                start_time,
                start_time_as_datetime,
            )

        result = _sorted_statistics_to_dict(
            hass, session, stats, statistic_ids, metadata, True, table, start_time, True
        )

        if period == "day":
            return _reduce_statistics_per_day(result)

        return _reduce_statistics_per_month(result)


def _get_last_statistics_stmt(
    metadata_id: int,
    number_of_stats: int,
    table: type[Statistics | StatisticsShortTerm],
) -> StatementLambdaElement:
    """Generate a statement for number_of_stats statistics for a given statistic_id."""
    if table == StatisticsShortTerm:
        stmt = lambda_stmt(lambda: select(*QUERY_STATISTICS_SHORT_TERM))
    else:
        stmt = lambda_stmt(lambda: select(*QUERY_STATISTICS))
    stmt += (
        lambda q: q.filter_by(metadata_id=metadata_id)
        .order_by(table.metadata_id, table.start.desc())
        .limit(number_of_stats)
    )
    return stmt


def _get_last_statistics(
    hass: HomeAssistant,
    number_of_stats: int,
    statistic_id: str,
    convert_units: bool,
    table: type[Statistics | StatisticsShortTerm],
) -> dict[str, list[dict]]:
    """Return the last number_of_stats statistics for a given statistic_id."""
    statistic_ids = [statistic_id]
    with session_scope(hass=hass) as session:
        # Fetch metadata for the given statistic_id
        metadata = get_metadata_with_session(hass, session, statistic_ids=statistic_ids)
        if not metadata:
            return {}
        metadata_id = metadata[statistic_id][0]
        stmt = _get_last_statistics_stmt(metadata_id, number_of_stats, table)
        stats = execute_stmt_lambda_element(session, stmt)

        if not stats:
            return {}

        # Return statistics combined with metadata
        return _sorted_statistics_to_dict(
            hass,
            session,
            stats,
            statistic_ids,
            metadata,
            convert_units,
            table,
            None,
        )


def get_last_statistics(
    hass: HomeAssistant, number_of_stats: int, statistic_id: str, convert_units: bool
) -> dict[str, list[dict]]:
    """Return the last number_of_stats statistics for a statistic_id."""
    return _get_last_statistics(
        hass, number_of_stats, statistic_id, convert_units, Statistics
    )


def get_last_short_term_statistics(
    hass: HomeAssistant, number_of_stats: int, statistic_id: str, convert_units: bool
) -> dict[str, list[dict]]:
    """Return the last number_of_stats short term statistics for a statistic_id."""
    return _get_last_statistics(
        hass, number_of_stats, statistic_id, convert_units, StatisticsShortTerm
    )


def _latest_short_term_statistics_stmt(
    metadata_ids: list[int],
) -> StatementLambdaElement:
    """Create the statement for finding the latest short term stat rows."""
    stmt = lambda_stmt(lambda: select(*QUERY_STATISTICS_SHORT_TERM))
    most_recent_statistic_row = (
        select(
            StatisticsShortTerm.metadata_id,
            func.max(StatisticsShortTerm.start).label("start_max"),
        )
        .where(StatisticsShortTerm.metadata_id.in_(metadata_ids))
        .group_by(StatisticsShortTerm.metadata_id)
    ).subquery()
    stmt += lambda s: s.join(
        most_recent_statistic_row,
        (
            StatisticsShortTerm.metadata_id  # pylint: disable=comparison-with-callable
            == most_recent_statistic_row.c.metadata_id
        )
        & (StatisticsShortTerm.start == most_recent_statistic_row.c.start_max),
    )
    return stmt


def get_latest_short_term_statistics(
    hass: HomeAssistant,
    statistic_ids: list[str],
    metadata: dict[str, tuple[int, StatisticMetaData]] | None = None,
) -> dict[str, list[dict]]:
    """Return the latest short term statistics for a list of statistic_ids."""
    with session_scope(hass=hass) as session:
        # Fetch metadata for the given statistic_ids
        if not metadata:
            metadata = get_metadata_with_session(
                hass, session, statistic_ids=statistic_ids
            )
        if not metadata:
            return {}
        metadata_ids = [
            metadata[statistic_id][0]
            for statistic_id in statistic_ids
            if statistic_id in metadata
        ]
        stmt = _latest_short_term_statistics_stmt(metadata_ids)
        stats = execute_stmt_lambda_element(session, stmt)
        if not stats:
            return {}

        # Return statistics combined with metadata
        return _sorted_statistics_to_dict(
            hass,
            session,
            stats,
            statistic_ids,
            metadata,
            False,
            StatisticsShortTerm,
            None,
        )


def _statistics_at_time(
    session: Session,
    metadata_ids: set[int],
    table: type[Statistics | StatisticsShortTerm],
    start_time: datetime,
) -> list | None:
    """Return last known statistics, earlier than start_time, for the metadata_ids."""
    # Fetch metadata for the given (or all) statistic_ids
    if table == StatisticsShortTerm:
        base_query = QUERY_STATISTICS_SHORT_TERM
    else:
        base_query = QUERY_STATISTICS

    query = session.query(*base_query)

    most_recent_statistic_ids = (
        session.query(
            func.max(table.id).label("max_id"),
        )
        .filter(table.start < start_time)
        .filter(table.metadata_id.in_(metadata_ids))
    )
    most_recent_statistic_ids = most_recent_statistic_ids.group_by(table.metadata_id)
    most_recent_statistic_ids = most_recent_statistic_ids.subquery()
    query = query.join(
        most_recent_statistic_ids,
        table.id == most_recent_statistic_ids.c.max_id,
    )

    return execute(query)


def _sorted_statistics_to_dict(
    hass: HomeAssistant,
    session: Session,
    stats: Iterable[Row],
    statistic_ids: list[str] | None,
    _metadata: dict[str, tuple[int, StatisticMetaData]],
    convert_units: bool,
    table: type[Statistics | StatisticsShortTerm],
    start_time: datetime | None,
    start_time_as_datetime: bool = False,
) -> dict[str, list[dict]]:
    """Convert SQL results into JSON friendly data structure."""
    result: dict = defaultdict(list)
    units = hass.config.units
    metadata = dict(_metadata.values())
    need_stat_at_start_time: set[int] = set()
    stats_at_start_time = {}

    def no_conversion(val: Any, _: Any) -> float | None:
        """Return x."""
        return val  # type: ignore[no-any-return]

    # Set all statistic IDs to empty lists in result set to maintain the order
    if statistic_ids is not None:
        for stat_id in statistic_ids:
            result[stat_id] = []

    # Identify metadata IDs for which no data was available at the requested start time
    for meta_id, group in groupby(stats, lambda stat: stat.metadata_id):  # type: ignore[no-any-return]
        first_start_time = process_timestamp(next(group).start)
        if start_time and first_start_time > start_time:
            need_stat_at_start_time.add(meta_id)

    # Fetch last known statistics for the needed metadata IDs
    if need_stat_at_start_time:
        assert start_time  # Can not be None if need_stat_at_start_time is not empty
        tmp = _statistics_at_time(session, need_stat_at_start_time, table, start_time)
        if tmp:
            for stat in tmp:
                stats_at_start_time[stat.metadata_id] = (stat,)

    # Append all statistic entries, and optionally do unit conversion
    for meta_id, group in groupby(stats, lambda stat: stat.metadata_id):  # type: ignore[no-any-return]
        unit = metadata[meta_id]["unit_of_measurement"]
        statistic_id = metadata[meta_id]["statistic_id"]
        convert: Callable[[Any, Any], float | None]
        if convert_units:
            convert = STATISTIC_UNIT_TO_DISPLAY_UNIT_CONVERSIONS.get(unit, lambda x, units: x)  # type: ignore[arg-type,no-any-return]
        else:
            convert = no_conversion
        ent_results = result[meta_id]
        for db_state in chain(stats_at_start_time.get(meta_id, ()), group):
            start = process_timestamp(db_state.start)
            end = start + table.duration
            ent_results.append(
                {
                    "statistic_id": statistic_id,
                    "start": start if start_time_as_datetime else start.isoformat(),
                    "end": end.isoformat(),
                    "mean": convert(db_state.mean, units),
                    "min": convert(db_state.min, units),
                    "max": convert(db_state.max, units),
                    "last_reset": process_timestamp_to_utc_isoformat(
                        db_state.last_reset
                    ),
                    "state": convert(db_state.state, units),
                    "sum": convert(db_state.sum, units),
                }
            )

    # Filter out the empty lists if some states had 0 results.
    return {metadata[key]["statistic_id"]: val for key, val in result.items() if val}


def validate_statistics(hass: HomeAssistant) -> dict[str, list[ValidationIssue]]:
    """Validate statistics."""
    platform_validation: dict[str, list[ValidationIssue]] = {}
    for platform in hass.data[DOMAIN].values():
        if not hasattr(platform, "validate_statistics"):
            continue
        platform_validation.update(platform.validate_statistics(hass))
    return platform_validation


def _statistics_exists(
    session: Session,
    table: type[Statistics | StatisticsShortTerm],
    metadata_id: int,
    start: datetime,
) -> int | None:
    """Return id if a statistics entry already exists."""
    result = (
        session.query(table.id)
        .filter((table.metadata_id == metadata_id) & (table.start == start))
        .first()
    )
    return result["id"] if result else None


@callback
def async_add_external_statistics(
    hass: HomeAssistant,
    metadata: StatisticMetaData,
    statistics: Iterable[StatisticData],
) -> None:
    """Add hourly statistics from an external source.

    This inserts an add_external_statistics job in the recorder's queue.
    """
    # The statistic_id has same limitations as an entity_id, but with a ':' as separator
    if not valid_statistic_id(metadata["statistic_id"]):
        raise HomeAssistantError("Invalid statistic_id")

    # The source must not be empty and must be aligned with the statistic_id
    domain, _object_id = split_statistic_id(metadata["statistic_id"])
    if not metadata["source"] or metadata["source"] != domain:
        raise HomeAssistantError("Invalid source")

    for statistic in statistics:
        start = statistic["start"]
        if start.tzinfo is None or start.tzinfo.utcoffset(start) is None:
            raise HomeAssistantError("Naive timestamp")
        if start.minute != 0 or start.second != 0 or start.microsecond != 0:
            raise HomeAssistantError("Invalid timestamp")
        statistic["start"] = dt_util.as_utc(start)

    # Insert job in recorder's queue
    hass.data[DATA_INSTANCE].async_external_statistics(metadata, statistics)


def _filter_unique_constraint_integrity_error(
    instance: Recorder,
) -> Callable[[Exception], bool]:
    def _filter_unique_constraint_integrity_error(err: Exception) -> bool:
        """Handle unique constraint integrity errors."""
        if not isinstance(err, StatementError):
            return False

        assert instance.engine is not None
        dialect_name = instance.engine.dialect.name

        ignore = False
        if (
            dialect_name == SupportedDialect.SQLITE
            and "UNIQUE constraint failed" in str(err)
        ):
            ignore = True
        if (
            dialect_name == SupportedDialect.POSTGRESQL
            and hasattr(err.orig, "pgcode")
            and err.orig.pgcode == "23505"
        ):
            ignore = True
        if dialect_name == "mysql" and hasattr(err.orig, "args"):
            with contextlib.suppress(TypeError):
                if err.orig.args[0] == 1062:
                    ignore = True

        if ignore:
            _LOGGER.warning(
                "Blocked attempt to insert duplicated statistic rows, please report at %s",
                "https://github.com/home-assistant/core/issues?q=is%3Aopen+is%3Aissue+label%3A%22integration%3A+recorder%22",
                exc_info=err,
            )

        return ignore

    return _filter_unique_constraint_integrity_error


@retryable_database_job("statistics")
def add_external_statistics(
    instance: Recorder,
    metadata: StatisticMetaData,
    statistics: Iterable[StatisticData],
) -> bool:
    """Process an add_external_statistics job."""

    with session_scope(
        session=instance.get_session(),
        exception_filter=_filter_unique_constraint_integrity_error(instance),
    ) as session:
        old_metadata_dict = get_metadata_with_session(
            instance.hass, session, statistic_ids=[metadata["statistic_id"]]
        )
        metadata_id = _update_or_add_metadata(session, metadata, old_metadata_dict)
        for stat in statistics:
            if stat_id := _statistics_exists(
                session, Statistics, metadata_id, stat["start"]
            ):
                _update_statistics(session, Statistics, stat_id, stat)
            else:
                _insert_statistics(session, Statistics, metadata_id, stat)

    return True


@retryable_database_job("adjust_statistics")
def adjust_statistics(
    instance: Recorder,
    statistic_id: str,
    start_time: datetime,
    sum_adjustment: float,
) -> bool:
    """Process an add_statistics job."""

    with session_scope(session=instance.get_session()) as session:
        metadata = get_metadata_with_session(
            instance.hass, session, statistic_ids=(statistic_id,)
        )
        if statistic_id not in metadata:
            return True

        units = instance.hass.config.units
        statistic_unit = metadata[statistic_id][1]["unit_of_measurement"]
        display_unit = _configured_unit(statistic_unit, units)
        convert = DISPLAY_UNIT_TO_STATISTIC_UNIT_CONVERSIONS.get(display_unit, lambda x, units: x)  # type: ignore[arg-type]
        sum_adjustment = convert(sum_adjustment, units)

        _adjust_sum_statistics(
            session,
            StatisticsShortTerm,
            metadata[statistic_id][0],
            start_time,
            sum_adjustment,
        )

        _adjust_sum_statistics(
            session,
            Statistics,
            metadata[statistic_id][0],
            start_time.replace(minute=0),
            sum_adjustment,
        )

    return True
