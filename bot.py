import json
import logging
import os
import re
import signal
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import discord
from discord import app_commands
from discord.ext import commands, tasks

import census
import voice_gate

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None

if load_dotenv:
    load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN is not set")

LOG_LEVEL = os.getenv("LOG_LEVEL", "info").upper()
LOG_FILE = Path(__file__).with_name("guardian.log")
LOG_TO_STDOUT = os.getenv("LOG_STDOUT", "true").lower() in {"1", "true", "yes", "on"}
handlers = [logging.FileHandler(LOG_FILE, encoding="utf-8")]
if LOG_TO_STDOUT:
    handlers.append(logging.StreamHandler())

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=handlers,
)
logger = logging.getLogger("guardian")

DUP_WINDOW_SECONDS = 60.0
DUP_COUNT = 3
BASE_TIMEOUT_SECONDS = 1
MAX_TIMEOUT_SECONDS = 2_419_200  # Discord's 28-day limit
EXTENSION_CHECK_INTERVAL_MINUTES = 5
MAX_MESSAGE_LENGTH = 1900
SMALL_MESSAGE_MAX_BYTES = 5
SMALL_MESSAGE_WINDOW_SECONDS = 60
SMALL_MESSAGE_COUNT = 5
LINK_IMAGE_WINDOW_SECONDS = 60
LINK_IMAGE_COUNT = 7
EVERYONE_WINDOW_SECONDS = 60 * 60 * 24
EVERYONE_COUNT = 2
# Discord has no read-only view of the ban list: reading it needs the same
# Ban Members permission that banning does.
_BAN_PERMISSION_HINT = (
    "I need the **Ban Members** permission to read the ban list. "
    "Grant it to the Jungle Guardian role in Server Settings → Roles."
)
LINK_REGEX = re.compile(r"(https?://\S+|www\.\S+)", re.IGNORECASE)
EVERYONE_REGEX = re.compile(r"@everyone\b", re.IGNORECASE)
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tiff"}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".webm", ".mkv", ".avi", ".mpeg", ".mpg", ".m4v"}
AUDIO_EXTENSIONS = {".mp3", ".wav", ".ogg", ".flac", ".aac", ".m4a", ".opus", ".oga"}
MEDIA_EXTENSIONS = IMAGE_EXTENSIONS | VIDEO_EXTENSIONS | AUDIO_EXTENSIONS

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

class Guardian(commands.Bot):
    async def close(self) -> None:
        census.stop()
        await voice_gate.shutdown(self)
        await super().close()


bot = Guardian(command_prefix="!", intents=intents)

_user_state = {}
_small_message_state = {}
_link_image_state = {}
_everyone_state = {}
_rules = []
_score = {}


class _SafeFormatDict(dict):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def _load_rules(path: Path) -> list[dict]:
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        logger.error("Rules file not found: %s", path)
        return []
    except OSError as exc:
        logger.error("Failed to read rules file %s: %s", path, exc)
        return []

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.error("Invalid JSON in %s: %s", path, exc)
        return []

    if not isinstance(data, list):
        logger.error("Rules file must contain a JSON array: %s", path)
        return []

    rules = []
    for rule in data:
        if not isinstance(rule, dict):
            logger.warning("Skipping non-object rule: %s", rule)
            continue
        missing = [key for key in ("id", "name", "number", "description", "response") if key not in rule]
        if missing:
            logger.warning("Skipping rule missing %s: %s", ", ".join(missing), rule)
            continue
        try:
            rule["number"] = int(rule["number"])
        except (TypeError, ValueError):
            logger.warning("Skipping rule with invalid number: %s", rule)
            continue
        rules.append(rule)

    rules.sort(key=lambda entry: entry["number"])
    return rules


def _load_score(path: Path) -> dict:
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        try:
            path.write_text("{}", encoding="utf-8")
        except OSError as exc:
            logger.error("Failed to create score file %s: %s", path, exc)
        return {}
    except OSError as exc:
        logger.error("Failed to read score file %s: %s", path, exc)
        return {}

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.error("Invalid JSON in %s: %s", path, exc)
        return {}

    if not isinstance(data, dict):
        logger.error("Score file must contain a JSON object: %s", path)
        return {}

    return data


def _save_score(path: Path, score: dict) -> None:
    try:
        tmp_path = path.with_suffix(".tmp")
        tmp_path.write_text(json.dumps(score, indent=2, sort_keys=True), encoding="utf-8")
        tmp_path.replace(path)
    except OSError as exc:
        logger.error("Failed to write score file %s: %s", path, exc)


def _get_score_entry(guild_id: int, user_id: int) -> dict:
    guild = _score.setdefault(str(guild_id), {})
    entry = guild.get(str(user_id))
    if not isinstance(entry, dict):
        entry = {
            "user_id": user_id,
            "timeouts": 0,
            "next_timeout_seconds": BASE_TIMEOUT_SECONDS,
            "violations": [],
        }
        guild[str(user_id)] = entry
    entry.setdefault("user_id", user_id)
    if not isinstance(entry.get("violations"), list):
        entry["violations"] = []
    return entry


def _get_timeout_seconds(guild_id: int, user_id: int) -> int:
    entry = _get_score_entry(guild_id, user_id)
    next_value = entry.get("next_timeout_seconds", BASE_TIMEOUT_SECONDS)
    try:
        next_value = int(next_value)
    except (TypeError, ValueError):
        next_value = BASE_TIMEOUT_SECONDS
    return max(1, next_value)


def _record_timeout(
    guild_id: int,
    user_id: int,
    timeout_seconds: int,
    rule_id: str | None,
    timestamp: str,
) -> None:
    entry = _get_score_entry(guild_id, user_id)
    try:
        timeouts = int(entry.get("timeouts", 0))
    except (TypeError, ValueError):
        timeouts = 0
    entry["timeouts"] = timeouts + 1
    entry["next_timeout_seconds"] = max(1, int(timeout_seconds) * 2)
    entry["violations"].append(
        {
            "user_id": user_id,
            "rule_id": rule_id or "",
            "timestamp": timestamp,
        }
    )


def _set_pending_extension(
    guild_id: int,
    user_id: int,
    remaining_seconds: int,
    chunk_expires_iso: str,
    reason: str,
) -> None:
    entry = _get_score_entry(guild_id, user_id)
    entry["pending_extension"] = {
        "remaining_seconds": remaining_seconds,
        "current_chunk_expires": chunk_expires_iso,
        "reason": reason,
    }


def _clear_pending_extension(guild_id: int, user_id: int) -> None:
    entry = _get_score_entry(guild_id, user_id)
    entry.pop("pending_extension", None)


def _format_duration(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds} second{'s' if seconds != 1 else ''}"
    if seconds < 3600:
        minutes = seconds // 60
        return f"{minutes} minute{'s' if minutes != 1 else ''}"
    if seconds < 86400:
        hours = seconds // 3600
        return f"{hours} hour{'s' if hours != 1 else ''}"
    days = seconds // 86400
    return f"{days} day{'s' if days != 1 else ''}"


def _render_response(rule: dict, message: discord.Message, timeout_seconds: int) -> str | None:
    template = rule.get("response", "")
    if not template:
        return None
    unit = "second" if timeout_seconds == 1 else "seconds"
    values = _SafeFormatDict(
        {
            "user_mention": message.author.mention,
            "user_name": message.author.name,
            "user_id": message.author.id,
            "channel_mention": message.channel.mention,
            "rule_name": rule.get("name", ""),
            "rule_number": rule.get("number", ""),
            "rule_id": rule.get("id", ""),
            "rule_description": rule.get("description", ""),
            "timeout_seconds": timeout_seconds,
            "timeout_unit": unit,
            "guild_name": message.guild.name,
        }
    )
    return template.format_map(values)


def _chunk_text(text: str, limit: int = MAX_MESSAGE_LENGTH) -> list[str]:
    if len(text) <= limit:
        return [text]

    chunks = []
    current = ""
    for line in text.splitlines():
        if len(line) > limit:
            if current:
                chunks.append(current.rstrip())
                current = ""
            for i in range(0, len(line), limit):
                chunks.append(line[i : i + limit])
            continue

        if len(current) + len(line) + 1 > limit:
            chunks.append(current.rstrip())
            current = ""
        current += line + "\n"

    if current:
        chunks.append(current.rstrip())

    return [chunk for chunk in chunks if chunk]


async def _send_long_response(
    interaction: discord.Interaction, text: str, *, ephemeral: bool = False
) -> None:
    chunks = _chunk_text(text) or ["No content to display."]

    # A deferred interaction has already been acknowledged, so the first chunk
    # has to go through followup like the rest of them.
    if interaction.response.is_done():
        for chunk in chunks:
            await interaction.followup.send(chunk, ephemeral=ephemeral)
        return

    await interaction.response.send_message(chunks[0], ephemeral=ephemeral)
    for chunk in chunks[1:]:
        await interaction.followup.send(chunk, ephemeral=ephemeral)


def _message_has_link_or_image(message: discord.Message) -> bool:
    if LINK_REGEX.search(message.content):
        return True

    for attachment in message.attachments:
        content_type = getattr(attachment, "content_type", None)
        if content_type and content_type.startswith(("image/", "video/", "audio/")):
            return True
        filename = (attachment.filename or "").lower()
        _, ext = os.path.splitext(filename)
        if ext in MEDIA_EXTENSIONS:
            return True

    return False


def _message_has_everyone(message: discord.Message) -> bool:
    if not message.content:
        return False
    if not EVERYONE_REGEX.search(message.content):
        return False
    return bool(getattr(message, "mention_everyone", True))


def _format_user_label(guild: discord.Guild, user_id: str, client: discord.Client) -> str:
    try:
        user_id_int = int(user_id)
    except (TypeError, ValueError):
        return f"User {user_id}"

    member = guild.get_member(user_id_int)
    if member:
        return member.display_name

    user = client.get_user(user_id_int)
    if user:
        return user.name

    return f"User {user_id}"


def _format_violation_timestamp(timestamp: str) -> str:
    try:
        parsed = datetime.fromisoformat(timestamp)
    except ValueError:
        return timestamp

    if parsed.tzinfo is not None:
        parsed = parsed.astimezone()
    return parsed.strftime("%m/%d/%Y %H:%M:%S")


async def _apply_timeout(rule: dict, message: discord.Message) -> bool:
    true_timeout = _get_timeout_seconds(message.guild.id, message.author.id)
    applied_seconds = min(true_timeout, MAX_TIMEOUT_SECONDS)
    remaining_seconds = true_timeout - applied_seconds
    now_utc = datetime.now(timezone.utc)
    until = now_utc + timedelta(seconds=applied_seconds)
    reason = rule.get("description") or "Rule violation"

    try:
        await message.author.edit(
            timed_out_until=until,
            reason=reason,
        )
    except discord.Forbidden:
        logger.warning("Missing permissions to timeout %s", message.author)
        return True
    except discord.HTTPException as exc:
        logger.warning("Failed to timeout %s: %s", message.author, exc)
        return True

    _record_timeout(
        message.guild.id,
        message.author.id,
        true_timeout,
        rule.get("id"),
        now_utc.isoformat(),
    )

    if remaining_seconds > 0:
        _set_pending_extension(
            message.guild.id,
            message.author.id,
            remaining_seconds,
            until.isoformat(),
            reason,
        )
        logger.info(
            "Timeout for %s exceeds 28 days: applied %s, %s remaining",
            message.author,
            _format_duration(applied_seconds),
            _format_duration(remaining_seconds),
        )
    else:
        _clear_pending_extension(message.guild.id, message.author.id)

    _save_score(SCORE_FILE, _score)
    logger.info(
        "Timed out %s for %s (rule %s)",
        message.author,
        _format_duration(true_timeout),
        rule.get("id"),
    )

    response = _render_response(rule, message, true_timeout)
    if response:
        try:
            await message.channel.send(response)
        except discord.Forbidden:
            logger.warning("Missing permissions to announce timeout in %s", message.channel)
        except discord.HTTPException as exc:
            logger.warning("Failed to announce timeout in %s: %s", message.channel, exc)

    return True


async def _handle_duplicate_messages(rule: dict, message: discord.Message, now: float) -> bool:
    if not message.content:
        return False

    content = message.content
    key = (message.guild.id, message.author.id)
    state = _user_state.get(key)

    if state and content == state["last_content"]:
        if (now - state["first_ts"]) <= DUP_WINDOW_SECONDS:
            count = state["count"] + 1
            first_ts = state["first_ts"]
        else:
            count = 1
            first_ts = now
    else:
        count = 1
        first_ts = now

    _user_state[key] = {
        "last_content": content,
        "first_ts": first_ts,
        "count": count,
    }

    if count < DUP_COUNT:
        return False

    _user_state[key] = {"last_content": "", "first_ts": now, "count": 0}
    return await _apply_timeout(rule, message)


async def _handle_small_messages(rule: dict, message: discord.Message, now: float) -> bool:
    if not message.content:
        return False

    content_length = len(message.content.encode("utf-8"))
    key = (message.guild.id, message.author.id)
    timestamps = _small_message_state.get(key, [])
    timestamps = [ts for ts in timestamps if (now - ts) <= SMALL_MESSAGE_WINDOW_SECONDS]

    if content_length < SMALL_MESSAGE_MAX_BYTES:
        timestamps.append(now)

    if not timestamps:
        _small_message_state.pop(key, None)
        return False

    _small_message_state[key] = timestamps

    if len(timestamps) < SMALL_MESSAGE_COUNT:
        return False

    _small_message_state[key] = []
    return await _apply_timeout(rule, message)


async def _handle_link_or_image_messages(rule: dict, message: discord.Message, now: float) -> bool:
    has_link_or_image = _message_has_link_or_image(message)
    key = (message.guild.id, message.author.id)
    timestamps = _link_image_state.get(key, [])
    timestamps = [ts for ts in timestamps if (now - ts) <= LINK_IMAGE_WINDOW_SECONDS]

    if has_link_or_image:
        timestamps.append(now)

    if not timestamps:
        _link_image_state.pop(key, None)
        return False

    _link_image_state[key] = timestamps

    if len(timestamps) < LINK_IMAGE_COUNT:
        return False

    _link_image_state[key] = []
    return await _apply_timeout(rule, message)


async def _handle_everyone_mentions(rule: dict, message: discord.Message, now: float) -> bool:
    has_everyone = _message_has_everyone(message)
    key = (message.guild.id, message.author.id)
    timestamps = _everyone_state.get(key, [])
    timestamps = [ts for ts in timestamps if (now - ts) <= EVERYONE_WINDOW_SECONDS]

    if has_everyone:
        timestamps.append(now)

    if not timestamps:
        _everyone_state.pop(key, None)
        return False

    _everyone_state[key] = timestamps

    if not has_everyone:
        return False

    if len(timestamps) < EVERYONE_COUNT:
        return False

    return await _apply_timeout(rule, message)

RULES_FILE = Path(__file__).with_name("rules.json")
_rules = _load_rules(RULES_FILE)
logger.info("Loaded %s rules from %s", len(_rules), RULES_FILE)
_rules_by_id = {rule.get("id"): rule for rule in _rules if isinstance(rule, dict)}

SCORE_FILE = Path(__file__).with_name("score.json")
_score = _load_score(SCORE_FILE)
score_entries = sum(len(entries) for entries in _score.values() if isinstance(entries, dict))
logger.info("Loaded %s score entries from %s", score_entries, SCORE_FILE)

_RULE_HANDLERS = {
    "duplicate_messages": _handle_duplicate_messages,
    "small_messages": _handle_small_messages,
    "link_or_image_messages": _handle_link_or_image_messages,
    "everyone_mentions": _handle_everyone_mentions,
}

_synced_commands = False


@tasks.loop(minutes=EXTENSION_CHECK_INTERVAL_MINUTES)
async def check_timeout_extensions() -> None:
    now_utc = datetime.now(timezone.utc)
    changed = False

    for guild_id_str, guild_entries in _score.items():
        if not isinstance(guild_entries, dict):
            continue
        for user_id_str, entry in guild_entries.items():
            if not isinstance(entry, dict):
                continue
            pending = entry.get("pending_extension")
            if not pending or not isinstance(pending, dict):
                continue

            try:
                expires = datetime.fromisoformat(pending["current_chunk_expires"])
            except (KeyError, ValueError):
                entry.pop("pending_extension", None)
                changed = True
                continue

            if now_utc < expires - timedelta(seconds=60):
                continue

            remaining = pending.get("remaining_seconds", 0)
            if remaining <= 0:
                entry.pop("pending_extension", None)
                changed = True
                continue

            next_chunk = min(remaining, MAX_TIMEOUT_SECONDS)
            new_remaining = remaining - next_chunk
            guild_id = int(guild_id_str)
            user_id = int(user_id_str)

            guild = bot.get_guild(guild_id)
            if not guild:
                continue
            try:
                member = guild.get_member(user_id) or await guild.fetch_member(user_id)
            except discord.NotFound:
                entry.pop("pending_extension", None)
                changed = True
                continue
            except discord.HTTPException:
                continue

            # If timeout was removed well before expected expiry, a mod did it
            if member.timed_out_until is None or member.timed_out_until <= now_utc:
                if expires > now_utc + timedelta(minutes=EXTENSION_CHECK_INTERVAL_MINUTES + 1):
                    logger.info(
                        "Timeout for %s in guild %s removed early; cancelling extension chain",
                        user_id, guild_id,
                    )
                    entry.pop("pending_extension", None)
                    changed = True
                    continue

            until = now_utc + timedelta(seconds=next_chunk)
            reason = pending.get("reason", "Extended timeout")
            try:
                await member.edit(timed_out_until=until, reason=reason)
            except discord.Forbidden:
                logger.warning("Missing permissions to extend timeout for %s", member)
                continue
            except discord.HTTPException as exc:
                logger.warning("Failed to extend timeout for %s: %s", member, exc)
                continue

            logger.info(
                "Extended timeout for %s: applied %s, %s remaining",
                member,
                _format_duration(next_chunk),
                _format_duration(new_remaining) if new_remaining > 0 else "none",
            )

            if new_remaining > 0:
                pending["remaining_seconds"] = new_remaining
                pending["current_chunk_expires"] = until.isoformat()
            else:
                entry.pop("pending_extension", None)
            changed = True

    if changed:
        _save_score(SCORE_FILE, _score)


@check_timeout_extensions.before_loop
async def before_check_timeout_extensions() -> None:
    await bot.wait_until_ready()


def _format_command_content(interaction: discord.Interaction) -> str | None:
    command = interaction.command
    command_name = None
    if command is not None:
        command_name = getattr(command, "qualified_name", None) or getattr(command, "name", None)
    data = interaction.data if isinstance(interaction.data, dict) else {}
    if not command_name:
        command_name = data.get("name")
    if not command_name:
        return None

    parts = [f"/{command_name}"]
    options = data.get("options", [])
    if isinstance(options, list) and options:
        formatted = _format_command_options(options)
        if formatted:
            parts.append(" ".join(formatted))

    return " ".join(parts)


def _format_command_options(options: list[dict]) -> list[str]:
    parts = []
    for option in options:
        if not isinstance(option, dict):
            continue
        name = option.get("name")
        if "value" in option:
            value = option.get("value")
            parts.append(f"{name}={value}")
            continue
        nested = option.get("options")
        if isinstance(nested, list):
            if name:
                parts.append(str(name))
            parts.extend(_format_command_options(nested))
    return parts


@bot.event
async def on_ready() -> None:
    logger.info("Logged in as %s (id=%s)", bot.user, bot.user.id)
    global _synced_commands
    if _synced_commands:
        return

    guild_id = os.getenv("DISCORD_GUILD_ID")
    try:
        if guild_id:
            guild = discord.Object(id=int(guild_id))
            await bot.tree.sync(guild=guild)
            logger.info("Synced slash commands to guild %s", guild_id)
        else:
            await bot.tree.sync()
            logger.info("Synced slash commands globally")
        _synced_commands = True
    except Exception as exc:
        logger.warning("Failed to sync slash commands: %s", exc)

    if not check_timeout_extensions.is_running():
        check_timeout_extensions.start()
        logger.info("Started timeout extension background task")

    await voice_gate.start(bot)
    await census.start(bot)


@bot.event
async def on_voice_state_update(
    member: discord.Member, before: discord.VoiceState, after: discord.VoiceState
) -> None:
    if member.bot:
        return

    before_id = before.channel.id if before.channel else None
    after_id = after.channel.id if after.channel else None
    if after_id is None or before_id == after_id:
        return

    try:
        await voice_gate.handle_join(member)
    except Exception:
        logger.exception("Voice gate join handler failed for %s", member)


@bot.event
async def on_guild_channel_delete(channel: discord.abc.GuildChannel) -> None:
    if isinstance(channel, (discord.VoiceChannel, discord.StageChannel)):
        voice_gate.handle_channel_delete(channel)


@bot.event
async def on_member_update(before: discord.Member, after: discord.Member) -> None:
    if before.timed_out_until and not after.timed_out_until:
        guild_id = after.guild.id
        user_id = after.id
        entry = _score.get(str(guild_id), {}).get(str(user_id))
        if isinstance(entry, dict) and entry.get("pending_extension"):
            logger.info(
                "Timeout manually removed for %s in guild %s; cancelling extension chain",
                user_id, guild_id,
            )
            entry.pop("pending_extension", None)
            _save_score(SCORE_FILE, _score)


@bot.listen("on_interaction")
async def on_interaction_listener(interaction: discord.Interaction) -> None:
    if interaction.type is not discord.InteractionType.application_command:
        return
    if interaction.guild is None or interaction.user.bot:
        return

    rule = _rules_by_id.get("duplicate_messages")
    if not rule:
        return

    content = _format_command_content(interaction)
    if not content:
        return

    message_like = SimpleNamespace(
        content=content,
        guild=interaction.guild,
        author=interaction.user,
        channel=interaction.channel,
    )
    await _handle_duplicate_messages(rule, message_like, time.monotonic())


@bot.tree.command(name="rules", description="List the rules enforced by the bot.")
async def rules_command(interaction: discord.Interaction) -> None:
    if not _rules:
        await interaction.response.send_message("No rules are configured.", ephemeral=True)
        return

    lines = [
        "Rules:",
        "",
        "The following actions will result in a timeout:",
    ]
    for rule in _rules:
        number = rule.get("number", "?")
        description = rule.get("description", "")
        lines.append(f"{number}. {description}")

    lines.append("")
    lines.append("Each user starts with a 1 second timeout that doubles with each violation.")
    await _send_long_response(interaction, "\n".join(lines), ephemeral=False)


@bot.tree.command(name="score", description="Show timeouts for a user or server.")
@app_commands.describe(user="User to check (optional)")
async def score_command(
    interaction: discord.Interaction, user: discord.Member | None = None
) -> None:
    if interaction.guild is None:
        await interaction.response.send_message("Use this command in a server.", ephemeral=True)
        return

    guild_scores = _score.get(str(interaction.guild.id), {})
    if user is not None:
        target = user
        entry = guild_scores.get(str(target.id), {})
        if not isinstance(entry, dict):
            entry = {}

        try:
            timeouts = int(entry.get("timeouts", 0))
        except (TypeError, ValueError):
            timeouts = 0

        try:
            next_timeout = int(entry.get("next_timeout_seconds", BASE_TIMEOUT_SECONDS))
        except (TypeError, ValueError):
            next_timeout = BASE_TIMEOUT_SECONDS

        display_name = target.display_name
        label = "timeout" if timeouts == 1 else "timeouts"
        header = f"Violations for {display_name}"

        if timeouts == 0:
            await interaction.response.send_message(
                f"{header}\nNo violations yet. Next timeout: {next_timeout}s."
            )
            return

        violations = entry.get("violations", [])
        if not isinstance(violations, list):
            violations = []

        lines = [header]
        for violation in violations:
            if not isinstance(violation, dict):
                continue
            raw_timestamp = violation.get("timestamp", "Unknown time")
            timestamp = _format_violation_timestamp(raw_timestamp)
            rule_id = violation.get("rule_id", "")
            rule = _rules_by_id.get(rule_id, {})
            description = rule.get("description") or f"Rule {rule_id or 'unknown'}"
            lines.append(f"- {timestamp} — {description}")

        if len(lines) == 1:
            lines.append("No violation history recorded yet.")
        lines.append(f"Total: {timeouts} {label}. Next timeout: {_format_duration(next_timeout)}.")

        pending = entry.get("pending_extension")
        if isinstance(pending, dict) and pending.get("remaining_seconds", 0) > 0:
            remaining = pending["remaining_seconds"]
            chunk_expires = pending.get("current_chunk_expires", "unknown")
            lines.append(
                f"Extended timeout active: {_format_duration(remaining)} remaining "
                f"(current chunk expires {chunk_expires})"
            )

        await _send_long_response(interaction, "\n".join(lines))
        return

    entries = []
    for user_id, entry in guild_scores.items():
        if not isinstance(entry, dict):
            continue
        try:
            timeouts = int(entry.get("timeouts", 0))
        except (TypeError, ValueError):
            timeouts = 0
        if timeouts <= 0:
            continue
        try:
            next_timeout = int(entry.get("next_timeout_seconds", BASE_TIMEOUT_SECONDS))
        except (TypeError, ValueError):
            next_timeout = BASE_TIMEOUT_SECONDS
        entries.append((next_timeout, timeouts, user_id))

    if not entries:
        await interaction.response.send_message("No timeouts recorded yet.")
        return

    entries.sort(key=lambda item: (item[0], item[1], item[2]), reverse=True)
    lines = ["Timeouts"]
    for next_timeout, timeouts, user_id in entries:
        label = "timeout" if timeouts == 1 else "timeouts"
        name = _format_user_label(interaction.guild, user_id, interaction.client)
        user_entry = guild_scores.get(user_id, {})
        pending = user_entry.get("pending_extension") if isinstance(user_entry, dict) else None
        ext_marker = " [extended]" if isinstance(pending, dict) and pending.get("remaining_seconds", 0) > 0 else ""
        lines.append(f"{name} — {timeouts} {label}, next: {_format_duration(next_timeout)}{ext_marker}")

    await _send_long_response(interaction, "\n".join(lines))


def _is_ban_moderator(interaction: discord.Interaction) -> bool:
    perms = getattr(interaction.user, "guild_permissions", None)
    return bool(perms and (perms.ban_members or perms.manage_guild))


def _format_ban(entry: discord.BanEntry) -> str:
    reason = (entry.reason or "").strip() or "no reason recorded"
    return f"`{entry.user.id}`  **{entry.user}** — {reason}"


def _count_bans(count: int) -> str:
    return f"{count:,} ban" if count == 1 else f"{count:,} bans"


@bot.tree.command(name="bans", description="Count the bans on this server, or look one up.")
@app_commands.describe(
    search="A user ID, or part of a name",
    full="Also print every banned user, not just the count",
)
async def bans_command(
    interaction: discord.Interaction, search: str | None = None, full: bool = False
) -> None:
    if interaction.guild is None:
        await interaction.response.send_message("Use this command in a server.", ephemeral=True)
        return
    if not _is_ban_moderator(interaction):
        await interaction.response.send_message(
            "You need Ban Members or Manage Server to use this.", ephemeral=True
        )
        return

    # Always ephemeral: who is banned and why is moderator business, and this
    # is easy to run in a public channel by accident.
    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild
    search = (search or "").strip()

    # A banned user is by definition not in the guild, so Discord's user picker
    # will not offer them. An ID goes straight to the single-ban endpoint;
    # anything else is matched against the names in the full list.
    if search.isdigit():
        try:
            entry = await guild.fetch_ban(discord.Object(id=int(search)))
        except discord.NotFound:
            await interaction.followup.send(f"`{search}` is not banned.", ephemeral=True)
            return
        except discord.Forbidden:
            await interaction.followup.send(_BAN_PERMISSION_HINT, ephemeral=True)
            return
        except discord.HTTPException as exc:
            await interaction.followup.send(f"Could not check that ID: {exc}", ephemeral=True)
            return
        await interaction.followup.send(_format_ban(entry), ephemeral=True)
        return

    try:
        entries = [entry async for entry in guild.bans(limit=None)]
    except discord.Forbidden:
        await interaction.followup.send(_BAN_PERMISSION_HINT, ephemeral=True)
        return
    except discord.HTTPException as exc:
        await interaction.followup.send(f"Could not read the ban list: {exc}", ephemeral=True)
        return

    if search:
        needle = search.casefold()
        entries = [e for e in entries if needle in str(e.user).casefold()]
        if not entries:
            await interaction.followup.send(f"No ban matches '{search}'.", ephemeral=True)
            return
        header = f"**{_count_bans(len(entries))}** matching '{search}'"
    elif not entries:
        await interaction.followup.send(f"No one is banned in **{guild.name}**.", ephemeral=True)
        return
    else:
        header = f"**{guild.name}** — {_count_bans(len(entries))}"

    # The count is the answer; the names are opt-in. Dumping a long list every
    # time buries the number, and a ban list is not something to spray around
    # unless it was asked for.
    if not full and not search:
        await interaction.followup.send(
            f"{header}\nUse `/bans full:True` to see who.", ephemeral=True
        )
        return

    entries.sort(key=lambda entry: str(entry.user).casefold())
    lines = [header, ""]
    lines.extend(_format_ban(entry) for entry in entries)
    await _send_long_response(interaction, "\n".join(lines), ephemeral=True)


@bot.tree.command(name="members", description="Show the member count and how it has moved.")
async def members_command(interaction: discord.Interaction) -> None:
    if interaction.guild is None:
        await interaction.response.send_message("Use this command in a server.", ephemeral=True)
        return

    guild = interaction.guild
    total = guild.member_count or len(guild.members)
    lines = [f"**{guild.name}** — {total:,} members"]

    if guild.members:
        bots = sum(1 for member in guild.members if member.bot)
        lines.append(f"{total - bots:,} humans · {bots:,} bots")

    history = census.series(guild.id)
    changes = []
    seen = set()
    for days_back, label in ((1, "1 day"), (7, "7 days"), (30, "30 days")):
        change = census.compare(guild.id, days_back)
        # Early on, every window falls back to the same oldest entry; showing
        # that one delta three times under three labels would be noise.
        if change is None or change["since"] in seen:
            continue
        seen.add(change["since"])
        # A fallback to an older entry means the span is not the one asked
        # for, so report what it really covers instead of quietly relabelling.
        span = change["span"]
        name = label if span == days_back else f"{span} days"
        changes.append(f"`{name:>8}`  {change['delta']:+,}")

    if changes:
        lines.append("")
        lines.extend(changes)

    if history:
        day_count = len(history)
        noun = "day" if day_count == 1 else "days"
        lines.append(f"\n{day_count} {noun} recorded, since {history[0][0]}")
    else:
        lines.append("\nNo history recorded yet.")

    await interaction.response.send_message("\n".join(lines))


def _is_voice_moderator(interaction: discord.Interaction) -> bool:
    perms = getattr(interaction.user, "guild_permissions", None)
    return bool(perms and (perms.mute_members or perms.manage_guild))


@bot.tree.command(name="unmuteall", description="Unmute everyone in the gated voice channel.")
async def unmuteall_command(interaction: discord.Interaction) -> None:
    if interaction.guild is None:
        await interaction.response.send_message("Use this command in a server.", ephemeral=True)
        return
    if not _is_voice_moderator(interaction):
        await interaction.response.send_message(
            "You need Mute Members or Manage Server to use this.", ephemeral=True
        )
        return

    await interaction.response.defer()
    cleared = await voice_gate.clear_all(bot, reason=f"/unmuteall by {interaction.user}")
    voice_gate.set_enabled(False)
    logger.info("%s ran /unmuteall; unmuted %s member(s)", interaction.user, cleared)
    await interaction.followup.send(
        f"Unmuted {cleared} member(s) and disabled the voice gate. "
        f"Re-enable with `/mutegate enabled:True`."
    )


@bot.tree.command(name="mutegate", description="Enable or disable site-driven voice muting.")
@app_commands.describe(enabled="True to let the site mute again, False to ignore it")
async def mutegate_command(interaction: discord.Interaction, enabled: bool | None = None) -> None:
    if interaction.guild is None:
        await interaction.response.send_message("Use this command in a server.", ephemeral=True)
        return

    if enabled is None:
        state = "enabled" if voice_gate.is_enabled() else "disabled"
        configured = "configured" if voice_gate.is_configured() else "not configured"
        await interaction.response.send_message(
            f"Voice gate is {state} ({configured}), tracking {voice_gate.tracked_count()} user(s)."
        )
        return

    if not _is_voice_moderator(interaction):
        await interaction.response.send_message(
            "You need Mute Members or Manage Server to use this.", ephemeral=True
        )
        return

    voice_gate.set_enabled(enabled)
    logger.info("%s set voice gate enabled=%s", interaction.user, enabled)
    await interaction.response.send_message(
        f"Voice gate {'enabled' if enabled else 'disabled'}."
    )


@bot.event
async def on_message(message: discord.Message) -> None:
    if message.author.bot or message.guild is None:
        return

    if not message.content and not message.attachments and not message.embeds:
        await bot.process_commands(message)
        return

    now = time.monotonic()
    for rule in _rules:
        rule_id = rule.get("id")
        handler = _RULE_HANDLERS.get(rule_id)
        if not handler:
            logger.warning("No handler for rule id %s", rule_id)
            continue
        try:
            triggered = await handler(rule, message, now)
        except Exception:
            logger.exception("Rule handler failed for %s", rule_id)
            triggered = False
        if triggered:
            break

    await bot.process_commands(message)


def _handle_sigterm(signum, frame) -> None:
    # systemd stops the service with SIGTERM, which would otherwise kill the
    # process outright and leave members server-muted. Route it into the same
    # path as Ctrl-C so Guardian.close() gets to unmute everyone first.
    raise KeyboardInterrupt


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, _handle_sigterm)
    bot.run(TOKEN)
