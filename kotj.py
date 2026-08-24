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
"""
import asyncio
import logging
import os

import aiohttp

logger = logging.getLogger("guardian.kotj")

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
