"""Standalone VidNest movie/TV provider.

VidNest (https://vidnest.fun) is an embed-API aggregator. Its movie/TV
endpoints ("hollymoviehd" namespace) are backed by the goodstream.cc /
hlmv.tripplestream.online family — a genuinely different source from
megaplay. The anime endpoints just relay megaplay, so this provider only
handles movies and TV shows.

Flow:
  GET new.vidnest.fun/hollymoviehd/{movie|tv}/{tmdbId}[/{s}/{e}]
      -> {"data": "<custom-alphabet base64>", "encrypted": true}
  Decrypt: base64 decode using the static alphabet as the index table.
      -> {"streams": [{url, type: mp4|hls, headers}]}

The MAIN stream is a direct MP4 (no referer needed to fetch data). HLS
playlists need the Referer header, so they run through the local proxy.
"""

import json
import re
from typing import Optional
from urllib.parse import urlparse

from anime_watch.core import SESSION, SCRAPE_TIMEOUT
from anime_watch.models import SearchResult, Episode, StreamSource
from .base import BaseProvider
from .tmdb_search import search_movies, get_tv_episodes
from .anikoto import _http_get, _proxy_hls

VIDNEST_BACKEND = "https://new.vidnest.fun"
_ALPHABET = "RB0fpH8ZEyVLkv7c2i6MAJ5u3IKFDxlS1NTsnGaqmXYdUrtzjwObCgQP94hoeW+/="


def _vidnest_decrypt(data: str) -> str:
    """Custom-alphabet base64: the 64-char alphabet IS the decode table."""
    idx = {c: i for i, c in enumerate(_ALPHABET[:64])}
    out = bytearray()
    n = len(data)
    for t in range(0, n, 4):
        chunk = (data[t:t + 4] + "====")[:4]
        d = [idx.get(c, 64) for c in chunk]
        out.append(((d[0] << 2) | (d[1] >> 4)) & 0xFF)
        if d[2] != 64:
            out.append((((d[1] & 15) << 4) | (d[2] >> 2)) & 0xFF)
        if d[3] != 64:
            out.append((((d[2] & 3) << 6) | d[3]) & 0xFF)
    return out.decode("utf-8")


class VidNestProvider(BaseProvider):
    name = "VidNest"
    slug = "vidnest"
    url = "https://vidnest.fun"
    category = "movies"

    def get_supported_qualities(self) -> list[str]:
        return ["1080p", "720p", "best"]

    def get_supported_audio(self) -> list[str]:
        return ["sub"]

    def search(self, query: str) -> list[SearchResult]:
        results = []
        for mr in search_movies(query):
            results.append(SearchResult(
                title=f"{mr.title} ({mr.year})" if mr.year else mr.title,
                url=f"https://vidnest.fun/movie/{mr.tmdb_id}",
                site_name=self.name,
                image=mr.poster or "",
                data={"tmdb_id": mr.tmdb_id, "media_type": mr.media_type},
            ))
        return results

    def get_episodes(self, result: SearchResult) -> list[Episode]:
        tmdb_id = (result.data or {}).get("tmdb_id")
        media_type = (result.data or {}).get("media_type")
        if not tmdb_id:
            return []
        if media_type == "tv":
            eps = get_tv_episodes(int(tmdb_id))
            for ep in eps:
                ep.site_name = self.name
            return eps
        an = result.title.split(" (")[0].strip()
        return [Episode(
            title=an,
            url=result.url,
            number="1",
            site_name=self.name,
            anime_name=an,
            data={"tmdb_id": int(tmdb_id), "media_type": "movie"},
        )]

    def _fetch_streams(self, tmdb_id: int, media_type: str,
                       season: Optional[int] = None,
                       episode: Optional[int] = None) -> Optional[list[dict]]:
        if media_type == "movie":
            url = f"{VIDNEST_BACKEND}/hollymoviehd/movie/{tmdb_id}"
        else:
            url = f"{VIDNEST_BACKEND}/hollymoviehd/tv/{tmdb_id}/{season}/{episode}"
        resp = _http_get(url, timeout=SCRAPE_TIMEOUT)
        if resp.status_code != 200:
            return None
        try:
            body = resp.json()
        except json.JSONDecodeError:
            return None
        if not body.get("encrypted") or not body.get("data"):
            return None
        try:
            dec = json.loads(_vidnest_decrypt(body["data"]))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None
        return dec.get("streams") or None

    def extract_stream(self, episode: Episode, audio_pref: str = "sub",
                       quality_pref: str = "best") -> Optional[StreamSource]:
        data = episode.data or {}
        tmdb_id = data.get("tmdb_id")
        media_type = data.get("media_type", "movie")
        if "season" in data and "episode" in data:
            media_type = "tv"
        if not tmdb_id:
            return None
        streams = self._fetch_streams(
            int(tmdb_id), media_type,
            data.get("season"), data.get("episode"),
        )
        if not streams:
            return None

        # Prefer the direct MP4 (no referer needed for data, native play).
        mp4 = next((st for st in streams
                    if st.get("type") == "mp4" and st.get("url")), None)
        if mp4:
            headers = mp4.get("headers") or {}
            return StreamSource(
                url=mp4["url"],
                site_name=self.name,
                quality=quality_pref,
                is_direct=True,
                headers=headers or None,
                subtitles=None,
            )

        # Fall back to HLS via the local referer proxy.
        hls = next((st for st in streams
                    if st.get("type") == "hls" and st.get("url")), None)
        if hls:
            headers = hls.get("headers") or {}
            referer = headers.get("Referer", "https://goodstream.cc/")
            proxy_url, proxy_server = _proxy_hls(
                hls["url"], referer, urlparse(hls["url"]).netloc)
            return StreamSource(
                url=proxy_url or hls["url"],
                site_name=self.name,
                quality=quality_pref,
                is_direct=True,
                headers={"Referer": referer},
                subtitles=None,
                proxy_server=proxy_server,
            )
        return None
