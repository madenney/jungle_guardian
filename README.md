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

## Member Census
Once a day at 00:05 UTC the bot records how many members each server has into
`census.json`, keyed by date:

```json
{
  "1401234567890123456": {
    "name": "The Jungle",
    "days": {
      "2026-08-05": { "total": 1230, "humans": 1225, "bots": 5, "at": "..." }
    }
  }
}
```

Discord keeps no history of member counts, so this only knows what it has been
running long enough to see — there is no way to backfill. A snapshot is also
taken at startup if the day has no entry yet, which covers a bot that was down
over midnight; longer outages simply leave gaps.

`/members` reports the current count and the change over the last 1, 7 and 30
days. Where a window has no entry it falls back to the closest earlier one and
reports the span it actually covers, so `31 days` means 31 days.

There is no online/offline count — that needs the privileged presences intent,
which the bot does not request.

## Slash Commands
- `/rules` lists the configured rules
- `/score` shows timeouts for all users (or a specified user). When a user
  is provided, it includes their violation history with timestamps.
- `/members` shows the member count and its recent trend
- `/bans` reports the number of people banned from the server, and nothing
  else. Anyone can run it and the count is posted publicly in the channel —
  it is a single number that names nobody.
- `/entrants` lists who is entered in the current junglemelee.com event. Open
  to everyone and posted publicly — it is the roster for tonight's public
  tournament, already visible on the site. Needs the tournament site link
  below.

`/bans` needs the bot to have **Ban Members**. Discord offers no read-only
view of the ban list, so the permission that lets Guardian read it is the same
one that would let it ban; there is no narrower option. Guardian contains no
code that bans, unbans, or names anyone — `guild.bans()` is counted and
discarded.

A missing permission and an empty ban list are deliberately different
replies, so a reported `0` always means zero and never "could not look".

Slash command updates can take a while to appear globally. For faster updates
in a single server, set `DISCORD_GUILD_ID` in `.env` and restart the bot to
sync commands to that guild.

## Tournament site link (`/entrants`)

Reads the current event's entrant list from kotj, the Node app behind
junglemelee.com. Both run on the same EC2 box, so the request goes over
loopback and never touches the network.

```ini
KOTJ_API_URL=http://127.0.0.1:3003
KOTJ_MACHINE_TOKEN=<machine token>
KOTJ_TIMEOUT_SECONDS=5
```

Only `KOTJ_MACHINE_TOKEN` is required; leaving it empty disables `/entrants`,
which then replies privately that it is not configured rather than failing in
the channel. `KOTJ_API_URL` only needs changing to point at a dev server.

Mint the token in the **kotj** repo, not this one:

```bash
node scripts/mint-machine-token.mjs
```

It grants `/api/machine/*` and nothing else — no chat, no user identity, no
guild membership. Guardian uses exactly one route from it:

```
GET /api/machine/entrants?event=current
```

which is display-only at the database layer: the query selects just
tag/seed/checked_in, so Slippi connect codes and user ids are never sent and
never held here. kotj also skips its "stream tool connected" liveness stamp on
this route, so a Discord command cannot make the ops dashboard think the Melee
Stream Tool is running.

Every failure mode gets its own reply, so an empty bracket can never be
confused with an outage — "no event running", "site did not answer in time",
"could not reach the site", "credentials rejected", and "the site does not have
the entrants endpoint yet" (a kotj build older than the endpoint).

> Bumping `MACHINE_TOKEN_KID` in kotj's env to revoke the stream tool's token
> revokes Guardian's too — the check is global. Re-mint both.

## Signup announcements

When somebody enters the bracket, kotj POSTs to Guardian and it says so in
Discord — `**bobby** signed up`, or a multi-line post if several arrive at
once.

```ini
KOTJ_ANNOUNCE_CHANNEL_ID=<channel to post in>
```

Unset means the feature is off (logged at startup); nothing else changes.

```
POST http://127.0.0.1:8787/kotj/signup
Authorization: Bearer <VOICE_GATE_TOKEN>

{ "event": { "id": 3, "name": "KOTJ#3" },
  "entrants": [ { "id": 41, "tag": "bobby" } ] }
```

Answers **202 before the message is sent**. kotj calls this on the path where
somebody just clicked *enter*, so it must never wait on the Discord API — and
kotj treats the call as fire-and-forget, meaning Guardian being down or
restarting is invisible to whoever is signing up.

`entrants` is an array so a burst can arrive in one call, capped at 100.
Repeats are dropped by `(event id, entrant id)`, so a kotj retry cannot post a
name twice; the reply then reports `announced: 0` with a `duplicates` count,
which is a success rather than an error. The dedupe memory resets when the
event id changes.

## Voice Gate (junglemelee.com)

Lets the site mute/unmute members in the commentary voice channel. Guardian
listens on loopback only; the site POSTs to it from the same box.

`VOICE_GATE_TOKEN` and `VOICE_GATE_PORT` configure the whole loopback server,
not just these routes — the signup endpoint above is served from the same
socket with the same secret. They keep those names because the site already
has them configured.

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

## Emoji converter (`tools/emojify.py`)

A local dev tool. It takes images, gifs and video clips and produces files
that Discord will accept as custom emoji — resized to 128px, re-encoded, and
squeezed under the 256 KB cap.

```bash
sudo apt install ffmpeg gifsicle        # the only dependencies
python3 tools/emojify.py emoji_src/ -o emoji_out/
```

No Python packages are needed, and nothing is added to `requirements.txt` —
the bot host has no image toolchain and does not need one. The one exception
is **animated WebP**, which ffmpeg cannot demux; install Pillow
(`pip install Pillow`) if you save gifs off Tenor or Discord, since those
arrive as `.webp`. Everything else works without it.

```
  ok    :big_noisy_gif:  gif 120f  128x128  2.1M -> 217K
  ok    :smooth_gradient:  gif 75f  128x128  5.7M -> 93K  [degraded x1]
  FAIL  %.png  no usable emoji name in the filename; rename it
  CLASH big_noisy_gif__.png  -> :big_noisy_gif: already taken by Big Noisy GIF!!.gif
```

Output filenames are the emoji name, sanitised from the source filename to
the `[a-z0-9_]{2,32}` Discord allows. Existing outputs are skipped unless
`--force` is given, so rerunning over a growing folder only does new work.

### Naming

Downloads are called `tenor.gif` and `ezgif-4-a3f9c0.gif`, so the derived
name is usually useless. `names.txt` overrides it without renaming files:

```ini
# <source filename> = <emoji name>
tenor.gif = copium
ezgif-4-a3f9c0.gif = jungle_hype
```

Illegal names are rewritten with a warning (`Jungle HYPE!!` → `jungle_hype`),
and entries matching no source file are reported. Generate a stub with every
source pre-filled using `--write-names`.

To decide names you have to see the files, which is what `--preview` is for:

```bash
python3 tools/emojify.py emoji_src/ --preview /tmp/previews
```

It writes one numbered image per source and prints the number-to-filename
mapping. Animated sources render as a 3-frame strip (start, middle, end),
because the first frame of a reaction gif is usually a fade-in that tells you
nothing. Point Claude Code at that folder and it can name the whole batch.

Animated sources are stepped down a quality ladder until they fit — framerate
first, then colours, then lossy compression, and only then dimensions.
`[degraded xN]` reports how far down that ladder a file had to go.

Options: `--size` (max dimension), `--max-bytes` (byte budget), `--force`,
`--dry-run`.

`emoji_src/` and `emoji_out/` are gitignored.

## Emoji upload (`tools/emoji_upload.py`)

The other half of the converter: it puts the finished files in the server, so
adding a batch of emoji is not one trip through Server Settings per file.

```bash
python3 tools/emoji_upload.py                  # report slots, upload nothing
python3 tools/emoji_upload.py emoji_out/       # upload the folder
python3 tools/emoji_upload.py emoji_out/tenor.gif --name copium
```

```
guild:  The Jungle  (boost tier 1)
emoji:  87/100 used  (16 animated, 71 static)  ->  13 free

  ok    :dk_surprise:  233K  <a:dk_surprise:1534980686857900193>
  skip  :cheems:  already in The Jungle
  FAIL  :toobig:  293K exceeds Discord's 256K  (run it through tools/emojify.py)
```

Credentials come from `.env`. It uses the bot's own token, which needs
**Manage Expressions** in the target server; `DISCORD_GUILD_ID` is the default
target and `--guild <id>` overrides it. **Check which server you are pointing
at** — the token is usually in more than one, and `DISCORD_GUILD_ID` locally
may not be the one you mean. `--list` prints every guild the token can see,
with ids.

Names already taken are skipped rather than duplicated (Discord permits two
emoji with one name; telling them apart afterwards is miserable). `--dry-run`
shows the plan, `--force` overrides the skips. Emoji creation is rate limited
hard, so a large batch pauses when Discord says to instead of failing halfway.

Standard library only — nothing is added to `requirements.txt`.

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
