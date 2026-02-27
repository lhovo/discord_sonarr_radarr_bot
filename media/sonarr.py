from typing import Any, TypedDict

from discord import Embed
from discord.ext import commands

from media.util import AlreadyAddedError, ArrClient, WebRuntimeError


class SonarrClient(ArrClient):
    def __init__(self, config: dict[str, Any], name: str):
        super().__init__(config, name, "tvdb")

    async def lookup(self, term: str) -> list[dict[str, Any]] | WebRuntimeError:
        return await self.get("/api/v3/series/lookup", params={"term": term})

    async def add_media(
        self,
        media_id: int,
        title: str,
        root_folder: str,
    ) -> dict[str, Any] | WebRuntimeError:
        body: dict[str, str | bool | int | dict[str, bool]] = {
            "tvdbId": media_id,
            "title": title,
            "qualityProfileId": self.quality_profile_id,
            "rootFolderPath": root_folder,
            "monitored": True,
            "seasonFolder": True,
            "addOptions": {
                "searchForMissingEpisodes": True,
                "searchForCutoffUnmetEpisodes": True,
            },
        }
        return await self.post("/api/v3/series", json_body=body)

    async def get_series(self, tvdb_id: int) -> dict[str, Any] | None:
        resp: list[dict[str, Any]] | WebRuntimeError = await self.get(
            "/api/v3/series", params={"tvdbId": tvdb_id}
        )
        if isinstance(resp, WebRuntimeError):
            self.log.error(resp.message)
            return None
        if not resp:
            return None
        return resp[0]

    async def get_episodes(self, series_id: int) -> list[dict[str, Any]] | WebRuntimeError:
        return await self.get("/api/v3/episode", params={"seriesId": series_id})

    async def get_episode(self, episode_id: int) -> dict[str, Any] | WebRuntimeError:
        return await self.get(f"/api/v3/episode/{episode_id}")

    async def tv_add(
        self,
        ctx: commands.Context,
        tvdb_id: int,
    ) -> bool:
        add_series = await self.find_and_add(tvdb_id)

        if isinstance(add_series, AlreadyAddedError):
            await ctx.send(f"❌ **{add_series.show_details['title']}** already added")
            return False

        if not add_series or isinstance(add_series, WebRuntimeError):
            self.log.error("Error while adding TV show TVDB ID %s", tvdb_id)
            if isinstance(add_series, WebRuntimeError):
                self.log.error(add_series.text)
            await ctx.send(
                (
                    f"❌ No show found with TVDB ID `{tvdb_id}`.\n"
                    "🔎 Please check [TheTVDB](https://www.thetvdb.com)."
                )
            )
            return False

        await ctx.send(
            f"✅ Added TV show: **{add_series['title']}** to folder "
            f"`{self.shorten_path(add_series['rootFolderPath'])}`"
        )
        return True

    async def tv_download_queue(self, ctx: commands.Context) -> None:
        items = await self.queue()
        shows: dict[str, list[str]] = {}

        async def episode_info(ep_id: int) -> tuple[str, str]:
            ep = await self.get_episode(ep_id)
            if isinstance(ep, WebRuntimeError):
                return ("Unknown Show", "Unknown Episode")
            series = ep.get("series", {})
            title = series.get("title", "Unknown Show")
            tvdb_id = series.get("tvdbId", "-")
            ep_title = ep.get("title", "Untitled")
            season = int(ep.get("seasonNumber", 0))
            episode = int(ep.get("episodeNumber", 0))
            return (f"{title} - tvdbId:{tvdb_id}", f"S{season:02}E{episode:02} **{ep_title}**")

        if isinstance(items, WebRuntimeError):
            self.log.error(items.message)
            await ctx.send("Sonarr queue fetch failed.")
            return

        for q in items:
            if q.get("sizeleft", 0) <= 0:
                continue
            size = float(q.get("size", 0))
            sizeleft = float(q.get("sizeleft", 0))
            progress = 0.0 if size <= 0 else 100.0 * (size - sizeleft) / size
            ep_id = q.get("episodeId")
            show_title, ep_line = (
                await episode_info(int(ep_id)) if ep_id else ("Unknown", "Episode")
            )
            shows.setdefault(show_title, []).append(f"{ep_line} ({progress:.1f}%)")

        if not shows:
            await ctx.send("No active Sonarr downloads.")
            return

        download_embeds: list[Embed] = []
        for show, lines in shows.items():
            emb = Embed(title=show, description="\n".join(lines), color=0x3498DB)
            download_embeds.append(emb)
        await self._send_embeds_in_batches(ctx, download_embeds)

    async def tv_lookup(self, ctx: commands.Context, query: str):
        results = await self.lookup(query)
        if isinstance(results, WebRuntimeError):
            self.log.error("Sonarr search failed: %s", results.message)
            await ctx.send(f"Sonarr search failed: {results.message}")
            return

        if not results:
            await ctx.send(f"❌ No results for `{query}`")
            return

        search_embeds: list[Embed] = []
        results.sort(key=lambda r: r.get("year") or 0, reverse=True)
        for r in results[:20]:
            title = r.get("title", "Untitled")
            year = r.get("year", "?")
            tvdbid = r.get("tvdbId", "-")
            status = r.get("status", "-")
            overview = r.get("overview", "-")
            title_slug = r.get("titleSlug", "-")

            emb = Embed(
                title=f"{title} ({year})",
                description=overview[:1000],
                color=0x9B59B6,
            )
            emb.add_field(name="TVDB", value=f"`{str(tvdbid)}`", inline=False)
            emb.add_field(name="Status", value=str(status))
            emb.add_field(name="Title Slug", value=title_slug, inline=False)

            if r.get("remotePoster"):
                emb.set_thumbnail(url=r.get("remotePoster"))
            search_embeds.append(emb)
        await self._send_embeds_in_batches(ctx, search_embeds)

    async def tv_show(self, ctx: commands.Context, tvdb_id: int):
        class SeasonData(TypedDict):
            total: int
            downloaded: int
            eps: list[dict[str, Any]]

        series = await self.get_series(tvdb_id)
        if not series:
            await ctx.send(f"❌ No show found with TVDB ID {tvdb_id}.")
            return

        series_id = series.get("id")
        if not isinstance(series_id, int):
            await ctx.send("❌ Sonarr returned invalid series data.")
            return

        episodes = await self.get_episodes(series_id)
        if isinstance(episodes, WebRuntimeError):
            await ctx.send(f"❌ Failed to load episodes: {episodes.message}")
            return

        queue_items = await self.queue()

        seasons: dict[int, SeasonData] = {}
        for ep in episodes:
            season = int(ep.get("seasonNumber", 0))
            if season not in seasons:
                seasons[season] = {"total": 0, "downloaded": 0, "eps": []}
            seasons[season]["total"] += 1
            if ep.get("hasFile"):
                seasons[season]["downloaded"] += 1
            seasons[season]["eps"].append(ep)

        msg_lines = [f"📺 **{series['title']}**"]

        for season, stats in sorted(seasons.items()):
            if season == 0 and stats["downloaded"] == 0:
                continue
            msg_lines.append(
                f"Season {season}: {stats['downloaded']}/{stats['total']} episodes downloaded"
            )

        if isinstance(queue_items, WebRuntimeError):
            msg_lines.append(f"⚠️ Warning: failed to fetch Sonarr queue: {queue_items.message}")
        else:
            downloading: list[str] = []
            for q in queue_items:
                if q.get("sizeleft", 0) <= 0:
                    continue
                episode_id = q.get("episodeId")
                if not episode_id:
                    continue
                episode = await self.get_episode(int(episode_id))
                if isinstance(episode, WebRuntimeError):
                    continue
                if episode.get("seriesId") != series_id:
                    continue
                season = int(episode.get("seasonNumber", 0))
                number = int(episode.get("episodeNumber", 0))
                title = episode.get("title", "Untitled")
                size = float(q.get("size", 0))
                sizeleft = float(q.get("sizeleft", 0))
                progress = 0.0 if size <= 0 else 100.0 * (size - sizeleft) / size
                downloading.append(f"S{season:02}E{number:02} {title} ({progress:.1f}%)")

            if downloading:
                msg_lines.append("")
                msg_lines.append("📥 Currently Downloading:")
                msg_lines.extend(downloading)

        await ctx.send("\n".join(msg_lines))
