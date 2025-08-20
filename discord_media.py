import os
import sys
import time
import yaml
import requests
import discord
import json
from discord.ext import commands
from functools import wraps
import logging
from aiohttp import web
import asyncio
import time
from collections import deque

from logging.handlers import RotatingFileHandler

# Ensure a logs folder exists
os.makedirs("/app/logs", exist_ok=True)

# Reset any existing handlers to avoid duplicate logs
logging.getLogger().handlers.clear()

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("/app/logs/bot.log"),
        logging.StreamHandler(sys.stdout)  # also log to stdout
    ]
)

logger = logging.getLogger(__name__)

rotating_handler = RotatingFileHandler(
    "/app/logs/bot.log",
    maxBytes=5*1024*1024,  # 5 MB
    backupCount=3
)
logging.getLogger().addHandler(rotating_handler)

# -------------------------
# LOAD CONFIG
# -------------------------
with open("config.yaml", "r") as f:
    CONFIG = yaml.safe_load(f)

DISCORD_TOKEN = CONFIG["discord"]["token"]
RESTRICTED_CHANNELS = CONFIG["discord"].get("restricted_channels", [])

# API details
RADARR_URL = CONFIG["radarr"]["url"].rstrip("/")
RADARR_API = CONFIG["radarr"]["api_key"]
SONARR_URL = CONFIG["sonarr"]["url"].rstrip("/")
SONARR_API = CONFIG["sonarr"]["api_key"]

SONARR_HEADERS = {
    "X-Api-Key": SONARR_API,
    "Content-Type": "application/json"
}

RADARR_HEADERS = {
    "X-Api-Key": RADARR_API,
    "Content-Type": "application/json"
}

QUALITY_PROFILE_ID = CONFIG.get("quality_profile_id", 6)
WEBHOOK_PORT = CONFIG.get("webhook_port", 5000)

# -------------------------# RECENTLY ADDED TRACKER
# -------------------------
recent_additions = {}  # { "tv_12345": timestamp, "movie_54321": timestamp }
TTL = 600  # 10 minutes

def mark_recently_added(media_type, media_id):
    key = f"{media_type}_{media_id}"
    recent_additions[key] = time.time()

def cleanup_recent():
    now = time.time()
    expired = [k for k, t in recent_additions.items() if now - t > TTL]
    for k in expired:
        del recent_additions[k]

# -------------------------
# Folder pickers based on genres
# -------------------------
def pick_root_folder(media_type, genres):
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
    async def send_bot_help(self, mapping):
        """Sends a list of all commands with brief descriptions"""
        embed = discord.Embed(
            title="📖 Bot Help",
            description="Here are the available commands:",
            color=discord.Color.blue()
        )
        for _, commands_list in mapping.items():
            # Filter out hidden commands
            visible_cmds = [cmd for cmd in commands_list if not cmd.hidden]

            # Sort alphabetically by command name
            visible_cmds.sort(key=lambda c: c.name)
            for cmd in visible_cmds:
                # Build aliases string
                alias_str = f" (alias: !{', !'.join(cmd.aliases)})" if cmd.aliases else ""
                embed.add_field(
                    name=f"!{cmd.name} {alias_str}",
                    value=cmd.help or "No description provided.",
                    inline=False
                )
        await self.get_destination().send(embed=embed)

    async def send_command_help(self, command):
        """Sends detailed help for a single command"""
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
send_task: asyncio.Task = None
event_cache = {}
event_queue = deque()
def schedule_send(channel):
    async def flush_events(channel):
        await asyncio.sleep(DEBOUNCE_TIME)

        if not event_queue:
            return

        # Collect all events in the queue
        batched = []
        while event_queue:
            batched.append(event_queue.popleft())

        # Send a single embed to Discord
        embed = discord.Embed(
            title="📡 Media Update",
            description="\n".join(batched),
            color=discord.Color.green()
        )
        await channel.send(embed=embed)

    global send_task
    if send_task is None or send_task.done():
        send_task = asyncio.create_task(flush_events(channel))

async def handle_event(request):
    channel = bot.get_channel(RESTRICTED_CHANNELS[0]) if RESTRICTED_CHANNELS else None

    if channel is None:
        return web.Response(text="")
    
    event_data = await request.json()
    logger.info(f"handle_event: {json.dumps(event_data)}")

    cleanup_recent()
    status = event_data.get("eventType")
    tvdb_id = event_data.get("series", {}).get("tvdbId", None)
    tmdb_id = event_data.get("movie", {}).get("tmdbId", None)
    key_sonar = f"tv_{tvdb_id}"
    key_radarr = f"movie_{tvdb_id}"
    if tvdb_id is not None:
        if status == "Grab" and key_sonar not in recent_additions:
            return web.Response(text="")

        series = event_data.get("series", {}).get("title")
        episode = event_data.get("episodes", [{}])[0]  # usually Sonarr sends a list
        season_num = episode.get("seasonNumber")
        episode_num = episode.get("episodeNumber")

        # Store in cache
        cache_key = f"{series}-S{season_num:02}E{episode_num:02}-{status}"
        event_cache[cache_key] = time.time()
        new_sonarr_event = f"📺 **{series}** - S{season_num:02}E{episode_num:02} → {status}"
        event_queue.append(new_sonarr_event)
        logger.info(f"Adding event: {new_sonarr_event}")

    if tmdb_id is not None:
        if status == "Grab" and key_radarr not in recent_additions:
            return web.Response(text="")
    
        movie_title = event_data.get("movie", {}).get("title", "Unknown")
        new_radarr_event = f"🎬 Movie **{movie_title}** → {status}"
        event_queue.append(new_radarr_event)
        logger.info(f"Adding event: {new_radarr_event}")

    # Debounce + batch send
    schedule_send(channel)
    return web.Response(text="")

def restricted_channel(func):
    @wraps(func)
    async def wrapper(ctx, *args, **kwargs):
        # Check if the command is in allowed channels
        if RESTRICTED_CHANNELS and ctx.channel.id not in RESTRICTED_CHANNELS:
            await ctx.send("❌ This command is not allowed in this channel.")
            return False
        return await func(ctx, *args, **kwargs)
    return wrapper

def shorten_path(path: str) -> str:
    return os.path.basename(path.rstrip("/\\"))

@bot.command(name="lookup", aliases=["find", "search"], help="Lookup a show by name and return TVDB ID + link")
@restricted_channel
async def lookup(ctx, *, query: str):
    """
    Arguments:
      query (str): TV show name to find
    """
    try:
        res = requests.get(f"{SONARR_URL}/api/v3/series/lookup?term={query}", headers={"X-Api-Key": SONARR_API})
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
        logger.error(f"Error while searching: {e}")
        await ctx.send(f"⚠️ Error while searching: {query}")

@bot.command(name="lookupmovie", aliases=["findmovie", "searchmovie"], help="Lookup a movie by name and return TMDB ID + links")
@restricted_channel
async def lookup_movie(ctx, *, query: str):
    """
    Arguments:
      query (str): Movie name to find
    """
    try:
        res = requests.get(
            f"{RADARR_URL}/api/v3/movie/lookup?term={query}",
            headers={"X-Api-Key": RADARR_API}
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

        for movie in results[:20]:  # limit to first 20
            logger.info(f"----- {json.dumps(movie)}")
            title = movie.get("title", "Unknown Title")
            year = movie.get("year", "N/A")
            tmdb_id = movie.get("tmdbId", "N/A")
            genres = ", ".join(movie.get("genres", [])) or "N/A"
            tmdb_link = f"https://www.themoviedb.org/movie/{tmdb_id}" if tmdb_id != "N/A" else "N/A"

            embed = discord.Embed()
            embed.add_field(
                name=f"{title} ({year})",
                value=f"🔗[TMDB]({tmdb_link})\nID: `{tmdb_id}`\nGenres: {genres}",
                inline=False
            )
            if movie.get("remotePoster", False):
                embed.set_thumbnail(url=movie.get("remotePoster"))
            embed_list.append(embed)

        # Send in batches of 10
        batch_size = 10
        for i in range(0, len(embed_list), batch_size):
            await ctx.send(embeds=embed_list[i:i+batch_size])

    except Exception as e:
        logger.error(f"Error while searching for movies: {e}")
        await ctx.send(f"⚠️ Error while searching for movies: {query}")

# Add TV show
@bot.command(name="addtv", help="Add a TV show by TVDB ID")
@restricted_channel
async def add_tv(ctx, tvdb_id: int):
    """
    Arguments:
      tvdb_id (int): The TVDB ID from [TheTvDB](https://www.thetvdb.com/) of the series to add.
    """
    try:
        res = requests.get(f"{SONARR_URL}/api/v3/series/lookup?term=tvdb:{tvdb_id}",
                           headers={"X-Api-Key": SONARR_API})
        res.raise_for_status()
        results = res.json()
        if not results:
            await ctx.send(f"❌ No show found with TVDB ID `{tvdb_id}`.\n🔎 Please check [TheTVDB](https://www.thetvdb.com).")
            return
        show = results[0]
        root_folder = pick_root_folder("sonarr", show.get("genres", []))
        payload = {
            "title": show["title"],
            "qualityProfileId": QUALITY_PROFILE_ID,
            "tvdbId": tvdb_id,
            "titleSlug": show["titleSlug"],
            "images": show.get("images", []),
            "rootFolderPath": root_folder,
            "addOptions": {"monitor": "all", "searchForMissingEpisodes": True}
        }
        add_res = requests.post(f"{SONARR_URL}/api/v3/series",
                                headers={"X-Api-Key": SONARR_API}, json=payload)
        if add_res.status_code == 400:
            await ctx.send(f"❌ **{show['title']}** already added")
            return
        add_res.raise_for_status()
        mark_recently_added("tv", tvdb_id)
        await ctx.send(f"✅ Added TV show: **{show['title']}** to folder `{shorten_path(root_folder)}`")
    except Exception as e:
        logger.error(f"Error while adding TV show: {e}")
        await ctx.send(f"⚠️ Error while adding TV show: {tvdb_id}")

# Add movie
@bot.command(name="addmovie", help="Add a movie by TMDB ID")
@restricted_channel
async def add_movie(ctx, tmdb_id: int):
    """
    Arguments:
      tmdb_id (int): The TMDB ID from [TheMovieDB](https://www.themoviedb.org/) of the series to add.
    """
    try:
        res = requests.get(f"{RADARR_URL}/api/v3/movie/lookup?term=tmdb:{tmdb_id}",
                           headers={"X-Api-Key": RADARR_API})
        res.raise_for_status()
        results = res.json()
        if not results:
            await ctx.send(f"❌ No movie found with TMDB ID `{tmdb_id}`.\n🔎 Please check [TheMovieDB](https://www.themoviedb.org/).")
            return
        movie = results[0]
        root_folder = pick_root_folder("radarr", movie.get("genres", []))
        payload = {
            "title": movie["title"],
            "qualityProfileId": QUALITY_PROFILE_ID,
            "tmdbId": tmdb_id,
            "titleSlug": movie["titleSlug"],
            "images": movie.get("images", []),
            "rootFolderPath": root_folder,
            "monitored": True,
            "addOptions": {"searchForMovie": True}
        }
        add_res = requests.post(f"{RADARR_URL}/api/v3/movie",
                                headers={"X-Api-Key": RADARR_API}, json=payload)
        if add_res.status_code == 400:
            await ctx.send(f"❌ **{movie['title']}** already added")
            return
        add_res.raise_for_status()
        mark_recently_added("movie", tmdb_id)
        await ctx.send(f"✅ Added movie: **{movie['title']}** to folder `{shorten_path(root_folder)}`")
    except Exception as e:
        logger.error(f"Error while adding movie: {e}")
        await ctx.send(f"⚠️ Error while adding movie: {tmdb_id}")

async def fetch_sonarr_queue(request_tvdb_id=None, page_size=500):
    """Fetch Sonarr queue and group episodes by show"""
    shows = {}
    try:
        resp = requests.get(f"{SONARR_URL}/api/v3/queue?pageSize={page_size}", headers=SONARR_HEADERS)
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
                        f"{SONARR_URL}/api/v3/episode/{episode_id}", headers=SONARR_HEADERS
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
        resp = requests.get(f"{RADARR_URL}/api/v3/queue?pageSize={page_size}", headers=RADARR_HEADERS)
        resp.raise_for_status()
        queue = resp.json().get("records", [])

        for q in queue:
            if not isinstance(q, dict):
                continue  

            if q.get("sizeleft", 0) > 0:  # still downloading
                movie_id = q.get("movieId")
                resp = requests.get(f"{RADARR_URL}/api/v3/movie/{movie_id}", headers=RADARR_HEADERS)
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
async def progress(ctx):
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
    resp = requests.get(url, headers=SONARR_HEADERS).json()
    for s in resp:
        if s.get("tvdbId") == tvdb_id:
            return s
    return None

async def get_movie_info(tmdb_id: int):
    movie_resp = requests.get(
        f"{RADARR_URL}/api/v3/movie",
        headers=RADARR_HEADERS,
        params={"tmdbId": tmdb_id}
    )

    if movie_resp.status_code != 200 or not movie_resp.json():
        logger.warning(f"get_movie_info: Unable to find movie {tmdb_id}")
        return None
    return movie_resp.json()[0]

async def get_episodes(tvdb_id: int):
    series = await get_series_info(tvdb_id)
    if not series:
        return []
    url = f"{SONARR_URL}/api/v3/episode?seriesId={series['id']}"
    return requests.get(url, headers=SONARR_HEADERS).json()

@bot.command(name="tv", help="Show series information per season, including downloads in progress.")
@restricted_channel
async def tv(ctx, tvdb_id: int):
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
    seasons = {}
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
        for show, eps in sonarr_data.items():
            msg_lines.extend([f"{ep}" for ep in eps])

    await ctx.send("\n".join(msg_lines))

@bot.command(name="movie", help="Show status of a movie by TMDB ID")
async def movie(ctx, tmdb_id: int):
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
    logger.info(f"Webhook server running on port {WEBHOOK_PORT}")


# Use setup_hook to start aiohttp server
@bot.event
async def setup_hook():
    bot.loop.create_task(start_webserver())

@bot.event
async def on_ready():
    logger.info(f"✅ Logged in as {bot.user}")

bot.run(DISCORD_TOKEN)
