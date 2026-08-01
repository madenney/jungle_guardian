# Jungle Guardian (Discord Bot)

Moderation-focused Discord bot with configurable rules and escalating timeouts.
Rules live in `rules.json`, and the timeout duration starts at 1 second and
doubles after each violation.

## Requirements
- Python 3.10+

## Setup (dev)
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Create `.env` with your token:
```ini
DISCORD_TOKEN=your_token_here
LOG_LEVEL=info
LOG_STDOUT=true
DISCORD_GUILD_ID=
```

Run:
```bash
python bot.py
```

## Setup (server)
There is a setup script that installs dependencies, writes a systemd unit,
and starts the bot.

```bash
chmod +x setup.sh
./setup.sh
```

If `DISCORD_TOKEN` is not set in `.env`, the script will exit after creating
the service file. Update `.env` and run:
```bash
sudo systemctl enable --now jungle-guardian
```

Helper scripts:
```bash
./start.sh
./stop.sh
./restart.sh
./scripts/reset_score.sh
```

Logs are written to `guardian.log` in the project directory.

## Rules
Rules are defined in `rules.json` and evaluated for every message. Each rule
needs:
- `number`: order/index for display and evaluation
- `id`: matches a rule handler in `bot.py`
- `name`: human-friendly label
- `description`: short explanation (used as the moderation reason)
- `response`: message posted in-channel when the rule fires

Available `response` placeholders:
- `{user_mention}`, `{user_name}`, `{user_id}`
- `{channel_mention}`, `{guild_name}`
- `{rule_name}`, `{rule_id}`, `{rule_number}`, `{rule_description}`
- `{timeout_seconds}`, `{timeout_unit}`

After editing `rules.json`, restart the bot to load the changes.

Only one rule triggers per message; the lowest rule number wins.

Default rules in `rules.json`:
- Rule 1: three identical messages in a row within one minute
- Rule 2: five messages smaller than five bytes within one minute
- Rule 3: seven messages containing links or media attachments within one minute
- Rule 4: sending more than one @everyone in 24 hours

## Score Tracking
Timeout durations scale per user and are stored in `score.json`. Each user
starts at 1 second, and the timeout doubles after every violation. The file
is updated whenever a timeout succeeds. Each entry also records a violations
history with user ID, rule ID, and timestamp.

## Slash Commands
- `/rules` lists the configured rules
- `/score` shows timeouts for all users (or a specified user). When a user
  is provided, it includes their violation history with timestamps.

Slash command updates can take a while to appear globally. For faster updates
in a single server, set `DISCORD_GUILD_ID` in `.env` and restart the bot to
sync commands to that guild.

## Voice Gate (junglemelee.com)

Lets the site mute/unmute members in the commentary voice channel. Guardian
listens on loopback only; the site POSTs to it from the same box.

Configure in `.env`:
```ini
VOICE_GATE_TOKEN=<shared secret>
VOICE_GATE_PORT=8787
VOICE_GATE_CHANNEL_ID=<optional default voice channel id>
```
Only `VOICE_GATE_TOKEN` is required; leaving it empty disables the feature.
`VOICE_GATE_CHANNEL_ID` is just a default for a fixed channel — events that
create a fresh channel each time send `channel_id` per request instead, so no
config change or restart is needed.

```
POST http://127.0.0.1:8787/voice/state
Authorization: Bearer <VOICE_GATE_TOKEN>

{
  "channel_id": "1401234567890123456",
  "updates": [
    { "user_id": "123456789012345678", "muted": true },
    { "user_id": "987654321098765432", "muted": false }
  ],
  "reason": "not fullscreen"
}
```

Responds with one result per user:
```json
{ "ok": true, "results": [
  { "user_id": "123456789012345678", "status": "applied" }
]}
```

| status | meaning |
| --- | --- |
| `applied` | mute state changed on Discord |
| `noop` | already in the requested state |
| `queued_not_in_voice` | not in the channel; intent saved, applied when they join |
| `skipped_manual_mute` | a moderator muted them by hand; left alone |
| `unknown_member` | not in the guild |
| `error` | see `detail` (`missing_permissions`, `http_<code>`) |

`queued_not_in_voice` is a normal outcome, not a failure — Discord can only
mute a member who is currently connected to voice.

### Ending an event

```
POST http://127.0.0.1:8787/voice/clear
{ "channel_id": "1401234567890123456", "reason": "event over" }
```

Unmutes everyone in the channel and drops its intent. Omit `channel_id` to
clear everything the gate is tracking.

**Call this before deleting an event channel, not after.** Discord's server
mute is a guild-wide flag on the member, not a per-channel one, and it cannot
be changed while the member is disconnected from voice. Deleting the channel
disconnects everyone, so a mute left in place at that moment cannot be cleared
until they reconnect somewhere. Guardian does recover on its own — the next
time such a member joins any voice channel, the stale mute is removed — but
they would be muted for the first moments of that call.

`GET /health` (no auth, loopback only) reports readiness, whether the gate is
enabled, and how many users are tracked.

Guardian requires the **Mute Members** permission, and its role must sit above
anyone it mutes. It can never mute the server owner.

Failsafes:
- Everyone in the gated channel is unmuted on startup and on shutdown, so a
  crash or restart never leaves someone silently muted.
- `/unmuteall` unmutes everyone and disables the gate until it is turned back
  on with `/mutegate enabled:True`.
- `/mutegate` with no argument reports current status.

## Get a Discord Bot Token
1. Go to https://discord.com/developers/applications
2. Click **New Application** and give it a name.
3. In the left sidebar, click **Bot**.
4. Click **Add Bot** (confirm).
5. Under **Token**, click **Reset Token** and copy it.
6. Paste the token into `.env` as `DISCORD_TOKEN`.

Also enable **Message Content Intent** in the Bot settings so the bot
can read messages, and make sure the bot has **Moderate Members**
permission in your server so timeouts work.
