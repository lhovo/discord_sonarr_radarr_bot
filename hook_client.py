import logging
import json
import asyncio
from typing import Any, Literal, TypedDict
from aiohttp import web
from collections import deque
from discord import Embed, Color

# ---------------------------
# Helpers & Types
# ---------------------------

class ArrEvent(TypedDict):
    service: Literal["sonarr", "radarr"]
    action: str
    title: str
    details: str
    key: str  # used for dedupe

class WebServer:
    def __init__(self, config: dict[str, Any]):
        # ---------------------------
        # Webhook server (aiohttp)
        # ---------------------------
        webhook_cfg = config.get("webhook", {})
        self.WEB_HOST = webhook_cfg.get("host", "0.0.0.0")
        self.WEB_PORT = int(webhook_cfg.get("port", 5000))
        self.WEB_SECRET = webhook_cfg.get("secret", None)
        self.RECENT_TTL = webhook_cfg.get("recent_ttl_seconds", 600)

        self._web_app = web.Application()
        self._web_app.router.add_post("/webhook", self.handle_event)

        # Recently added tracker

        # { "tv_12345": timestamp, "movie_54321": timestamp }
        self.recent_additions: dict[str, float] = {}
        self.runner: web.AppRunner | None = None
        self.site: web.TCPSite | None = None
        self.event_cache: dict[str, float] = {}
        self.event_queue: deque[str] = deque()
        self.log = logging.getLogger("WebServer")

    async def schedule_send(self) -> Embed | None:
        if not self.event_queue:
            return None

        batched = []
        while self.event_queue:
            batched.append(self.event_queue.popleft())

        embed = Embed(
            title="📡 Media Update",
            description="\n".join(batched),
            color=Color.green()
        )

        return embed

    def _now(self) -> float:
        return asyncio.get_event_loop().time()

    async def start(self) -> None:
        if self.runner is not None:
            return
        self.runner = web.AppRunner(self._web_app)
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, self.WEB_HOST, self.WEB_PORT)
        await self.site.start()
        self.log.info("Webhook server listening on %s:%s", self.WEB_HOST, self.WEB_PORT)


    async def stop(self) -> None:
        if self.site is not None:
            await self.site.stop()
            self.site = None
        if self.runner is not None:
            await self.runner.cleanup()
            self.runner = None

    def mark_recently_added(self, media_type: str, media_id: int) -> None:
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
        self.recent_additions[key] = self._now()

    def cleanup_recent(self) -> None:
        """Remove expired entries from the recent additions cache."""
        now = self._now()
        expired = [k for k, t in self.recent_additions.items() if now - t > self.RECENT_TTL]
        for k in expired:
            del self.recent_additions[k]

    def check_recent_list(self, status: str, media_type: str, media_id: int) -> bool:
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
        return status == "Grab" and key not in self.recent_additions

    def format_bytes(self, n: int | float) -> str:
        try:
            n = float(n)
        except Exception:
            return "-"
        step = 1024.0
        for unit in ["B", "KB", "MB", "GB", "TB"]:
            if n < step:
                return f"{n:.1f} {unit}"
            n /= step
        return f"{n:.1f} PB"

    async def handle_event(self, request: web.Request) -> web.Response:
        """
        Handle webhook POST events from Sonarr and Radarr.
        Parses incoming JSON and schedules event notifications to Discord.
        """

        if self.WEB_SECRET and request.headers.get("X-Webhook-Secret") != self.WEB_SECRET:
            return web.Response(status=401, text="unauthorized")

        try:
            event_data = await request.json()
        except json.JSONDecodeError:
            return web.Response(status=400, text="invalid json")

        self.cleanup_recent()

        status = event_data.get("eventType")
        tvdb_id = event_data.get("series", {}).get("tvdbId", None)
        tmdb_id = event_data.get("movie", {}).get("tmdbId", None)

        if tvdb_id is not None:
            if self.check_recent_list(status, "tv", tvdb_id):
                return web.Response(text="")

            series = event_data.get("series", {}).get("title")
            episode_list: list[dict[str, str | int]] = event_data.get("episodes") or [{}]
            episode = episode_list[0]
            season_num = episode.get("seasonNumber")
            episode_num = episode.get("episodeNumber")

            cache_key = f"{series}-S{season_num:02}E{episode_num:02}-{status}"
            self.event_cache[cache_key] = self._now()
            new_sonarr_event = f"📺 **{series}** - S{season_num:02}E{episode_num:02} → {status}"
            self.event_queue.append(new_sonarr_event)
            self.log.info("Adding event: %s", new_sonarr_event)

        if tmdb_id is not None:
            if self.check_recent_list(status, "movie", tmdb_id):
                return web.Response(text="")

            movie_title = event_data.get("movie", {}).get("title", "Unknown")
            new_radarr_event = f"🎬 Movie **{movie_title}** → {status}"
            self.event_queue.append(new_radarr_event)
            self.log.info("Adding event: %s", new_radarr_event)

        return web.Response(text="")
