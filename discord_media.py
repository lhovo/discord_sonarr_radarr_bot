from __future__ import annotations

"""
Discord Sonarr/Radarr Bot

This file expects a config.yml next to it, e.g.:

sonarr:
  url: "http://localhost:8989"
  api_key: "YOUR_SONARR_API_KEY"
  quality_profile_id: 6
  root_folder: "/tv"

radarr:
  url: "http://localhost:7878"
  api_key: "YOUR_RADARR_API_KEY"
  quality_profile_id: 6
  root_folder: "/movies"

discord:
  token: "DISCORD_BOT_TOKEN"
  prefix: "!"
  restricted_channels: [123456789012345678]

webhook:
  host: "0.0.0.0"
  port: 5000
  secret: "OPTIONAL_SHARED_SECRET"

logging:
  level: "INFO"   # one of DEBUG, INFO, WARNING, ERROR, CRITICAL
  file: "/app/logs/bot.log"

"""

import asyncio
import logging
import re
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

import discord
import yaml
from discord.ext import commands, tasks

from hook_client import WebServer
from media.radarr import RadarrClient
from media.sonarr import SonarrClient

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.yaml"

DEFAULT_LOG_FILE = "/app/logs/bot.log"
DEFAULT_LOG_LEVEL = "INFO"

def _load_config() -> dict[str, Any]:
    if not CONFIG_PATH.exists():
        raise FileNotFoundError(f"Missing config file: {CONFIG_PATH}")
    with CONFIG_PATH.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}

CONFIG: dict[str, Any] = _load_config()

def _setup_logging(cfg: dict[str, Any] | None) -> None:
    log_cfg: dict[str, Any] = cfg.get("logging", {}) if isinstance(cfg, dict) else {}
    level_name = (log_cfg.get("level") or DEFAULT_LOG_LEVEL).upper()
    level = getattr(logging, level_name, logging.INFO)

    log_file = log_cfg.get("file") or DEFAULT_LOG_FILE
    Path(log_file).parent.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()

    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    fh = RotatingFileHandler(log_file, maxBytes=2_000_000, backupCount=3)
    fh.setFormatter(fmt)
    fh.setLevel(level)
    root.addHandler(fh)

    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    sh.setLevel(level)
    root.addHandler(sh)

_setup_logging(CONFIG)
logger = logging.getLogger("discord_media")

discord_cfg = CONFIG.get("discord", {})
DISCORD_TOKEN: str = discord_cfg.get("token", "")
DISCORD_PREFIX: str = discord_cfg.get("prefix", "!")
RESTRICTED_CHANNELS: list[int] = list(discord_cfg.get("restricted_channels") or [])

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix=DISCORD_PREFIX, intents=intents)

sonarr_cfg: dict[str, Any] = CONFIG.get("sonarr", {})
radarr_cfg: dict[str, Any] = CONFIG.get("radarr", {})

sonarr = SonarrClient(sonarr_cfg, "sonarr")
radarr = RadarrClient(radarr_cfg, "radarr")
web_hook_server = WebServer(CONFIG)

BATCH_DEBOUNCE_S = 4.0


async def _resolve_primary_channel() -> discord.TextChannel | None:
    if not RESTRICTED_CHANNELS:
        logger.warning("No restricted_channels configured; cannot send embeds.")
        return None
    chan_id = RESTRICTED_CHANNELS[0]
    channel = bot.get_channel(chan_id)
    if channel is None:
        try:
            channel = await bot.fetch_channel(chan_id)
        except (
            discord.InvalidData,
            discord.HTTPException,
            discord.NotFound,
            discord.Forbidden,
        ) as e:
            logger.warning("Unable to resolve channel %s: %s", chan_id, e)
            return None
    if isinstance(channel, discord.TextChannel):
        return channel
    return None


@tasks.loop(seconds=BATCH_DEBOUNCE_S)
async def schedule_send():
    embed = await web_hook_server.schedule_send()
    if not embed:
        return

    channel = await _resolve_primary_channel()
    if channel is None:
        return
    try:
        await channel.send(embed=embed)
    except discord.Forbidden:
        logger.error("Missing permission to send in channel %s", getattr(channel, "id", "?"))
    except discord.HTTPException as e:
        logger.error("Failed to send embed: %s", e)


@bot.event
async def on_ready():
    logger.info("Logged in as %s (id=%s)", bot.user, bot.user.id if bot.user else "?")
    await web_hook_server.start()
    if not schedule_send.is_running():
        schedule_send.start()


@bot.event
async def on_close():
    await web_hook_server.stop()


def _restrict_channels():
    async def predicate(ctx: commands.Context) -> bool:
        if not RESTRICTED_CHANNELS:
            return True
        return ctx.channel.id in RESTRICTED_CHANNELS

    return commands.check(predicate)


@bot.command(
    name="tv",
    help="!tv [search query | tvdbId]\nShow series information per season, including downloads in progress.",
)
@_restrict_channels()
async def tv_command(ctx: commands.Context, *, arg: str | None = None) -> None:
    """Search for TV shows in Sonarr: `!tv [search query | tvdbId]`.
    If no query is provided, shows current Sonarr queue for active downloads.
    """
    if not arg:
        await sonarr.tv_download_queue(ctx)
    elif arg.isdigit():
        await sonarr.tv_show(ctx, int(arg))
    else:
        await sonarr.tv_lookup(ctx, arg)


@bot.command(name="tvadd", help="!tvadd <tvdbId>\nAdd a TV show using TVDB ID")
@_restrict_channels()
async def tv_add_command(ctx: commands.Context, tvdb_id: int) -> None:
    if await sonarr.tv_add(ctx, tvdb_id):
        web_hook_server.mark_recently_added("tv", tvdb_id)


@bot.command(
    name="tvsearch",
    help="!tvsearch <tvdbId> s<season>e<episode>\nTrigger automatic Sonarr search for one episode.",
)
@_restrict_channels()
async def tv_search_command(ctx: commands.Context, tvdb_id: int, episode_ref: str) -> None:
    match = re.fullmatch(r"s(\d+)e(\d+)", episode_ref.strip().lower())
    if not match:
        await ctx.send("Format: `!tvsearch <tvdbId> s<season>e<episode>` (example: `!tvsearch 12345 s1e2`)")
        return

    season = int(match.group(1))
    episode = int(match.group(2))
    await sonarr.search_episode(ctx, tvdb_id, season, episode)


@bot.command(name="movie")
@_restrict_channels()
async def movie_command(ctx: commands.Context, *, query: str | None = None) -> None:
    """Search for movies in Radarr: `!movie <query>`.
    If no query is provided, shows current Radarr queue for active downloads.
    """
    if not query:
        await radarr.movie_download_queue(ctx)
    elif query.isdigit():
        await ctx.send("Use !movieadd <tmdbId> to add by TMDB ID.")
    else:
        await radarr.movie_lookup(ctx, query)


@bot.command(name="movieadd")
@_restrict_channels()
async def movie_add_command(ctx: commands.Context, tmdb_id: int) -> None:
    if await radarr.movie_add(ctx, tmdb_id):
        web_hook_server.mark_recently_added("movie", tmdb_id)


async def main() -> None:
    try:
        await bot.start(DISCORD_TOKEN)
    finally:
        await web_hook_server.stop()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
