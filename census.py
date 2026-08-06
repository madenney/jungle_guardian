"""Daily membership census.

Records one snapshot per guild per day into `census.json`. Discord exposes no
history for member counts, so a day that goes unrecorded is gone for good --
which is why `start()` takes a snapshot immediately instead of waiting for the
next midnight, and why the day key makes a repeat run a no-op rather than a
duplicate.

Kept out of `score.json` on purpose: that file is moderation state keyed by
user, this is a time series keyed by date. Resetting one should never touch
the other.
"""

import json
import logging
from datetime import date, datetime, time as dtime, timedelta, timezone
from pathlib import Path

import discord
from discord.ext import tasks

logger = logging.getLogger("guardian.census")

CENSUS_FILE = Path(__file__).with_name("census.json")

# Five past midnight rather than midnight: the snapshot then sits
# unambiguously inside the day it is labelled with, and it is not competing
# with every other scheduled job on the hour.
CENSUS_TIME = dtime(hour=0, minute=5, tzinfo=timezone.utc)

_history: dict = {}
_bot: discord.Client | None = None


def _load(path: Path) -> dict:
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError as exc:
        logger.error("Failed to read census file %s: %s", path, exc)
        return {}

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.error("Invalid JSON in %s: %s", path, exc)
        return {}

    if not isinstance(data, dict):
        logger.error("Census file must contain a JSON object: %s", path)
        return {}

    return data


def _save(path: Path, history: dict) -> None:
    try:
        tmp_path = path.with_suffix(".tmp")
        tmp_path.write_text(json.dumps(history, indent=2, sort_keys=True), encoding="utf-8")
        tmp_path.replace(path)
    except OSError as exc:
        logger.error("Failed to write census file %s: %s", path, exc)


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _snapshot(guild: discord.Guild) -> dict:
    """One day's numbers for one guild.

    `member_count` is the authoritative total. The human/bot split comes from
    the member cache, which the members intent keeps filled -- if that cache is
    somehow empty the split would be a fiction, so it is left out rather than
    recorded as zero. There is no online count: that needs the presences
    intent, which this bot does not ask for.
    """
    total = guild.member_count or len(guild.members)
    entry = {
        "total": total,
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    if guild.members:
        entry["bots"] = sum(1 for member in guild.members if member.bot)
        entry["humans"] = total - entry["bots"]
    return entry


def record(bot: discord.Client, *, force: bool = False) -> list[tuple[discord.Guild, dict]]:
    """Snapshot every guild the bot is in, unless today is already recorded."""
    day = _today()
    written = []

    for guild in bot.guilds:
        guild_entry = _history.setdefault(str(guild.id), {})
        guild_entry["name"] = guild.name
        days = guild_entry.setdefault("days", {})
        if day in days and not force:
            continue
        days[day] = _snapshot(guild)
        written.append((guild, days[day]))

    if written:
        _save(CENSUS_FILE, _history)
        for guild, entry in written:
            logger.info("Census %s: %s has %s members", day, guild.name, entry["total"])

    return written


@tasks.loop(time=CENSUS_TIME)
async def daily_census() -> None:
    if _bot is not None:
        record(_bot)


def series(guild_id: int) -> list[tuple[str, dict]]:
    """Every recorded day for a guild, oldest first."""
    days = _history.get(str(guild_id), {}).get("days", {})
    return sorted(days.items()) if isinstance(days, dict) else []


def compare(guild_id: int, days_back: int) -> dict | None:
    """Change since roughly `days_back` days ago.

    Falls back to the newest entry at or before that date, so a restart-shaped
    hole in the history still yields a usable number. The span actually
    covered is returned rather than the one asked for, because reporting a
    9-day change as a 7-day change would be a quiet lie.
    """
    entries = series(guild_id)
    if len(entries) < 2:
        return None

    latest_day, latest = entries[-1]
    target = date.fromisoformat(latest_day) - timedelta(days=days_back)
    prior = [(day, entry) for day, entry in entries[:-1] if date.fromisoformat(day) <= target]
    if not prior:
        return None

    day, entry = prior[-1]
    return {
        "delta": latest["total"] - entry["total"],
        "span": (date.fromisoformat(latest_day) - date.fromisoformat(day)).days,
        "since": day,
        "then": entry["total"],
    }


async def start(bot: discord.Client) -> None:
    global _bot, _history
    _bot = bot
    _history = _load(CENSUS_FILE)
    record(bot)
    if not daily_census.is_running():
        daily_census.start()
        logger.info("Started daily census task (%s UTC)", CENSUS_TIME.strftime("%H:%M"))


def stop() -> None:
    if daily_census.is_running():
        daily_census.cancel()
