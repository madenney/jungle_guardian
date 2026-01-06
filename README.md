# Jungle Guardian (Discord Bot)

Simple Discord bot that times out users for 10 seconds if they post
3 duplicate messages in a row within 2 seconds.

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

## Score Tracking
Timeout durations scale per user and are stored in `score.json`. Each user
starts at 1 second, and the timeout doubles after every violation. The file
is updated whenever a timeout succeeds. Each entry also records a violations
history with user ID, rule ID, and timestamp.

## Slash Commands
- `/rules` lists the configured rules
- `/score` shows timeouts for you (or a specified user)

Slash command updates can take a while to appear globally. For faster updates
in a single server, set `DISCORD_GUILD_ID` in `.env` and restart the bot to
sync commands to that guild.

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
