"""Standalone MegaPlay anime provider.

MegaPlay (https://megaplay.buzz) is the embed backend behind anikoto's
Vidstream/HD-1 servers. It publishes its own public API (see
https://megaplay.buzz/api): stream embeds at
megaplay.buzz/stream/{ani|mal}/{id}/{ep}/{lang}, then
/stream/getSources?id={file_id} for the HLS master. No anikoto scraping
involved — catalog discovery goes through AniList's public GraphQL API.

The HLS segments need Referer: https://megaplay.buzz/ and are PNG-wrapped
MPEG-TS, so streams run through the same local proxy anikoto uses.
"""

import json
import re
from typing import Optional
from urllib.parse import urlparse

import requests

from anime_watch.models import SearchResult, Episode, StreamSource
from anime_watch.core import SESSION, SCRAPE_TIMEOUT
from .base import BaseProvider
from .anikoto import _http_get, _pick_hls_variant, _proxy_hls
from .searchfallback import search_anilist, search_ladder, resolve_episode_count

ANILIST_API = "https://graphql.anilist.co"
MEGAPLAY_REFERER = "https://megaplay.buzz/"

SEARCH_QUERY = """
query ($search: String) {
  Page(page: 1, perPage: 20) {
    media(search: $search, type: ANIME) {
      id
      title { romaji english }
      episodes
      coverImage { large }
      startDate { year }
      format
    }
  }
}
"""

MEDIA_QUERY = """
query ($id: Int) {
  Media(id: $id) {
    episodes
    nextAiringEpisode { episode }
  }
}
"""


class MegaPlayProvider(BaseProvider):
    name = "MegaPlay"
    slug = "megaplay"
    url = "https://megaplay.buzz"
    category = "anime"

    def get_supported_qualities(self) -> list[str]:
        return ["1080p", "720p", "480p", "best"]

    def get_supported_audio(self) -> list[str]:
        return ["sub", "dub"]

    def search(self, query: str) -> list[SearchResult]:
        def _build(items: list) -> list[SearchResult]:
            out = []
            for it in items:
                aid = it.get("anilist_id")
                if not aid:
                    continue
                year = it.get("year") or ""
                title = it.get("title") or "Unknown"
                display = f"{title} ({year})" if year else title
                out.append(SearchResult(
                    title=display,
                    url=f"{self.url}/stream/ani/{aid}/1/sub",
                    site_name=self.name,
                    image=it.get("image") or "",
                    data={"anilist_id": str(aid), "episodes": it.get("episodes") or 0,
                          "year": year},
                ))
            return out

        results = _build(search_anilist(query))
        if not results:
            # AniList is down — Kitsu, anikoto scrape, then disk cache.
            results = _build(search_ladder(self.slug, query))
        return results

    def get_episodes(self, result: SearchResult) -> list[Episode]:
        aid = result.data.get("anilist_id", "")
        if not aid:
            return []
        try:
            total = int(result.data.get("episodes") or 0)
        except (ValueError, TypeError):
            total = 0
        if total <= 0:
            # Airing shows report null episodes — resolve from ani.zip
            # (reliable cross-database service), then the disk cache.
            total = resolve_episode_count(self.slug, aid)
        if total <= 0:
            total = 12
        an = result.title.split(" (")[0].strip()
        episodes = []
        for n in range(1, total + 1):
            episodes.append(Episode(
                title=f"Episode {n}",
                url=result.url,
                number=str(n),
                site_name=self.name,
                anime_name=an,
                data={"anilist_id": aid, "ep_num": n},
            ))
        return episodes

    def extract_stream(self, episode: Episode, audio_pref: str = "sub",
                       quality_pref: str = "best") -> Optional[StreamSource]:
        aid = episode.data.get("anilist_id", "")
        ep = episode.data.get("ep_num", 0)
        if not ep:
            try:
                ep = int(episode.number or 0)
            except (ValueError, TypeError):
                ep = 0
        if not aid or not ep:
            return None
        lang = "dub" if audio_pref == "dub" else "sub"
        embed_url = f"{self.url}/stream/ani/{aid}/{ep}/{lang}"
        try:
            resp = _http_get(
                embed_url,
                headers={"Referer": MEGAPLAY_REFERER},
                timeout=SCRAPE_TIMEOUT,
            )
            if resp.status_code != 200:
                return None
            m = re.search(r"File\s+(\d+)\s+-", resp.text)
            if not m:
                return None
            file_id = m.group(1)

            src = _http_get(
                f"{self.url}/stream/getSources?id={file_id}",
                headers={"Referer": embed_url, "X-Requested-With": "XMLHttpRequest"},
                timeout=SCRAPE_TIMEOUT,
            )
            if src.status_code != 200:
                return None
            data = src.json()
            sources = data.get("sources", {})
            m3u8_url = sources.get("file", "") if isinstance(sources, dict) else ""
            if not m3u8_url:
                return None

            if quality_pref != "best":
                best_variant = _pick_hls_variant(m3u8_url, quality_pref)
                if best_variant:
                    m3u8_url = best_variant

            proxy_server = None
            proxy_url, proxy_server = _proxy_hls(
                m3u8_url, MEGAPLAY_REFERER, urlparse(m3u8_url).netloc)
            if proxy_url:
                m3u8_url = proxy_url

            subtitles = [
                {"url": t["file"], "label": t.get("label", ""),
                 "lang": t.get("label", "").lower()}
                for t in data.get("tracks", [])
                if t.get("kind") == "captions" and t.get("file")
            ]
            return StreamSource(
                url=m3u8_url,
                site_name=self.name,
                quality=quality_pref,
                is_direct=True,
                headers={"Referer": MEGAPLAY_REFERER},
                subtitles=subtitles or None,
                proxy_server=proxy_server,
            )
        except (requests.RequestException, json.JSONDecodeError, KeyError, ValueError):
            return None
