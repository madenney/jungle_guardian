#!/usr/bin/env python3
"""Upload converted emoji to a Discord server from the command line.

The other half of `tools/emojify.py`: that one produces files Discord will
accept, this one puts them in the server so nobody has to click through
Server Settings once per emoji.

    python3 tools/emoji_upload.py                    # list slots, upload nothing
    python3 tools/emoji_upload.py emoji_out/         # upload the whole folder
    python3 tools/emoji_upload.py emoji_out/dk.gif --name dk_surprise

Credentials come from `.env` -- the same DISCORD_TOKEN the bot runs on, which
needs **Manage Expressions** in the target guild. DISCORD_GUILD_ID is the
default target; `--guild` overrides it, and is worth being deliberate about
when the token is in more than one server. `--list` on its own prints every
guild the token can see, which is the way to find that id.

Only the standard library is used, deliberately: requirements.txt is
installed on the bot host by the deploy, and a local dev tool has no business
adding to it.
"""
from __future__ import annotations

import argparse
import base64
import json
import mimetypes
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

API = "https://discord.com/api/v10"
ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = ROOT / ".env"

MAX_BYTES = 256 * 1024
NAME_RE = re.compile(r"^[a-z0-9_]{2,32}$")

# Emoji slots per boost tier, used only when the API does not report
# `max_emojis` itself. Servers get a bigger allowance than these numbers for
# static emoji nowadays, so treating a full pool as fatal would be wrong --
# the count is a warning, and Discord is left to have the last word.
TIER_SLOTS = {0: 50, 1: 100, 2: 150, 3: 250}


def read_env(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise SystemExit(f"error: cannot read {path}: {exc}")
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        env[key.strip()] = value.strip().strip('"').strip("'")
    return env


def call(token: str, method: str, path: str, body: dict | None = None):
    """One REST call, with a wait-and-retry on rate limits.

    Emoji creation is rate limited far more aggressively than most routes, so
    a folder of any size will hit a 429 partway through. Discord says exactly
    how long to wait, so waiting is a better answer than failing the run and
    leaving half a batch uploaded.
    """
    data = json.dumps(body).encode() if body is not None else None
    for attempt in range(5):
        req = urllib.request.Request(API + path, data=data, method=method)
        req.add_header("Authorization", f"Bot {token}")
        # Discord answers 403 to urllib's default User-Agent, whatever the
        # token says.
        req.add_header("User-Agent", "JungleGuardian-emoji-upload (local, 1.0)")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.loads(resp.read() or "null")
        except urllib.error.HTTPError as exc:
            payload = exc.read().decode(errors="replace")
            if exc.code == 429 and attempt < 4:
                try:
                    wait = float(json.loads(payload).get("retry_after", 5))
                except (ValueError, AttributeError):
                    wait = 5.0
                print(f"  rate limited, waiting {wait:.1f}s", file=sys.stderr)
                time.sleep(wait + 0.5)
                continue
            raise SystemExit(f"error: HTTP {exc.code} on {method} {path}: {payload}")
        except urllib.error.URLError as exc:
            raise SystemExit(f"error: {method} {path} failed: {exc.reason}")
    raise SystemExit(f"error: {method} {path} gave up after repeated rate limits")


def emoji_name(path: Path, override: str | None) -> str:
    """Discord's name rules, applied to a filename.

    emojify.py already writes files named after the sanitised emoji name, so
    the stem is normally usable as-is; this exists for files that came from
    somewhere else.
    """
    name = override if override else path.stem
    name = re.sub(r"[^a-z0-9_]+", "_", name.lower()).strip("_")
    name = re.sub(r"_{2,}", "_", name)[:32]
    return name


def list_guilds(token: str) -> None:
    for guild in call(token, "GET", "/users/@me/guilds"):
        print(f"  {guild['id']}  {guild['name']}")


def show_slots(guild: dict, existing: list[dict]) -> int:
    tier = guild.get("premium_tier", 0)
    limit = guild.get("max_emojis") or TIER_SLOTS.get(tier, 50)
    animated = sum(1 for e in existing if e.get("animated"))
    print(f"guild:  {guild['name']}  (boost tier {tier})")
    print(f"emoji:  {len(existing)}/{limit} used"
          f"  ({animated} animated, {len(existing) - animated} static)"
          f"  ->  {max(0, limit - len(existing))} free")
    return limit


def gather(inputs: list[Path]) -> list[Path]:
    found: list[Path] = []
    for item in inputs:
        if item.is_dir():
            found.extend(sorted(p for p in item.iterdir() if p.is_file()))
        elif item.is_file():
            found.append(item)
        else:
            print(f"  skip  {item}  (not found)", file=sys.stderr)
    return found


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Upload emoji files to a Discord server.")
    parser.add_argument("inputs", nargs="*", type=Path,
                        help="files or directories (default: report slots only)")
    parser.add_argument("--guild", help="target guild id (default: DISCORD_GUILD_ID)")
    parser.add_argument("--name", help="emoji name for a single file")
    parser.add_argument("--list", action="store_true",
                        help="list the guilds this token can see, then exit")
    parser.add_argument("-n", "--dry-run", action="store_true",
                        help="show what would be uploaded and exit")
    parser.add_argument("-f", "--force", action="store_true",
                        help="upload even if the name is already taken (Discord "
                             "allows duplicate names; they become hard to tell apart)")
    args = parser.parse_args()

    env = read_env(ENV_FILE)
    token = env.get("DISCORD_TOKEN")
    if not token:
        print(f"error: DISCORD_TOKEN not set in {ENV_FILE}", file=sys.stderr)
        return 1

    if args.list:
        list_guilds(token)
        return 0

    guild_id = args.guild or env.get("DISCORD_GUILD_ID")
    if not guild_id:
        print("error: no guild; pass --guild or set DISCORD_GUILD_ID in .env",
              file=sys.stderr)
        print("  python3 tools/emoji_upload.py --list", file=sys.stderr)
        return 1

    if args.name and len(args.inputs) != 1:
        print("error: --name applies to exactly one file", file=sys.stderr)
        return 1

    guild = call(token, "GET", f"/guilds/{guild_id}")
    existing = call(token, "GET", f"/guilds/{guild_id}/emojis")
    limit = show_slots(guild, existing)

    sources = gather(args.inputs)
    if not sources:
        for emoji in existing:
            print(f"  :{emoji['name']}:{'  (animated)' if emoji.get('animated') else ''}")
        return 0

    taken = {emoji["name"] for emoji in existing}
    free = max(0, limit - len(existing))
    uploaded = skipped = failed = 0

    print()
    for path in sources:
        name = emoji_name(path, args.name)
        size = path.stat().st_size

        if not NAME_RE.match(name):
            print(f"  FAIL  {path.name}  no usable emoji name; pass --name")
            failed += 1
            continue
        if size > MAX_BYTES:
            print(f"  FAIL  :{name}:  {size / 1024:.0f}K exceeds Discord's 256K"
                  f"  (run it through tools/emojify.py)")
            failed += 1
            continue
        if name in taken and not args.force:
            print(f"  skip  :{name}:  already in {guild['name']}")
            skipped += 1
            continue
        if uploaded >= free and not args.force:
            print(f"  skip  :{name}:  no free slots")
            skipped += 1
            continue
        if args.dry_run:
            print(f"  would upload  :{name}:  {size / 1024:.0f}K")
            continue

        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        payload = base64.b64encode(path.read_bytes()).decode()
        created = call(token, "POST", f"/guilds/{guild_id}/emojis", {
            "name": name,
            "image": f"data:{mime};base64,{payload}",
            "roles": [],
        })
        # Discord decides animated-ness from the file, not from the request, and
        # the id is what a bot response would need, so both are worth echoing.
        prefix = "a:" if created.get("animated") else ":"
        print(f"  ok    :{created['name']}:  {size / 1024:.0f}K"
              f"  <{prefix}{created['name']}:{created['id']}>")
        taken.add(created["name"])
        uploaded += 1

    parts = []
    if uploaded:
        parts.append(f"{uploaded} uploaded")
    if skipped:
        parts.append(f"{skipped} skipped")
    if failed:
        parts.append(f"{failed} failed")
    if parts:
        print(f"\n{', '.join(parts)}  ->  {guild['name']}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
