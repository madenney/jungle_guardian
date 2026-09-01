import logging
import os

import discord
from aiohttp import web

import loopback

logger = logging.getLogger("guardian.voice_gate")

MAX_BATCH = 100
DEFAULT_REASON = "junglemelee voice gate"


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, ""))
    except (TypeError, ValueError):
        return default

# Optional fallback for a fixed commentary channel. Events that spin up a fresh
# channel each time should send channel_id in the request instead.
CHANNEL_ID = _int_env("VOICE_GATE_CHANNEL_ID", 0)

# Desired state pushed by the site: (guild_id, user_id) -> (channel_id, muted).
# Discord can only mute a member who is currently connected to voice, so intent
# for anyone else is held here and applied when they join that channel.
_desired: dict[tuple[int, int], tuple[int, bool]] = {}

# Members this process muted. Used to avoid undoing a moderator's manual mute,
# and to recognise our own stale mutes that need clearing.
_bot_muted: set[tuple[int, int]] = set()

_enabled = True


def is_configured() -> bool:
    # The socket and its token belong to loopback now; the gate is configured
    # exactly when the server it is served from is.
    return loopback.is_configured()


def is_enabled() -> bool:
    return _enabled


def set_enabled(value: bool) -> None:
    global _enabled
    _enabled = value
    logger.info("Voice gate %s", "enabled" if value else "disabled")


def tracked_count() -> int:
    return len(_desired)


def _resolve_channel(
    bot: discord.Client, channel_id: int
) -> discord.VoiceChannel | discord.StageChannel | None:
    channel = bot.get_channel(channel_id)
    if isinstance(channel, (discord.VoiceChannel, discord.StageChannel)):
        return channel
    return None


async def _set_mute(member: discord.Member, muted: bool, reason: str | None) -> dict:
    try:
        await member.edit(mute=muted, reason=reason or DEFAULT_REASON)
    except discord.Forbidden:
        logger.warning("Missing permissions to set mute=%s on %s", muted, member)
        return {"status": "error", "detail": "missing_permissions"}
    except discord.HTTPException as exc:
        logger.warning("Failed to set mute=%s on %s: %s", muted, member, exc)
        return {"status": "error", "detail": f"http_{exc.status}"}

    key = (member.guild.id, member.id)
    if muted:
        _bot_muted.add(key)
    else:
        _bot_muted.discard(key)
    return {"status": "applied"}


async def _sync_member(
    member: discord.Member,
    channel_id: int,
    muted: bool,
    reason: str | None,
    *,
    on_join: bool = False,
) -> dict:
    voice = member.voice
    if voice is None or voice.channel is None or voice.channel.id != channel_id:
        return {"status": "queued_not_in_voice"}

    key = (member.guild.id, member.id)
    if bool(voice.mute) == muted:
        return {"status": "noop"}

    # A moderator muted them by hand during this session, so leave it alone. On
    # join we do override: server mutes persist across sessions, so a mute seen
    # at join time is far more likely to be our own leftover state than a fresh
    # moderator action, and a silently stuck viewer is the worse failure.
    if voice.mute and not muted and key not in _bot_muted and not on_join:
        return {"status": "skipped_manual_mute"}

    return await _set_mute(member, muted, reason)


async def apply_updates(
    bot: discord.Client, channel_id: int, updates: list, reason: str | None = None
) -> list[dict] | None:
    channel = _resolve_channel(bot, channel_id)
    if channel is None:
        return None
    guild = channel.guild

    results = []
    for entry in updates:
        if not isinstance(entry, dict):
            results.append({"user_id": None, "status": "error", "detail": "not_an_object"})
            continue

        raw_id = entry.get("user_id")
        try:
            user_id = int(raw_id)
        except (TypeError, ValueError):
            results.append({"user_id": raw_id, "status": "error", "detail": "bad_user_id"})
            continue

        muted = entry.get("muted")
        if not isinstance(muted, bool):
            results.append(
                {"user_id": str(user_id), "status": "error", "detail": "muted_must_be_bool"}
            )
            continue

        member = guild.get_member(user_id)
        if member is None:
            try:
                member = await guild.fetch_member(user_id)
            except discord.NotFound:
                results.append({"user_id": str(user_id), "status": "unknown_member"})
                continue
            except discord.HTTPException as exc:
                results.append(
                    {"user_id": str(user_id), "status": "error", "detail": f"http_{exc.status}"}
                )
                continue

        _desired[(guild.id, user_id)] = (channel_id, muted)
        outcome = await _sync_member(member, channel_id, muted, reason)
        results.append({"user_id": str(user_id), **outcome})

    return results


async def handle_join(member: discord.Member) -> None:
    """Called when a member joins any voice channel."""
    voice = member.voice
    if voice is None or voice.channel is None:
        return

    key = (member.guild.id, member.id)
    entry = _desired.get(key)

    if entry is not None and entry[0] == voice.channel.id:
        outcome = await _sync_member(member, entry[0], entry[1], "voice gate rejoin", on_join=True)
        logger.info(
            "Applied pending state to %s on join (muted=%s): %s",
            member, entry[1], outcome["status"],
        )
        return

    # They joined somewhere the gate isn't watching while still carrying a mute
    # this process applied. Server mute is guild-wide, so that mute is leaking
    # outside the channel it was meant for -- clear it. This is what rescues
    # people when the commentary channel is deleted before they were unmuted.
    if key in _bot_muted and voice.mute:
        outcome = await _set_mute(member, False, "voice gate stale mute cleanup")
        logger.info(
            "Cleared stale gate mute on %s after joining %s: %s",
            member, voice.channel.name, outcome["status"],
        )


def handle_channel_delete(channel: discord.abc.GuildChannel) -> None:
    """Drop intent for a deleted channel. Members disconnected by the delete
    cannot be unmuted (Discord rejects mute edits for anyone not in voice), so
    any lingering mute is cleared by handle_join when they next connect."""
    stale = [key for key, (chan_id, _) in _desired.items() if chan_id == channel.id]
    if not stale:
        return
    for key in stale:
        del _desired[key]
    still_muted = [key for key in stale if key in _bot_muted]
    logger.warning(
        "Gated channel %s deleted with %s tracked member(s); %s still server-muted "
        "and will be cleared on their next voice join",
        channel.id, len(stale), len(still_muted),
    )


async def clear_channel(bot: discord.Client, channel_id: int, reason: str) -> int:
    """Unmute everyone in a channel and drop its intent. Call this before
    deleting an event channel, not after."""
    for key, (chan_id, _) in list(_desired.items()):
        if chan_id == channel_id:
            del _desired[key]

    channel = _resolve_channel(bot, channel_id)
    if channel is None:
        return 0

    cleared = 0
    for member in list(channel.members):
        if member.voice is None or not member.voice.mute:
            continue
        if (await _set_mute(member, False, reason))["status"] == "applied":
            cleared += 1
    return cleared


async def clear_all(bot: discord.Client, reason: str = "voice gate cleared") -> int:
    """Panic path: unmute everyone the gate is tracking or has muted, wherever
    they currently are, including members muted by a moderator."""
    channels = {chan_id for chan_id, _ in _desired.values()}
    if CHANNEL_ID:
        channels.add(CHANNEL_ID)
    for guild_id, user_id in _bot_muted:
        guild = bot.get_guild(guild_id)
        member = guild.get_member(user_id) if guild else None
        if member and member.voice and member.voice.channel:
            channels.add(member.voice.channel.id)

    cleared = 0
    for channel_id in channels:
        cleared += await clear_channel(bot, channel_id, reason)

    _desired.clear()
    _bot_muted.clear()
    return cleared


def _channel_from_body(body: dict) -> int | None:
    raw = body.get("channel_id")
    if raw is None:
        return CHANNEL_ID or None
    try:
        return int(raw) or None
    except (TypeError, ValueError):
        return None


async def _read_body(request: web.Request) -> tuple[dict | None, web.Response | None]:
    """Auth and body parsing, plus the gate's own on/off switch.

    The `_enabled` check is what makes this different from `loopback.read_json`
    and why it stays here: `/unmuteall` turns the gate off, and only the voice
    routes should go quiet when it does.
    """
    body, error = await loopback.read_json(request)
    if error is not None:
        return None, error
    if not _enabled:
        return None, web.json_response({"ok": False, "error": "gate_disabled"}, status=503)
    return body, None


async def _handle_state(request: web.Request) -> web.Response:
    body, error = await _read_body(request)
    if error is not None:
        return error

    channel_id = _channel_from_body(body)
    if channel_id is None:
        return web.json_response({"ok": False, "error": "channel_id_required"}, status=400)

    updates = body.get("updates")
    if not isinstance(updates, list) or not updates:
        return web.json_response(
            {"ok": False, "error": "updates_must_be_a_non_empty_list"}, status=400
        )
    if len(updates) > MAX_BATCH:
        return web.json_response({"ok": False, "error": "batch_too_large"}, status=400)

    reason = body.get("reason")
    if not isinstance(reason, str):
        reason = None

    results = await apply_updates(request.app["bot"], channel_id, updates, reason)
    if results is None:
        return web.json_response({"ok": False, "error": "channel_unavailable"}, status=503)

    return web.json_response({"ok": True, "channel_id": str(channel_id), "results": results})


async def _handle_clear(request: web.Request) -> web.Response:
    body, error = await _read_body(request)
    if error is not None:
        return error

    bot = request.app["bot"]
    reason = body.get("reason")
    if not isinstance(reason, str):
        reason = "voice gate cleared"

    channel_id = _channel_from_body(body)
    if channel_id is None:
        cleared = await clear_all(bot, reason)
    else:
        cleared = await clear_channel(bot, channel_id, reason)

    logger.info("Cleared %s mute(s) via /voice/clear", cleared)
    return web.json_response({"ok": True, "cleared": cleared})


def routes() -> list:
    """The gate's own routes, for bot.py to hand to the loopback server."""
    return [
        web.post("/voice/state", _handle_state),
        web.post("/voice/clear", _handle_clear),
    ]


def health_snapshot() -> dict:
    """The gate's contribution to /health.

    Keep these key names: scripts/guardian-deploy reads them, so renaming one
    breaks `guardian-deploy health` on a box that has not been updated.
    """
    return {
        "enabled": _enabled,
        "default_channel_id": str(CHANNEL_ID) if CHANNEL_ID else None,
        "tracked": len(_desired),
        "muted": len(_bot_muted),
    }


async def start(bot: discord.Client) -> None:
    """Post-bind startup for the gate itself; loopback owns the socket."""
    if not is_configured():
        logger.warning("Voice gate disabled: set VOICE_GATE_TOKEN to enable it")
        return

    # Fail open: nobody should still be muted from a previous run. Only covers
    # the fixed channel; per-event mutes are cleared by handle_join instead.
    if CHANNEL_ID:
        cleared = await clear_channel(bot, CHANNEL_ID, "voice gate startup reset")
        if cleared:
            logger.warning("Startup reset unmuted %s member(s)", cleared)


async def shutdown(bot: discord.Client) -> None:
    # Unmute only; the socket is closed by loopback.shutdown(). This still has
    # to run before the process dies, which is what the SIGTERM handler in
    # bot.py exists for -- otherwise `systemctl stop` leaves people muted.
    if is_configured():
        try:
            cleared = await clear_all(bot, "voice gate shutting down")
            if cleared:
                logger.info("Shutdown unmuted %s member(s)", cleared)
        except Exception:
            logger.exception("Voice gate shutdown unmute failed")
