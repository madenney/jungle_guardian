import json
import logging
import os
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import discord
from discord import app_commands
from discord.ext import commands

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

DUP_WINDOW_SECONDS = 2.0
DUP_COUNT = 3
BASE_TIMEOUT_SECONDS = 1
MAX_MESSAGE_LENGTH = 1900
SMALL_MESSAGE_MAX_BYTES = 5
SMALL_MESSAGE_WINDOW_SECONDS = 60
SMALL_MESSAGE_COUNT = 5
LINK_IMAGE_WINDOW_SECONDS = 60
LINK_IMAGE_COUNT = 3
LINK_REGEX = re.compile(r"(https?://\S+|www\.\S+)", re.IGNORECASE)
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tiff"}

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)

_user_state = {}
_small_message_state = {}
_link_image_state = {}
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
    chunks = _chunk_text(text)
    if not chunks:
        await interaction.response.send_message("No content to display.", ephemeral=ephemeral)
        return

    await interaction.response.send_message(chunks[0], ephemeral=ephemeral)
    for chunk in chunks[1:]:
        await interaction.followup.send(chunk, ephemeral=ephemeral)


def _message_has_link_or_image(message: discord.Message) -> bool:
    if LINK_REGEX.search(message.content):
        return True

    for attachment in message.attachments:
        content_type = getattr(attachment, "content_type", None)
        if content_type and content_type.startswith("image/"):
            return True
        filename = (attachment.filename or "").lower()
        _, ext = os.path.splitext(filename)
        if ext in IMAGE_EXTENSIONS:
            return True

    return False


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
    timeout_seconds = _get_timeout_seconds(message.guild.id, message.author.id)
    now_utc = datetime.now(timezone.utc)
    until = now_utc + timedelta(seconds=timeout_seconds)
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
        timeout_seconds,
        rule.get("id"),
        now_utc.isoformat(),
    )
    _save_score(SCORE_FILE, _score)
    logger.info(
        "Timed out %s for %s seconds (rule %s)",
        message.author,
        timeout_seconds,
        rule.get("id"),
    )

    response = _render_response(rule, message, timeout_seconds)
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
}

_synced_commands = False


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
        lines.append(f"Total: {timeouts} {label}. Next timeout: {next_timeout}s.")

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
        lines.append(f"{name} — {timeouts} {label}, next: {next_timeout}s")

    await _send_long_response(interaction, "\n".join(lines))


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


if __name__ == "__main__":
    bot.run(TOKEN)
