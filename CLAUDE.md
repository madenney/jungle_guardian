# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A Discord moderation bot for the Jungle Melee server. Two mostly independent concerns:

- **`bot.py`** — watches messages, matches them against rules in `rules.json`, applies escalating timeouts persisted in `score.json`.
- **`census.py`** — a daily member-count snapshot into `census.json`. Fully independent of the other two.
- **`voice_gate.py`** — a loopback-only HTTP endpoint that lets junglemelee.com mute/unmute members in the commentary voice channel based on whether they have the stream fullscreen. Same process, same gateway connection, separate module.
- **`loopback.py`** — the one HTTP server the site calls into (`127.0.0.1:8787`). Owns the bind, the shared bearer check and `/health`; the routes belong to the concerns, and `bot.py` composes them.
- **`kotj.py`** — a read-only client for junglemelee.com's machine API, backing `/entrants`. The other direction of the voice-gate relationship: there the site calls in, here the bot calls out.

## Commands

```bash
source .venv/bin/activate && python bot.py   # run locally (reads .env)
./setup.sh                                    # install deps + write/enable systemd unit "jungle-guardian"
./start.sh / ./stop.sh / ./restart.sh         # systemctl wrappers (need sudo)
./scripts/reset_score.sh                      # wipe score.json (interactive confirm)
```

There is no test suite, linter, or build step. Verification is manual: run the bot against a test guild and watch `guardian.log`.

Editing `rules.json` requires a bot restart — rules are loaded once at import time.

## Architecture

**Rule dispatch.** `on_message` iterates `_rules` (sorted by `number`), looks up a handler in `_RULE_HANDLERS` by the rule's `id`, and stops at the first handler that returns `True`. Only one rule fires per message; lowest `number` wins.

**Adding a rule requires two edits that must agree:** an entry in `rules.json` and a handler registered in `_RULE_HANDLERS` under the same `id`. A JSON rule with no matching handler is silently skipped (logged as a warning); a handler with no JSON entry never runs. Handlers are `async (rule, message, now) -> bool` where `True` means "triggered, stop evaluating".

**Two kinds of state, deliberately separated:**
- *Sliding windows* (`_user_state`, `_small_message_state`, `_link_image_state`, `_everyone_state`) are module-level dicts keyed by `(guild_id, user_id)`, holding `time.monotonic()` timestamps. In-memory only — a restart forgives all in-flight spam streaks. Do not persist these; monotonic values are meaningless across processes.
- *Escalation* lives in `score.json` (`{guild_id: {user_id: {...}}}`), written atomically via a `.tmp` sibling + `replace()`. Each user's `next_timeout_seconds` starts at 1 and doubles per violation, forever.

**Timeouts longer than Discord's 28-day cap** are applied in chunks. `_apply_timeout` applies `min(true_timeout, MAX_TIMEOUT_SECONDS)` and stashes the remainder in the score entry as `pending_extension`. The `check_timeout_extensions` task loop (every 5 min) re-applies the next chunk as each one nears expiry. The chain is cancelled two ways: `on_member_update` fires when a mod lifts a timeout, and the loop itself detects a timeout that vanished well before its expected expiry. Both paths matter — the event can be missed if the bot was offline.

**Slash commands are also rate-limited.** The `on_interaction` listener formats the invoked command into a string and feeds it through `_handle_duplicate_messages` using a `SimpleNamespace` that duck-types a `discord.Message` (`content`, `guild`, `author`, `channel`). Any handler reachable from that path must not touch other `Message` attributes.

**Response templates** in `rules.json` are rendered with `format_map` over a `_SafeFormatDict`, so an unknown `{placeholder}` renders literally instead of raising. Available keys are listed in README.md.

## Voice gate

`voice_gate.py` serves `/voice/state` and `/voice/clear` on `127.0.0.1` only (see README for the payload contract). Both it and the site backend run on the same EC2 box — **never change the bind to `0.0.0.0`**, that's the whole security model.

Two facts about Discord drive nearly every design decision in this module:

1. **Server mute only applies to a member currently connected to voice.** Mute edits for anyone else are rejected, so intent has to be held and applied later.
2. **Server mute is a guild-wide flag on the member, not per-channel.** A mute applied for the commentary channel follows the member into every other voice channel in the server.

Hence two pieces of in-memory state, neither persisted:
- `_desired` — `(guild_id, user_id) -> (channel_id, muted)`. Applied by `on_voice_state_update` when the member joins *that* channel. `queued_not_in_voice` is a success path, not an error.
- `_bot_muted` — who this process muted. Serves two jobs: don't undo a moderator's manual mute, and recognise our own mutes that have leaked out of scope.

Commentary channels are created per event and deleted afterwards, so `channel_id` comes in the request body; `VOICE_GATE_CHANNEL_ID` is only a fallback default. Deleting a channel disconnects everyone, and fact (1) means lingering mutes cannot be cleared at that moment — `handle_join` clears them on next connect instead. The site is expected to `POST /voice/clear` *before* deleting the channel; the join-time cleanup is the backstop for when it doesn't.

The manual-mute rule has a deliberate asymmetry worth understanding before changing it: an unmute *over HTTP* respects a mute the bot didn't apply, but an unmute *on join* overrides it. Server mutes persist across sessions, so a mute observed at join time is far more likely to be the bot's own leftover state than a fresh mod action — and a silently stuck viewer is the worse failure.

Fail-open is what makes in-memory state acceptable: `shutdown()` clears everything, `start()` resets the fixed channel if one is configured, and `handle_join` catches whatever slipped through. The SIGTERM handler in `bot.py` exists solely so `systemctl stop` routes through `Guardian.close()` rather than killing the process with people still muted.

## Tournament site link

`kotj.py` reads one route on junglemelee.com's Node app — `GET /api/machine/entrants?event=current` — over loopback, since both processes share a box. It never writes.

The interesting parts are all about **not lying to the channel**:

- **Every failure gets its own message.** A command that could not distinguish "no event scheduled" from "the site is down" would announce an empty bracket on a night the box was unreachable. `KotjError` carries a `friendly` line per case, including a 404 on the *route* (a kotj build older than the endpoint) as distinct from a 404 meaning `no_current_event`.
- **The config check happens before `defer()`.** A public defer forces every later reply to be public too, so a missing-token notice would land in the channel; checking first lets it stay ephemeral. Same reasoning as `/bans`.
- **Missing token disables the command** rather than erroring, mirroring how an empty `VOICE_GATE_TOKEN` disables the gate.

Two things kotj does on its side that matter here, and would be easy to undo by accident: its query selects only `tag`/`seed`/`checked_in`, so Slippi codes never reach this process at all; and it skips its stream-tool liveness stamp on this route, so `/entrants` cannot make the ops dashboard report the Melee Stream Tool as connected. The heavier `/api/machine/state` has neither property — don't switch to it for convenience.

**The signup announcement is the inbound half** of the same relationship: kotj POSTs `/kotj/signup` when someone enters and Guardian posts a line in the channel. Three things about it are load-bearing:

- **It answers 202 before sending the Discord message.** kotj calls this on the path where a player just clicked *enter*; making that request wait on the Discord API would put Guardian's latency inside somebody's signup. kotj is fire-and-forget on its side for the same reason.
- **Dedupe is by `(event id, entrant id)`**, which is why the push carries an entrant id even though the roster endpoint deliberately has none. A retry kotj never saw the answer to is normal, not an error.
- **It is served off `loopback.py`, not `voice_gate`.** The gate's own `_read_body` enforces its `_enabled` flag, so anything reusing it inherits the mute gate's on/off switch — `/unmuteall` would silently stop signup announcements, and nothing would connect those two facts. `loopback.read_json` checks auth and nothing else. There is a test for exactly this.

Entrants are printed in the order kotj returns them (seed, then entry time). Re-sorting here would mean two orderings of the same bracket.

## Census

`census.py` writes one entry per guild per UTC day. The only thing worth knowing before touching it: **Discord exposes no history for member counts**, so an unrecorded day is unrecoverable. That is why `start()` records immediately instead of waiting for the next `tasks.loop(time=...)` firing, and why the day key makes a repeat call a no-op rather than a duplicate — the catch-up has to be safe to run on every reconnect.

The human/bot split comes from the member cache and is omitted rather than zeroed if that cache is empty. There is no presence count; the presences intent is not requested and adding it would be a privileged-intent change.

## Operational notes

- The systemd unit runs as root, so `score.json`, `census.json` and `guardian.log` end up root-owned. Running `python bot.py` as your user afterward will fail to write the score file — stop the service first, or `chown` those files.
- `DISCORD_GUILD_ID` in `.env` scopes slash-command sync to one guild (near-instant) instead of globally (slow to propagate). Commands previously synced globally stay live in every server until explicitly removed, so `on_ready` purges the global list once a guild is configured — via `bot.http`, not `tree.clear_commands(guild=None)`, which would leave `copy_global_to()` nothing to copy on the next connect and empty the guild instead.
- **Three things want to write to `guardian.log`**, and only one may: the systemd unit redirects both standard streams into it with `append:`, `logging.StreamHandler()` writes to stderr, and `bot.run()` installs a stderr handler of its own. Unchecked that is every line three times over. `bot.run(TOKEN, log_handler=None)` drops discord.py's, and the console handler is skipped when stderr is found to be the log file already (inode comparison — systemd sets `JOURNAL_STREAM` only for the journal, not for a file). The systemd redirect is kept so tracebacks that never reach `logging` still land in the file. Adding a handler here needs the same care.
- The bot needs the Message Content intent enabled in the Discord developer portal and the Moderate Members permission in the guild.
