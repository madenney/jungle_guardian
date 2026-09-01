"""Read-only client for the junglemelee.com (kotj) machine API.

kotj runs the weekly tournament and serves junglemelee.com from the same EC2
box as this bot -- a Node process on 127.0.0.1:3003 behind nginx. It exposes an
`/api/machine/*` surface for its non-human callers, gated by a bearer token
minted out of band (`node scripts/mint-machine-token.mjs` in the kotj repo).

This module reads one endpoint and writes nothing:

    GET /api/machine/entrants?event=current

kotj answers with the event, the entrant count and the tags. That route is
display-only by construction -- its query selects just tag/seed/checked_in, so
Slippi connect codes and user ids never leave kotj's database layer and are
never held here. The heavier /api/machine/state would have carried both.

The request is deliberately NOT the same shape as the stream tool's polling:
kotj skips its "MST connected" liveness stamp for this route, so a Discord
command can never make the ops dashboard claim the stream tool is running.

Leaving KOTJ_MACHINE_TOKEN unset disables the feature, the same way an empty
VOICE_GATE_TOKEN disables the voice gate.

The module also serves the OTHER direction -- kotj pushing signups in, so the
bot can announce them -- at the bottom of this file. Both halves of the site
relationship live here; the socket they arrive on belongs to loopback.py.
"""
import asyncio
import logging
import os

import aiohttp
import discord
from aiohttp import web

import loopback

logger = logging.getLogger("guardian.kotj")

# A single POST from kotj should not be able to make the bot post an unbounded
# wall of names, however the site's own batching behaves.
MAX_SIGNUP_BATCH = 100


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, ""))
    except (TypeError, ValueError):
        return default

# Loopback by default: kotj listens on 127.0.0.1 and both processes share a box,
# so the request never touches the network. Overridable only for local testing
# against a dev server on another port.
API_URL = os.getenv("KOTJ_API_URL", "http://127.0.0.1:3003").rstrip("/")
TOKEN = os.getenv("KOTJ_MACHINE_TOKEN", "")

try:
    TIMEOUT_SECONDS = float(os.getenv("KOTJ_TIMEOUT_SECONDS", ""))
except (TypeError, ValueError):
    # Short on purpose. This is a loopback call behind a slash command, and
    # Discord gives a deferred interaction 15 minutes but users about three
    # seconds of patience.
    TIMEOUT_SECONDS = 5.0


class KotjError(Exception):
    """A failed read, carrying a line that is safe to post in a channel.

    Every failure mode gets a distinct message. A command that cannot tell
    "nothing scheduled" from "site is down" would report an empty bracket on a
    night the box was simply unreachable.
    """

    def __init__(self, kind: str, friendly: str) -> None:
        super().__init__(kind)
        self.kind = kind
        self.friendly = friendly


def enabled() -> bool:
    return bool(TOKEN)


async def entrants(event: str = "current") -> dict:
    """The entrant list for `event` ("current", "arena", or a numeric id)."""
    if not TOKEN:
        raise KotjError("not_configured", "The tournament site link is not set up.")

    url = f"{API_URL}/api/machine/entrants"
    timeout = aiohttp.ClientTimeout(total=TIMEOUT_SECONDS)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(
                url,
                params={"event": event},
                headers={"Authorization": f"Bearer {TOKEN}"},
            ) as resp:
                # kotj answers JSON on every path it owns. An HTML body means
                # nginx or Next handled it instead -- usually a build that
                # predates this endpoint.
                try:
                    payload = await resp.json(content_type=None)
                except (ValueError, aiohttp.ContentTypeError):
                    payload = None

                if resp.status == 200 and isinstance(payload, dict):
                    return payload

                error = payload.get("error") if isinstance(payload, dict) else None
                raise KotjError(error or f"http_{resp.status}", _explain(resp.status, error))
    except asyncio.TimeoutError:
        logger.warning("kotj timed out after %.1fs", TIMEOUT_SECONDS)
        raise KotjError("timeout", "The tournament site did not answer in time.")
    except aiohttp.ClientError as exc:
        logger.warning("kotj unreachable at %s: %s", API_URL, exc)
        raise KotjError("unreachable", "Could not reach the tournament site.")


def _explain(status: int, error: str | None) -> str:
    if error == "no_current_event":
        return "There is no event running right now."
    if error == "no_arena_event":
        return "There is no arena event right now."
    if error == "not_found":
        return "No event with that id."
    if status in (401, 403):
        # A token that kotj rejects is an operator problem, not a user one, so
        # say which knob it is rather than "something went wrong".
        return "The tournament site rejected this bot's credentials."
    if status == 404:
        # Distinct from `no_current_event` above: the route itself is missing,
        # which means the running kotj build predates the endpoint.
        return "The tournament site does not have the entrants endpoint yet."
    return f"The tournament site returned an error ({status})."


# --- Inbound: signup announcements ------------------------------------------
# The other direction. kotj POSTs here the moment somebody enters the bracket,
# and Guardian says so in Discord. Served off the shared loopback socket (see
# loopback.py), so the site authenticates with the token it already holds.

ANNOUNCE_CHANNEL_ID = _int_env("KOTJ_ANNOUNCE_CHANNEL_ID", 0)

# (event_id, entrant_id) already announced. kotj is fire-and-forget on its
# side, so a retry after a timeout it never saw the answer to is a normal
# event, not an error -- this is what stops that becoming a second message.
# Reset when the event changes, which is the only unbounded direction.
_announced: set[tuple[int, int]] = set()
_announced_event: int | None = None

# Background posts, held so the event loop cannot garbage-collect a task
# mid-flight (asyncio only keeps weak references to running tasks).
_tasks: set = set()


def announces() -> bool:
    return bool(ANNOUNCE_CHANNEL_ID)


def _fresh(event_id: int, entrants: list[dict]) -> list[dict]:
    """Entrants not already announced for this event, in arrival order."""
    global _announced_event
    if event_id != _announced_event:
        _announced.clear()
        _announced_event = event_id

    fresh = []
    for entrant in entrants:
        tag = str(entrant.get("tag") or "").strip()
        if not tag:
            continue
        raw = entrant.get("id")
        try:
            key = (event_id, int(raw))
        except (TypeError, ValueError):
            # No usable id: announce it rather than drop it, and accept that a
            # retry could duplicate. A missing name is worse than a repeat.
            fresh.append({"tag": tag})
            continue
        if key in _announced:
            continue
        _announced.add(key)
        fresh.append({"tag": tag})
    return fresh


def render_signups(entrants: list[dict], event_name: str | None = None) -> str:
    """One line for one person, a multi-line post for a burst."""
    names = [discord.utils.escape_markdown(e["tag"]) for e in entrants]

    if len(names) == 1:
        # The event name is printed exactly as kotj sends it. Re-casing it here
        # would mean the bracket is called one thing on the site and another in
        # Discord on the same night.
        if event_name:
            return f"**{names[0]}** signed up for {discord.utils.escape_markdown(event_name)}"
        return f"**{names[0]}** signed up"

    # Hyphens escaped for the same reason /entrants escapes them: Discord turns
    # a line opening with "- " into its own indented bullet list.
    lines = [f"**{len(names)} new sign-ups just now:**"]
    lines.extend(f"\\- {name}" for name in names)
    return "\n".join(lines)


async def _post_signups(bot, entrants: list[dict], event_name: str | None = None) -> None:
    channel = bot.get_channel(ANNOUNCE_CHANNEL_ID)
    if channel is None:
        try:
            channel = await bot.fetch_channel(ANNOUNCE_CHANNEL_ID)
        except Exception:
            logger.exception("Signup channel %s is unreachable", ANNOUNCE_CHANNEL_ID)
            return
    try:
        await channel.send(render_signups(entrants, event_name))
    except Exception:
        logger.exception("Could not post %s signup(s)", len(entrants))


async def _handle_signup(request):
    body, error = await loopback.read_json(request)
    if error is not None:
        return error

    event = body.get("event") or {}
    try:
        event_id = int(event.get("id"))
    except (TypeError, ValueError):
        return web.json_response({"ok": False, "error": "event_id_required"}, status=400)

    entrants = body.get("entrants")
    if not isinstance(entrants, list) or not entrants:
        return web.json_response({"ok": False, "error": "entrants_required"}, status=400)
    entrants = [e for e in entrants if isinstance(e, dict)][:MAX_SIGNUP_BATCH]

    fresh = _fresh(event_id, entrants)
    if not fresh:
        # Every one was a duplicate. That is a success: kotj retried and we
        # already said it.
        return web.json_response({"ok": True, "announced": 0, "duplicates": len(entrants)})

    if not announces():
        logger.info("Signup announcements off; dropped %s (set KOTJ_ANNOUNCE_CHANNEL_ID)", len(fresh))
        return web.json_response({"ok": True, "announced": 0, "error": "no_channel"})

    # Answered before the message is sent, on purpose. kotj calls this on the
    # path where somebody just clicked "enter", and must never wait on the
    # Discord API to finish.
    name = event.get("name")
    task = asyncio.create_task(
        _post_signups(request.app["bot"], fresh, str(name) if name else None)
    )
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return web.json_response({"ok": True, "announced": len(fresh)}, status=202)


def routes() -> list:
    return [web.post("/kotj/signup", _handle_signup)]
