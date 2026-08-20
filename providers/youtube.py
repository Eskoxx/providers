"""YouTube provider: search regular YouTube videos and play them.

Search comes from the same innertube client ytmusicapi uses — generic search
with NO music filter, so trailers, movies, TV clips, music videos and
anything else on YouTube surface. Playback hands the watch URL to mpv,
which resolves the stream via yt-dlp (same backend as ytmusic).
"""

from __future__ import annotations

import os as _os
import json as _json
import subprocess as _sp
from typing import Optional

from anime_watch.models import SearchResult, Episode, StreamSource, MediaResult
from .base import BaseProvider

_YT_BASE = "https://www.youtube.com"


# mpv's ffmpeg HTTP demuxer sends "Icy-MetaData: 1" by default, which
# googlevideo answers with 403 — disable it or every resolved URL fails.
_MPV_EXTRAS = ["--demuxer-lavf-o=icy=0"]


def _fmt_duration(seconds) -> str:
    if not seconds:
        return ""
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


class YouTubeProvider(BaseProvider):
    name = "YouTube"
    slug = "youtube"
    url = _YT_BASE
    category = "video"

    def get_supported_qualities(self) -> list[str]:
        return ["best"]

    def get_supported_audio(self) -> list[str]:
        return ["sub"]

    def search(self, query: str) -> list[SearchResult]:
        results: list[SearchResult] = []
        try:
            # Real YouTube search via yt-dlp's ytsearch: (youtube.com's own
            # index, not the music backend — ytmusicapi only covers music).
            r = _sp.run(
                ["yt-dlp", "--flat-playlist", "--no-warnings", "-J", f"ytsearch20:{query}"],
                capture_output=True, text=True, timeout=30,
            )
            if r.returncode != 0:
                return results
            data = _json.loads(r.stdout)
            for e in data.get("entries", []) or []:
                video_id = e.get("id")
                if not video_id:
                    continue
                title = e.get("title", "")
                if not title:
                    continue
                channel = e.get("channel") or e.get("uploader") or ""
                duration = _fmt_duration(e.get("duration") or 0)
                display = f"{title} — {channel}" if channel else title
                if duration:
                    display += f" [{duration}]"
                results.append(SearchResult(
                    title=display,
                    url=f"{_YT_BASE}/watch?v={video_id}",
                    site_name=self.name,
                    image=(e.get("thumbnails") or [{}])[0].get("url", "") if e.get("thumbnails") else "",
                    data={
                        "video_id": video_id,
                        "title": title,
                        "channel": channel,
                        "duration": duration,
                    },
                ))
        except Exception:
            pass
        return results

    def get_episodes(self, result: SearchResult) -> list[Episode]:
        data = result.data or {}
        title = data.get("title") or result.title.split(" — ")[0]
        return [Episode(
            title=result.title,
            url=result.url,
            number="1",
            site_name=self.name,
            anime_name=title,
            data={
                "video_id": data.get("video_id", ""),
                "title": title,
                "channel": data.get("channel", ""),
                "duration": data.get("duration", ""),
            },
        )]

    @staticmethod
    def _resolve_direct(url: str, fmt: str,
                        client: str = "") -> Optional[list[str]]:
        """Resolve the watch URL to direct media streams once via yt-dlp, so
        mpv plays them with no yt-dlp involvement (faster startup, less load)."""
        args = ["yt-dlp", "-g", "-f", fmt, "--no-warnings"]
        if client:
            args += ["--extractor-args", f"youtube:player_client={client}"]
        args.append(url)
        try:
            r = _sp.run(args, capture_output=True, text=True, timeout=30)
            if r.returncode != 0:
                return None
            lines = [l for l in r.stdout.strip().splitlines() if l.strip()]
            return lines or None
        except Exception:
            return None

    @staticmethod
    def _probe_ok(url: str) -> bool:
        """YouTube randomly hands out dead signed URLs (~40% 403). A URL is
        stable once issued, so probe before handing it to the player.

        Probe with the FULL range (bytes=0-), not a tiny prefix: some
        signatures answer 206 for a small range but 403 the full stream —
        which is exactly what the player requests."""
        import urllib.request as _ur
        try:
            req = _ur.Request(url, method="GET", headers={"Range": "bytes=0-"})
            with _ur.urlopen(req, timeout=5) as resp:
                return resp.status in (200, 206)
        except Exception:
            return False

    def _resolve_playable(self, url: str, fmt: str, tries: int = 3) -> Optional[list[str]]:
        """Resolve with retries until the signed URLs actually fetch (200/206).

        YouTube's android client now hands out dead URLs (403); fall back to
        the web_embedded player client when the default client's URLs fail."""
        # YouTube's android client now hands out dead URLs (403). web_embedded
        # is the only client that produces live URLs — use it directly.
        for _ in range(tries):
            urls = self._resolve_direct(url, fmt, client="web_embedded")
            if urls and all(self._probe_ok(u) for u in urls):
                return urls
        return None

    def _best_merged(self, url: str, min_height: int = 720) -> Optional[str]:
        """Best MERGED (muxed) format with height >= min_height via yt-dlp
        format info. Merged files are A/V-synced by construction; progressive
        formats below the bar (e.g. 360p-only videos) are rejected so the
        caller falls back to the DASH pair instead."""
        try:
            r = _sp.run(["yt-dlp", "-J", "--no-warnings", url],
                        capture_output=True, text=True, timeout=30)
            if r.returncode != 0:
                return None
            data = _json.loads(r.stdout)
            merged = [f for f in data.get("formats", []) or []
                      if f.get("vcodec") not in (None, "none")
                      and f.get("acodec") not in (None, "none")
                      and f.get("height")]
            merged.sort(key=lambda f: (f.get("height") or 0, f.get("tbr") or 0), reverse=True)
            for f in merged:
                if (f.get("height") or 0) >= min_height and f.get("ext") == "mp4":
                    return f.get("url")
            for f in merged:
                if (f.get("height") or 0) >= min_height:
                    return f.get("url")
        except Exception:
            pass
        return None

    def _mux_relay(self, video_url: str, audio_url: str) -> tuple[str, object]:
        """Mux the DASH pair into ONE local mpegts stream via ffmpeg
        (-c copy, no transcode) and serve it over HTTP. The player gets a
        single muxed stream, so A/V sync is guaranteed at full resolution —
        the same approach the Android relay uses."""
        import socketserver as _ss
        import threading as _th
        from http.server import BaseHTTPRequestHandler, HTTPServer

        class _H(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "video/mp2t")
                self.end_headers()
                try:
                    p = _sp.Popen(
                        ["ffmpeg", "-loglevel", "error",
                         "-i", video_url, "-i", audio_url,
                         "-c", "copy", "-f", "mpegts", "pipe:1"],
                        stdout=_sp.PIPE, stderr=_sp.DEVNULL)
                    try:
                        while True:
                            chunk = p.stdout.read(65536)
                            if not chunk:
                                break
                            try:
                                self.wfile.write(chunk)
                            except (BrokenPipeError, ConnectionResetError, OSError):
                                break
                    finally:
                        try:
                            p.kill()
                        except Exception:
                            pass
                except Exception:
                    pass

            def log_message(self, *a):
                pass

        srv = HTTPServer(("127.0.0.1", 0), _H)
        _th.Thread(target=srv.serve_forever, daemon=True).start()
        return f"http://127.0.0.1:{srv.server_address[1]}/stream", srv

    def resolve_suggestion(self, episode) -> Optional[StreamSource]:
        """Cheap resolution for autoplay suggestions: ONE merged resolve and
        one probe — no retry storm, no extra calls. Dead signatures just skip
        to the next candidate (parallelized in the fetcher)."""
        # Probe until a full 1080p DASH pair is found — never fall back to
        # low progressive formats. Dead signatures just skip the candidate.
        fmt = ("bestvideo[height<=1080][vcodec^=avc1]+bestaudio[ext=m4a]/"
               "bestvideo[height<=1080]+bestaudio/best")
        for _ in range(3):
            urls = self._resolve_direct(episode.url, fmt)
            if urls and len(urls) >= 2 and self._probe_ok(urls[0]) and self._probe_ok(urls[1]):
                return StreamSource(
                    url=urls[0], site_name=self.name, quality="best",
                    is_direct=True,
                    extra_mpv_args=_MPV_EXTRAS + [f"--audio-file={urls[1]}"])
        return None

    def extract_stream(self, episode: Episode, audio_pref: str = "sub",
                       quality_pref: str = "best") -> Optional[StreamSource]:
        if not episode.url:
            return None
        if _os.environ.get("ANDROID_ROOT") is not None:
            # Android: the player activity is single-URL, so the DASH pair
            # (video + separate audio) is relayed through a local ffmpeg
            # mpegts proxy by the app. Pick h264/m4a so the remux is
            # stream-copy; fall back to a single merged URL.
            urls = self._resolve_playable(
                episode.url,
                "bestvideo[ext=mp4][vcodec^=avc1][height<=1080]+bestaudio[ext=m4a]/best")
            if urls and len(urls) >= 2:
                return StreamSource(
                    url=urls[0],
                    site_name=self.name,
                    quality="best",
                    is_direct=True,
                    extra_mpv_args=_MPV_EXTRAS + [f"--audio-file={urls[1]}"],
                )
            urls = self._resolve_playable(episode.url, "best[height<=720]/best")
            return StreamSource(
                url=(urls[0] if urls else episode.url),
                site_name=self.name,
                quality="best",
                is_direct=True,
            )
        # PC: h264 (avc1) DASH pair at full 1080p via --audio-file (old
        # method, no ffmpeg relay). Falls back to any 1080p, then merged.
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
                extra_mpv_args=_MPV_EXTRAS + [f"--audio-file={urls[1]}"],
            )
        urls = self._resolve_playable(episode.url, "best[height<=1080]/best")
        return StreamSource(
            url=(urls[0] if urls else episode.url),
            site_name=self.name,
            quality="best",
            is_direct=True,
            extra_mpv_args=_MPV_EXTRAS,
        )

    def resolve(self, media: MediaResult, audio_pref: str = "sub",
                quality_pref: str = "best") -> Optional[StreamSource]:
        results = self.search(media.title)
        if not results:
            return None
        return self.extract_stream(self.get_episodes(results[0])[0], audio_pref, quality_pref)
