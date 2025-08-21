# This file is part of Discord Sonarr/Radarr Bot.
#
# Copyright (C) 2025 Luke H
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.


# Discord Sonarr/Radarr Bot
# -------------------------

# This bot integrates with Sonarr and Radarr APIs, providing the following features:
# - Lookup TV shows and movies by name
# - Add TV shows and movies by ID
# - Track and display download progress
# - Expose a webhook endpoint for Radarr/Sonarr events
# - Send event updates to Discord channels with batching and debounce

import os
import time
from functools import wraps
import logging
import asyncio
from collections import deque
from logging.handlers import RotatingFileHandler
from typing import TypedDict, Mapping, Union, Any
import requests
import yaml
from aiohttp import web
import discord
from discord.ext import commands

# Ensure a logs folder exists
os.makedirs("/app/logs", exist_ok=True)

# Load config
with open("config.yaml", "r", encoding="utf-8") as f:
    CONFIG = yaml.safe_load(f)

# Reset any existing handlers to avoid duplicate logs
logging.getLogger().handlers.clear()

LOG_LEVEL_ERROR = False
if CONFIG["logging"]["level"] in logging.getLevelNamesMapping():
    LOG_LEVEL = logging.getLevelNamesMapping()[CONFIG["logging"]["level"]]
else:
    LOG_LEVEL_ERROR = True
    LOG_LEVEL = logging.INFO

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("/app/logs/bot.log"),
        logging.StreamHandler()  # also log to stdout
    ]
)

logger = logging.getLogger(__name__)

rotating_handler = RotatingFileHandler(
    "/app/logs/bot.log",
    maxBytes=5*1024*1024,  # 5 MB
    backupCount=3
)
logging.getLogger().addHandler(rotating_handler)

if LOG_LEVEL_ERROR:
    logger.error("Unknown log level %s, using INFO", CONFIG["logging"]["level"])

DISCORD_TOKEN: str = CONFIG["discord"]["token"]
RESTRICTED_CHANNELS: list[int] = CONFIG["discord"].get("restricted_channels", [])

def quality_profile_parser(profile_id: str | int, service: str) -> int:
    """
    Parse and validate a quality profile ID for a given service.

    Args:
        profile_id (str | int): The quality profile identifier to parse.
        service (str): The name of the service (used in log messages).

    Returns:
        int: A valid quality profile ID, or 6 if the input is invalid.
    """
    try:
        quality_profile = int(profile_id)
        if quality_profile > 0:
            return quality_profile
        else:
            logging.error("%s quality_profile_id must be greater than 0", service)
    except (TypeError, ValueError):
        logging.error("%s quality_profile_id must be a number", service)
    logging.info("%s quality_profile_id using defaults", service)
    return 6 # default of 6 = 720p/1080p

# API details
API_TIMEOUT = 30
RADARR_URL = CONFIG["radarr"]["url"].rstrip("/")
RADARR_API = CONFIG["radarr"]["api_key"]
RADARR_QUALITY_PROFILE_ID = \
    quality_profile_parser(CONFIG["radarr"].get("quality_profile_id", 6), "radarr")
SONARR_URL = CONFIG["sonarr"]["url"].rstrip("/")
SONARR_API = CONFIG["sonarr"]["api_key"]
SONARR_QUALITY_PROFILE_ID = \
    quality_profile_parser(CONFIG["sonarr"].get("quality_profile_id", 6), "radarr")

SONARR_HEADERS = {
    "X-Api-Key": SONARR_API,
    "Content-Type": "application/json"
}

RADARR_HEADERS = {
    "X-Api-Key": RADARR_API,
    "Content-Type": "application/json"
}

WEBHOOK_PORT = CONFIG.get("webhook_port", 5000)

# Recently added tracker
RECENT_TTL = CONFIG["settings"].get("recent_ttl_seconds", 600)
recent_additions: dict[str, float] = {}  # { "tv_12345": timestamp, "movie_54321": timestamp }

def mark_recently_added(media_type: str, media_id: int) -> None:
    """
    Mark a media item as recently added.

    Stores the current timestamp for a given media item, identified by 
    its type and ID, in the global `recent_additions` mapping. This can 
    later be used to check if an item was added recently.

    Args:
        media_type (str): The type of media (e.g., "movie", "tv").
        media_id (int): The unique identifier for the media item.
    """
    key = f"{media_type}_{media_id}"
    recent_additions[key] = time.time()

def cleanup_recent() -> None:
    """Remove expired entries from the recent additions cache."""
    now = time.time()
    expired = [k for k, t in recent_additions.items() if now - t > RECENT_TTL]
    for k in expired:
        del recent_additions[k]

def check_recent_list(status: str, media_type: str, media_id: int) -> bool:
    """
    Check the recent list to see if we should send a grap notification
    
    Args:
        status (str): The notification status type
        media_type (str): The type of media (e.g., "movie", "tv").
        media_id (int): The unique identifier for the media item.

    Returns:
        bool: Returns true if the item is not of type Grab or in recent_additions
    """
    key = f"{media_type}_{media_id}"
    return status == "Grab" and key not in recent_additions

# -------------------------
# Folder pickers based on genres
# -------------------------
def pick_root_folder(media_type: str, genres: list[str]) -> str:
    """
    Pick a root folder path for a given media type based on its genres.

    Args:
        media_type (str): Either "sonarr" or "radarr".
        genres (list[str]): A list of genre strings.

    Returns:
        str: Path to the selected root folder.
    """
    folders = CONFIG[media_type]["folders"]
    if not genres:
        return folders["default"]

    genres_lower = [g.lower() for g in genres]
    if "kids" in genres_lower or "animation" in genres_lower:
        return folders["kids"]
    if "documentary" in genres_lower:
        return folders["documentary"]
    return folders["default"]

class CustomHelpCommand(commands.HelpCommand):
    """Custom help command that displays bot commands in Discord embeds."""

    async def send_bot_help(self,
            mapping:Mapping[Union[commands.Cog, None], list[commands.Command[Any, ..., Any]]],
            /) -> None:
        """Send a list of all commands with brief descriptions."""
        embed = discord.Embed(
            title="📖 Bot Help",
            description="Here are the available commands:",
            color=discord.Color.blue()
        )
        for _, commands_list in mapping.items():
            visible_cmds = [cmd for cmd in commands_list if not cmd.hidden]
            visible_cmds.sort(key=lambda c: c.name)
            for cmd in visible_cmds:
                alias_str = f" (alias: !{', !'.join(cmd.aliases)})" if cmd.aliases else ""
                embed.add_field(
                    name=f"!{cmd.name} {alias_str}",
                    value=cmd.help or "No description provided.",
                    inline=False
                )
        await self.get_destination().send(embed=embed)

    async def send_command_help(self, command:commands.Command[Any, ..., Any], /) -> None:
        """Send detailed help for a single command."""
        doc = command.callback.__doc__ or ""
        help_text = f"{command.help or 'No description provided.'}\n"
        if doc:
            help_text += f"{doc}"

        embed = discord.Embed(
            title=f"ℹ️ Help for `!{command.name}`",
            description=help_text,
            color=discord.Color.green()
        )
        await self.get_destination().send(embed=embed)

# -------------------------
# DISCORD SETUP
# -------------------------
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)
bot.help_command = CustomHelpCommand()

# Store recent events in memory
CACHE_TTL = 60  # seconds to keep duplicate protection
DEBOUNCE_TIME = 5  # seconds to wait before sending batch

# Debounce worker
send_task: asyncio.Task | None = None
event_cache = {}
event_queue: deque[str] = deque()

def schedule_send() -> None:
    """
    Schedule a batch send of queued events to a Discord channel.
    Groups multiple events into one embed if triggered close together.
    """
    async def flush_events():
        await asyncio.sleep(DEBOUNCE_TIME)

        if not event_queue:
            return

        batched = []
        while event_queue:
            batched.append(event_queue.popleft())

        embed = discord.Embed(
            title="📡 Media Update",
            description="\n".join(batched),
            color=discord.Color.green()
        )

        if not RESTRICTED_CHANNELS:
            logger.warning("restricted_channels not defined in config")
            return

        channel = bot.get_channel(RESTRICTED_CHANNELS[0])
        if channel is None:
            logger.warning("Unable to find restricted_channels number %s", RESTRICTED_CHANNELS[0])
        await channel.send(embed=embed)

    global send_task
    if send_task is None or send_task.done():
        send_task = asyncio.create_task(flush_events())

async def handle_event(request: web.Request) -> web.Response:
    """
    Handle webhook POST events from Sonarr and Radarr.
    Parses incoming JSON and schedules event notifications to Discord.
    """

    if not RESTRICTED_CHANNELS:
        logger.warning("There are no restricted_channels in the config defined")
        return web.Response(text="")

    event_data = await request.json()
    cleanup_recent()

    status = event_data.get("eventType")
    tvdb_id = event_data.get("series", {}).get("tvdbId", None)
    tmdb_id = event_data.get("movie", {}).get("tmdbId", None)

    if tvdb_id is not None:
        if check_recent_list(status, "tv", tvdb_id):
            return web.Response(text="")

        series = event_data.get("series", {}).get("title")
        episode = event_data.get("episodes", [{}])[0]
        season_num = episode.get("seasonNumber")
        episode_num = episode.get("episodeNumber")

        cache_key = f"{series}-S{season_num:02}E{episode_num:02}-{status}"
        event_cache[cache_key] = time.time()
        new_sonarr_event = f"📺 **{series}** - S{season_num:02}E{episode_num:02} → {status}"
        event_queue.append(new_sonarr_event)
        logger.info("Adding event: %s", new_sonarr_event)

    if tmdb_id is not None:
        if check_recent_list(status, "movie", tmdb_id):
            return web.Response(text="")

        movie_title = event_data.get("movie", {}).get("title", "Unknown")
        new_radarr_event = f"🎬 Movie **{movie_title}** → {status}"
        event_queue.append(new_radarr_event)
        logger.info("Adding event: %s", new_radarr_event)

    schedule_send()
    return web.Response(text="")

def restricted_channel(func):
    """Decorator to restrict commands to specific Discord channels."""
    @wraps(func)
    async def wrapper(ctx, *args, **kwargs):
        if RESTRICTED_CHANNELS and ctx.channel.id not in RESTRICTED_CHANNELS:
            await ctx.send("❌ This command is not allowed in this channel.")
            return False
        return await func(ctx, *args, **kwargs)
    return wrapper

def shorten_path(path: str) -> str:
    """Return only the final component of a filesystem path."""
    return os.path.basename(path.rstrip("/\\"))

@bot.command(name="lookup", aliases=["find", "search"], help="Lookup a show by name and return TVDB ID + link")
@restricted_channel
async def lookup_command(ctx, *, query: str):
    """
    Arguments:
      query (str): TV show name to find
    """
    try:
        res = requests.get(
            f"{SONARR_URL}/api/v3/series/lookup?term={query}",
            headers={"X-Api-Key": SONARR_API},
            timeout=API_TIMEOUT
        )
        res.raise_for_status()
        results = res.json()
        if not results:
            await ctx.send(f"❌ No shows found for `{query}`.")
            return

        header = discord.Embed(
            title=f"Search Results for: {query}",
            color=0x3498db
        )
        await ctx.send(embed=header)
        embed_list = []
        for show in results[:20]: # limit to first 20
            title = show.get("title", "Unknown")
            year = show.get("year", "N/A")
            tvdb_id = show.get("tvdbId", "N/A")
            genres = ", ".join(show.get("genres", [])) or "N/A"
            tvdb_link = f"https://www.thetvdb.com/dereferrer/series/{tvdb_id}"

            embed = discord.Embed()
            embed.add_field(
                name=f"{title} ({year})",
                value=f"🔗[TVDB]({tvdb_link})\nID: `{tvdb_id}`\nGenres: {genres}",
                inline=False
            )
            if show.get("remotePoster", False):
                embed.set_thumbnail(url=show.get("remotePoster"))
            embed_list.append(embed)

        # Send in batches of 10
        batch_size = 10
        for i in range(0, len(embed_list), batch_size):
            await ctx.send(embeds=embed_list[i:i+batch_size])
    except Exception as e:
        logger.error("Error while searching: %s", e)
        await ctx.send(f"⚠️ Error while searching: {query}")

@bot.command(name="lookupmovie", aliases=["findmovie", "searchmovie"], help="Lookup a movie by name and return TMDB ID + links")
@restricted_channel
async def lookup_movie_command(ctx, *, query: str):
    """
    Arguments:
      query (str): Movie name to find
    """
    try:
        res = requests.get(
            f"{RADARR_URL}/api/v3/movie/lookup?term={query}",
            headers={"X-Api-Key": RADARR_API},
            timeout=API_TIMEOUT
        )
        res.raise_for_status()
        results = res.json()

        if not results:
            await ctx.send(f"❌ No movies found for `{query}`.")
            return

        header = discord.Embed(
            title=f"Search Results for: {query}",
            color=0x3498db
        )
        await ctx.send(embed=header)
        embed_list = []

        for l_movie in results[:20]:  # limit to first 20
            title = l_movie.get("title", "Unknown Title")
            year = l_movie.get("year", "N/A")
            tmdb_id = l_movie.get("tmdbId", "N/A")
            genres = ", ".join(l_movie.get("genres", [])) or "N/A"
            tmdb_link = f"https://www.themoviedb.org/movie/{tmdb_id}" if tmdb_id != "N/A" else "N/A"

            embed = discord.Embed()
            embed.add_field(
                name=f"{title} ({year})",
                value=f"🔗[TMDB]({tmdb_link})\nID: `{tmdb_id}`\nGenres: {genres}",
                inline=False
            )
            if l_movie.get("remotePoster", False):
                embed.set_thumbnail(url=l_movie.get("remotePoster"))
            embed_list.append(embed)

        # Send in batches of 10
        batch_size = 10
        for i in range(0, len(embed_list), batch_size):
            await ctx.send(embeds=embed_list[i:i+batch_size])

    except Exception as e:
        logger.error("Error while searching for movies: %s", e)
        await ctx.send(f"⚠️ Error while searching for movies: {query}")

# Add TV show
@bot.command(name="addtv", help="Add a TV show by TVDB ID")
@restricted_channel
async def add_tv_command(ctx, tvdb_id: int):
    """
    Arguments:
      tvdb_id (int): The TVDB ID from [TheTvDB](https://www.thetvdb.com/) of the series to add.
    """
    try:
        res = requests.get(
            f"{SONARR_URL}/api/v3/series/lookup?term=tvdb:{tvdb_id}",
            headers={"X-Api-Key": SONARR_API},
            timeout=API_TIMEOUT
        )
        res.raise_for_status()
        results = res.json()
        if not results:
            await ctx.send(f"❌ No show found with TVDB ID `{tvdb_id}`.\n🔎 Please check [TheTVDB](https://www.thetvdb.com).")
            return
        show = results[0]
        root_folder = pick_root_folder("sonarr", show.get("genres", []))
        payload = {
            "title": show["title"],
            "qualityProfileId": SONARR_QUALITY_PROFILE_ID,
            "tvdbId": tvdb_id,
            "titleSlug": show["titleSlug"],
            "images": show.get("images", []),
            "rootFolderPath": root_folder,
            "addOptions": {"monitor": "all", "searchForMissingEpisodes": True}
        }
        add_res = requests.post(
            f"{SONARR_URL}/api/v3/series",
            headers={"X-Api-Key": SONARR_API},
            json=payload,
            timeout=API_TIMEOUT
        )
        if add_res.status_code == 400:
            await ctx.send(f"❌ **{show['title']}** already added")
            return
        add_res.raise_for_status()
        mark_recently_added("tv", tvdb_id)
        await ctx.send(f"✅ Added TV show: **{show['title']}** to folder `{shorten_path(root_folder)}`")
    except Exception as e:
        logger.error("Error while adding TV show: %s", e)
        await ctx.send(f"⚠️ Error while adding TV show: {tvdb_id}")

# Add movie
@bot.command(name="addmovie", help="Add a movie by TMDB ID")
@restricted_channel
async def add_movie_command(ctx, tmdb_id: int):
    """
    Arguments:
      tmdb_id (int): The TMDB ID from [TheMovieDB](https://www.themoviedb.org/) of the series to add.
    """
    try:
        res = requests.get(
            f"{RADARR_URL}/api/v3/movie/lookup?term=tmdb:{tmdb_id}",
            headers={"X-Api-Key": RADARR_API},
            timeout=API_TIMEOUT
        )
        res.raise_for_status()
        results = res.json()
        if not results:
            await ctx.send(f"❌ No movie found with TMDB ID `{tmdb_id}`.\n🔎 Please check [TheMovieDB](https://www.themoviedb.org/).")
            return
        a_movie = results[0]
        root_folder = pick_root_folder("radarr", a_movie.get("genres", []))
        payload = {
            "title": a_movie["title"],
            "qualityProfileId": RADARR_QUALITY_PROFILE_ID,
            "tmdbId": tmdb_id,
            "titleSlug": a_movie["titleSlug"],
            "images": a_movie.get("images", []),
            "rootFolderPath": root_folder,
            "monitored": True,
            "addOptions": {"searchForMovie": True}
        }
        add_res = requests.post(
            f"{RADARR_URL}/api/v3/movie",
            headers={"X-Api-Key": RADARR_API},
            json=payload,
            timeout=API_TIMEOUT
        )
        if add_res.status_code == 400:
            await ctx.send(f"❌ **{a_movie['title']}** already added")
            return
        add_res.raise_for_status()
        mark_recently_added("movie", tmdb_id)
        await ctx.send(f"✅ Added movie: **{a_movie['title']}** to folder `{shorten_path(root_folder)}`")
    except Exception as e:
        logger.error("Error while adding movie: %s", e)
        await ctx.send(f"⚠️ Error while adding movie: {tmdb_id}")

async def fetch_sonarr_queue(request_tvdb_id=None, page_size=500):
    """Fetch Sonarr queue and group episodes by show"""
    shows = {}
    try:
        resp = requests.get(
            f"{SONARR_URL}/api/v3/queue?pageSize={page_size}",
            headers=SONARR_HEADERS,
            timeout=API_TIMEOUT
        )
        resp.raise_for_status()
        queue = resp.json().get("records", [])
        for q in queue:
            if not isinstance(q, dict):
                continue

            if q.get("sizeleft", 0) > 0:  # still downloading
                # fetch episode details
                show_title = "Unknown Show"
                episode_id = q.get("episodeId")
                episode_title = "Unknown"
                season_number = 0
                episode_number = 0

                if episode_id:
                    ep_resp = requests.get(
                        f"{SONARR_URL}/api/v3/episode/{episode_id}",
                        headers=SONARR_HEADERS,
                        timeout=API_TIMEOUT
                    )
                    if ep_resp.ok:
                        ep = ep_resp.json()
                        series = ep.get("series", {})
                        show_title = f"{series.get("title", "Unknown Show")} - {series.get("tvdbId", "-")}"
                        episode_title = ep.get("title", "Untitled")
                        season_number = ep.get("seasonNumber", 0)
                        episode_number = ep.get("episodeNumber", 0)

                progress = 100 * (q["size"] - q["sizeleft"]) / q["size"]

                ep_line = f"S{season_number:02}E{episode_number:02} **{episode_title}** ({progress:.1f}%)"
                if request_tvdb_id is None or request_tvdb_id == series.get("tvdbId", "-"):
                    shows.setdefault(show_title, []).append(ep_line)

    except Exception as e:
        return {"error": str(e)}

    return shows


async def fetch_radarr_queue(request_tmdb_id=None, page_size=500):
    """Fetch Radarr queue and group movies that are downloading"""
    movies = {}
    try:
        resp = requests.get(
            f"{RADARR_URL}/api/v3/queue?pageSize={page_size}",
            headers=RADARR_HEADERS,
            timeout=API_TIMEOUT
        )
        resp.raise_for_status()
        queue = resp.json().get("records", [])

        for q in queue:
            if not isinstance(q, dict):
                continue  

            if q.get("sizeleft", 0) > 0:  # still downloading
                movie_id = q.get("movieId")
                resp = requests.get(
                    f"{RADARR_URL}/api/v3/movie/{movie_id}",
                    headers=RADARR_HEADERS,
                    timeout=API_TIMEOUT
                )
                resp.raise_for_status()
                movie = resp.json()
                title = f"{movie.get('title', 'Unknown Movie')} {movie.get('year', '????')}"

                progress = 100 * (q.get("size", 0) - q.get("sizeleft", 0)) / max(q.get("size", 1), 1)

                line = f"({progress:.1f}%)"
                if request_tmdb_id is None or request_tmdb_id == movie.get("tmdbId", "-"):
                    movies.setdefault(title, []).append(line)

    except Exception as e:
        return {"error": str(e)}

    return movies

@bot.command(name="progress", aliases=["status", "downloads"], help="Show the progress of any media files downloading")
@restricted_channel
async def progress_command(ctx):
    output_lines = ["📥 **Currently Downloading:**"]

    # --- Sonarr ---
    sonarr_data = await fetch_sonarr_queue()
    if "error" in sonarr_data:
        output_lines.append(f"⚠️ Error fetching Sonarr queue: {sonarr_data['error']}")
    elif sonarr_data:
        for show, eps in sonarr_data.items():
            output_lines.append(f"📺 **{show}**")
            output_lines.extend([f" └ {ep}" for ep in eps])

    # --- Radarr ---
    radarr_data = await fetch_radarr_queue()
    if isinstance(radarr_data, dict) and "error" in radarr_data:
        output_lines.append(f"⚠️ Error fetching Radarr queue: {radarr_data['error']}")
    elif radarr_data:
        output_lines.append("🎬 **Movies**")
        for movie, progress in radarr_data.items():
            output_lines.append(f" └ {movie} {progress[0]}")

    # Final output
    if len(output_lines) == 1:
        await ctx.send("✅ Nothing is currently downloading.")
    else:
        await ctx.send("\n".join(output_lines))

async def get_series_info(tvdb_id: int):
    url = f"{SONARR_URL}/api/v3/series"
    resp = requests.get(
        url,
        headers=SONARR_HEADERS,
        timeout=API_TIMEOUT
    ).json()
    for s in resp:
        if s.get("tvdbId") == tvdb_id:
            return s
    return None

async def get_movie_info(tmdb_id: int):
    movie_resp = requests.get(
        f"{RADARR_URL}/api/v3/movie",
        headers=RADARR_HEADERS,
        params={"tmdbId": tmdb_id},
        timeout=API_TIMEOUT
    )

    if movie_resp.status_code != 200 or not movie_resp.json():
        logger.warning("get_movie_info: Unable to find movie %s", tmdb_id)
        return None
    return movie_resp.json()[0]

async def get_episodes(tvdb_id: int):
    series = await get_series_info(tvdb_id)
    if not series:
        return []
    url = f"{SONARR_URL}/api/v3/episode?seriesId={series['id']}"
    return requests.get(
        url,
        headers=SONARR_HEADERS,
        timeout=API_TIMEOUT
    ).json()

class SeasonData(TypedDict):
    total: int
    downloaded: int
    eps: list

@bot.command(name="tv", help="Show series information per season, including downloads in progress.")
@restricted_channel
async def tv_command(ctx, tvdb_id: int):
    """
    Arguments:
      tvdb_id (int): The TVDB ID from [TheTvDB](https://www.thetvdb.com/) of the series to query.
    """
    series = await get_series_info(tvdb_id)
    if not series:
        await ctx.send(f"❌ No show found with TVDB ID {tvdb_id}. Please check on https://thetvdb.com/")
        return

    episodes = await get_episodes(tvdb_id)
    sonarr_data = await fetch_sonarr_queue(tvdb_id)

    # Group episodes by season
    seasons : dict[int,SeasonData]= {}
    for ep in episodes:
        season = ep.get("seasonNumber", 0)
        if season not in seasons:
            seasons[season] = {"total": 0, "downloaded": 0, "eps": []}
        seasons[season]["total"] += 1
        if ep.get("hasFile"):
            seasons[season]["downloaded"] += 1
        seasons[season]["eps"].append(ep)

    # Start message
    msg_lines = [f"📺 **{series['title']}**"]

    # Per season breakdown
    for season, stats in sorted(seasons.items()):
        if season == 0 and stats["downloaded"] == 0:  
            # Skip specials if none are downloaded
            continue
        msg_lines.append(
            f"Season {season}: {stats['downloaded']}/{stats['total']} episodes downloaded"
        )

    if "error" in sonarr_data:
        msg_lines.append(f"⚠️ Error fetching Sonarr queue: {sonarr_data['error']}")
    elif sonarr_data:
        msg_lines.append("\n📥 **Currently Downloading:**")
        for _, eps in sonarr_data.items():
            msg_lines.extend([f"{ep}" for ep in eps])

    await ctx.send("\n".join(msg_lines))

@bot.command(name="movie", help="Show status of a movie by TMDB ID")
@restricted_channel
async def movie_command(ctx, tmdb_id: int):
    """
    Arguments:
      tmdb_id (int): The TMDB ID from [TheMovieDB](https://www.themoviedb.org/) of the series to add.
    """
    movie = await get_movie_info(tmdb_id)
    if movie is None:
        await ctx.send(f"❌ No movie found with TMDB ID {tmdb_id}")
        return

    movie_id = movie["id"]
    movie_title = movie["title"]
    year = movie.get("year", "")

    queue_items = await fetch_radarr_queue()

    # 3. Match downloads for this movie
    downloads = []
    for item in queue_items:
        if item.get("movieId") == movie_id:
            size = item.get("size", 0)
            sizeleft = item.get("sizeleft", 0)
            progress = 0 if size == 0 else round(100 * (1 - sizeleft / size), 1)
            downloads.append(
                f"📥 {item['quality']['quality']['name']} - {progress}% complete"
            )

    # 4. Build response
    if downloads:
        msg = f"🎬 **{movie_title} ({year})**\nDownloading:\n" + "\n".join(downloads)
    else:
        msg = f"🎬 **{movie_title} ({year})**\nStatus: {"Avaliable" if movie.get('hasFile', False) else "Seeking"}"

    await ctx.send(msg)

# -------------------------
# RUN BOT
# -------------------------

async def start_webserver():
    app = web.Application()
    app.router.add_post("/webhook", handle_event)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", WEBHOOK_PORT)
    await site.start()
    logger.info("Webhook server running on port %s", WEBHOOK_PORT)


# Use setup_hook to start aiohttp server
@bot.event
async def setup_hook():
    bot.loop.create_task(start_webserver())

@bot.event
async def on_ready():
    logger.info("✅ Logged in as %s", bot.user)

bot.run(DISCORD_TOKEN)
