"""The loopback HTTP server junglemelee.com calls into.

Guardian and the site (kotj) share one EC2 box, so the site reaches this bot
over 127.0.0.1 instead of the network. **Never change HOST to 0.0.0.0** -- the
bind is the security model. The bearer token is a second line behind it, not
the first.

This module owns only the plumbing: the bind, the shared bearer check, and
/health. The routes belong to the concerns that serve them -- `voice_gate`
registers /voice/*, `kotj` registers /kotj/* -- and `bot.py` composes them, so
neither concern imports the other and neither owns the socket.

That split is deliberate. The voice gate's own body reader also enforces its
`_enabled` flag, so anything built on top of it inherits the mute gate's
on/off switch: `/unmuteall` would silently stop signup announcements, and the
two facts are far apart enough that nobody would connect them. `read_json`
here checks auth and nothing else.

VOICE_GATE_TOKEN is the shared secret for everything served here, not just the
voice routes. It predates the second caller and keeps that name because the
site already has it configured on the other side of the socket; renaming it
would mean a coordinated change in two repos for no gain.
"""
import hmac
import logging
import os

import discord
from aiohttp import web

logger = logging.getLogger("guardian.loopback")

HOST = "127.0.0.1"  # loopback only; never bind 0.0.0.0 on a public box


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, ""))
    except (TypeError, ValueError):
        return default


TOKEN = os.getenv("VOICE_GATE_TOKEN", "")
PORT = _int_env("VOICE_GATE_PORT", 8787)

_runner: web.AppRunner | None = None


def is_configured() -> bool:
    return bool(TOKEN)


def authorized(request: web.Request) -> bool:
    scheme, _, value = request.headers.get("Authorization", "").partition(" ")
    if scheme != "Bearer" or not value:
        return False
    return hmac.compare_digest(value, TOKEN)


async def read_json(request: web.Request) -> tuple[dict | None, web.Response | None]:
    """Auth plus a parsed object body, or the response to return instead.

    Checks the bearer and nothing else -- see the module docstring on why this
    does not consult any concern's enabled/disabled state.
    """
    if not authorized(request):
        return None, web.json_response({"ok": False, "error": "unauthorized"}, status=401)
    try:
        body = await request.json()
    except Exception:
        return None, web.json_response({"ok": False, "error": "invalid_json"}, status=400)
    if not isinstance(body, dict):
        return None, web.json_response({"ok": False, "error": "body_must_be_object"}, status=400)
    return body, None


async def start(bot: discord.Client, routes: list, health=None) -> bool:
    """Bind the server. `health` supplies the concern-specific /health fields."""
    global _runner
    if _runner is not None:
        return True
    if not is_configured():
        logger.warning("Loopback server disabled: set VOICE_GATE_TOKEN to enable it")
        return False

    async def _handle_health(request: web.Request) -> web.Response:
        client = request.app["bot"]
        body = {"ok": True, "ready": client.is_ready()}
        if health is not None:
            try:
                body.update(health())
            except Exception:
                logger.exception("health snapshot failed")
        return web.json_response(body)

    app = web.Application()
    app["bot"] = bot
    app.add_routes([*routes, web.get("/health", _handle_health)])

    runner = web.AppRunner(app)
    await runner.setup()
    try:
        await web.TCPSite(runner, HOST, PORT).start()
    except OSError as exc:
        logger.error("Loopback server failed to bind %s:%s: %s", HOST, PORT, exc)
        await runner.cleanup()
        return False

    _runner = runner
    served = ", ".join(sorted({r.path for r in routes})) or "no concern routes"
    logger.info("Loopback server listening on http://%s:%s (%s)", HOST, PORT, served)
    return True


async def shutdown() -> None:
    global _runner
    if _runner is not None:
        await _runner.cleanup()
        _runner = None
