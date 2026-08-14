"""YouTube Music provider: search tracks/albums/playlists and play them.

Search/metadata come from ytmusicapi (the Python equivalent of youtubei.js);
playback hands the watch URL to mpv, which resolves the stream via yt-dlp —
the same backend youtube-music-cli uses, but natively in Python.
"""

from __future__ import annotations

import os as _os
import subprocess as _sp
from typing import Optional

from anime_watch.models import SearchResult, Episode, StreamSource, MediaResult
from .base import BaseProvider

_YT_BASE = "https://music.youtube.com"


def _norm_title(title: str) -> str:
    import re as _re
    t = title.lower().strip()
    t = _re.sub(r"\b(official video|official audio|music video|lyrics|lyric video|audio)\b", "", t)
    t = _re.sub(r"[^a-z0-9 ]+", "", t)
    return _re.sub(r"\s+", " ", t).strip()


def ytmusic_client():
    """Import ytmusicapi from the VENDORED copy shipped with the providers
    feed (providers/ytmusicapi) — pure Python, no pip/APK needed on Android.
    Falls back to any pip-installed copy if the vendored one is absent."""
    import sys as _sys
    _dir = _os.path.dirname(_os.path.abspath(__file__))
    if _dir not in _sys.path:
        _sys.path.insert(0, _dir)
    from ytmusicapi import YTMusic
    return YTMusic()


def _yt_client():
    return ytmusic_client()


def _fmt_duration(seconds: int) -> str:
    if not seconds:
        return ""
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


class YouTubeMusicProvider(BaseProvider):
    name = "ytmusic"
    slug = "ytmusic"
    url = _YT_BASE
    category = "music"

    _ym = None

    def _client(self):
        if self._ym is None:
            self._ym = _yt_client()
        return self._ym

    def get_supported_qualities(self) -> list[str]:
        return ["best"]

    def get_supported_audio(self) -> list[str]:
        return ["sub"]

    def search(self, query: str) -> list[SearchResult]:
        results: list[SearchResult] = []
        try:
            client = self._client()
            # Index video-format results by title so each song can carry its
            # real music video (16:9) alongside the audio/art-track version.
            mv_by_title: dict[str, str] = {}
            for v in client.search(query, filter="videos", limit=20) or []:
                vid = v.get("videoId")
                key = _norm_title(v.get("title", ""))
                if vid and key and key not in mv_by_title:
                    mv_by_title[key] = vid
            for r in client.search(query, filter="songs", limit=15):
                video_id = r.get("videoId")
                if not video_id:
                    continue
                title = r.get("title", "")
                artist = (r.get("artists") or [{}])[0].get("name", "")
                album = (r.get("album") or {}).get("name", "")
                duration = _fmt_duration(r.get("duration_seconds") or 0)
                display = f"{title} — {artist}" if artist else title
                mv_id = mv_by_title.get(_norm_title(title))
                results.append(SearchResult(
                    title=display,
                    url=f"{_YT_BASE}/watch?v={video_id}",
                    site_name=self.name,
                    image=(r.get("thumbnails") or [{}])[0].get("url", "") if r.get("thumbnails") else "",
                    data={
                        "kind": "song",
                        "video_id": video_id,
                        "mv_id": mv_id or "",
                        "artist": artist,
                        "album": album,
                        "duration": duration,
                        "title": title,
                    },
                ))
            if not results:
                # Fall back to generic search (covers playlists/albums as tracks).
                for r in client.search(query, limit=10):
                    video_id = r.get("videoId")
                    if not video_id:
                        continue
                    title = r.get("title", "")
                    artist = (r.get("artists") or [{}])[0].get("name", "")
                    results.append(SearchResult(
                        title=f"{title} — {artist}" if artist else title,
                        url=f"{_YT_BASE}/watch?v={video_id}",
                        site_name=self.name,
                        data={"kind": "song", "video_id": video_id, "artist": artist,
                              "title": title},
                    ))
        except Exception:
            pass
        return results

    def get_episodes(self, result: SearchResult) -> list[Episode]:
        data = result.data or {}
        an = result.title
        if data.get("kind") == "song":
            eps = [Episode(
                title=f"♫ {an}",
                url=result.url,
                number="1",
                site_name=self.name,
                anime_name=an,
                data={"kind": "song", "video_id": data.get("video_id", "")},
            )]
            if data.get("mv_id"):
                eps.append(Episode(
                    title=f"▶ {an} (Video)",
                    url=f"{_YT_BASE}/watch?v={data['mv_id']}",
                    number="1v",
                    site_name=self.name,
                    anime_name=an,
                    data={"kind": "video", "video_id": data["mv_id"]},
                ))
            return eps
        return []

    @staticmethod
    def _resolve_direct(url: str, fmt: str) -> Optional[list[str]]:
        """Resolve the watch URL to direct media streams once via yt-dlp, so
        mpv plays them with no yt-dlp involvement (faster startup, less load)."""
        try:
            r = _sp.run(["yt-dlp", "-g", "-f", fmt, "--no-warnings", url],
                        capture_output=True, text=True, timeout=30)
            if r.returncode != 0:
                return None
            lines = [l for l in r.stdout.strip().splitlines() if l.strip()]
            return lines or None
        except Exception:
            return None

    @staticmethod
    def _probe_ok(url: str) -> bool:
        """YouTube randomly hands out dead/half-dead signed URLs: some answer
        a tiny range probe but 403 the full stream. Probe the FULL range
        (bytes=0-) — exactly what the player requests."""
        import urllib.request as _ur
        try:
            req = _ur.Request(url, method="GET", headers={"Range": "bytes=0-"})
            with _ur.urlopen(req, timeout=5) as resp:
                return resp.status in (200, 206)
        except Exception:
            return False

    def _resolve_playable(self, url: str, fmt: str, tries: int = 5) -> Optional[list[str]]:
        """Resolve with retries until the signed URLs actually fetch (200/206)."""
        for _ in range(tries):
            urls = self._resolve_direct(url, fmt)
            if urls and all(self._probe_ok(u) for u in urls):
                return urls
        return None

    def resolve_suggestion(self, episode: Episode) -> Optional[StreamSource]:
        """Cheap resolution for autoplay suggestions: ONE attempt and one
        probe — no retry storm. Dead signatures skip to the next candidate."""
        urls = self._resolve_direct(episode.url, "best[height<=720]/best")
        if urls and self._probe_ok(urls[0]):
            return StreamSource(
                url=urls[0], site_name=self.name, quality="best",
                is_direct=True, extra_mpv_args=None)
        return None

    def extract_stream(self, episode: Episode, audio_pref: str = "sub",
                       quality_pref: str = "best") -> Optional[StreamSource]:
        if not episode.url:
            return None
        if _os.environ.get("ANDROID_ROOT") is not None:
            # Android: the player activity is single-URL, so the DASH pair
            # (video + separate audio) is relayed through a local ffmpeg
            # mpegts proxy by the app (see _play_one_android). Pick h264/m4a
            # so the remux is stream-copy (no transcode). Fall back to a
            # single merged URL if the pair can't be resolved.
            urls = self._resolve_playable(
                episode.url,
                "bestvideo[ext=mp4][vcodec^=avc1][height<=1080]+bestaudio[ext=m4a]/best")
            if urls and len(urls) >= 2:
                return StreamSource(
                    url=urls[0],
                    site_name=self.name,
                    quality="best",
                    is_direct=True,
                    headers=None,
                    subtitles=None,
                    extra_mpv_args=[f"--audio-file={urls[1]}"],
                )
            urls = self._resolve_playable(episode.url, "best[height<=720]/best")
            return StreamSource(
                url=(urls[0] if urls else episode.url),
                site_name=self.name,
                quality="best",
                is_direct=True,
                headers=None,
                subtitles=None,
            )
        kind = (episode.data or {}).get("kind", "song")
        if kind == "song":
            # "Audio" version: play the song's art-track visual (square album
            # art) with the audio — sharpened to the 720p art format when
            # available (keeps the player window, controls and seeking, like
            # a music player).
            urls = self._resolve_playable(
                episode.url,
                "bestvideo[height<=1080][vcodec^=avc1]+bestaudio[ext=m4a]/"
                "bestvideo[height<=1080]+bestaudio/best")
            if urls and len(urls) >= 2:
                return StreamSource(
                    url=urls[0],
                    site_name=self.name,
                    quality="best",
                    is_direct=True,
                    headers=None,
                    subtitles=None,
                    extra_mpv_args=[f"--audio-file={urls[1]}"],
                )
            urls = self._resolve_playable(episode.url, "bestaudio/best")
            return StreamSource(
                url=(urls[0] if urls else episode.url),
                site_name=self.name,
                quality="best",
                is_direct=True,
                headers=None,
                subtitles=None,
                extra_mpv_args=["--vid=no"],
            )
        # Video version: prefer the full-resolution DASH video + its separate
        # audio stream (mpv merges them via --audio-file). Falls back to the
        # highest single merged format, then to mpv's own yt-dlp resolution.
        urls = self._resolve_playable(
            episode.url,
            "bestvideo[height<=1080][vcodec^=avc1]+bestaudio[ext=m4a]/"
            "bestvideo[height<=1080]+bestaudio/best")
        if urls and len(urls) >= 2:
            return StreamSource(
                url=urls[0],
                site_name=self.name,
                quality="best",
                is_direct=True,
                headers=None,
                subtitles=None,
                extra_mpv_args=[f"--audio-file={urls[1]}"],
            )
        urls = self._resolve_playable(episode.url, "best[height<=1080]/best")
        return StreamSource(
            url=(urls[0] if urls else episode.url),
            site_name=self.name,
            quality="best",
            is_direct=True,
            headers=None,
            subtitles=None,
        )

    def resolve(self, media: MediaResult, audio_pref: str = "sub",
                quality_pref: str = "best") -> Optional[StreamSource]:
        results = self.search(media.title)
        if not results:
            return None
        return self.extract_stream(self.get_episodes(results[0])[0], audio_pref, quality_pref)
