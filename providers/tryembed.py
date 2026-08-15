import base64
import json
import logging
import re
from datetime import date, datetime
import socketserver
import threading
import urllib.parse
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import Optional

import requests

try:
    from curl_cffi.requests import Session as CurlSession
    HAS_CURL = True
except ImportError:
    HAS_CURL = False

from anime_watch.models import SearchResult, Episode, StreamSource
from .base import BaseProvider
from .searchfallback import search_anilist, search_ladder

logger = logging.getLogger(__name__)

ANILIST_API = "https://graphql.anilist.co"
ANI_ZIP_API = "https://api.ani.zip/mappings"
TRYEMBED_HOST = "https://tryembed.us.cc"

BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Sec-Ch-Ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
    "Connection": "keep-alive",
}

def _new_session():
    """curl_cffi (impersonation) when available, else plain requests."""
    if HAS_CURL:
        return CurlSession(impersonate="chrome124")
    s = requests.Session()
    s.headers.update(BROWSER_HEADERS)
    return s

def _unmask_png(data: bytes) -> bytes:
    """Strip a PNG shell from a media segment. The CDN wraps real MPEG-TS
    payload after the PNG's IEND chunk (1x1 transparent image trick) to
    block naive downloading; players choke on the PNG magic bytes."""
    if data[:1] != b"\x89" or data[:8] != b"\x89PNG\r\n\x1a\n":
        return data
    offset = 8
    while offset < len(data) - 8:
        chunk_len = int.from_bytes(data[offset:offset + 4], "big")
        if data[offset + 4:offset + 8] == b"IEND":
            return data[offset + 12:]
        offset += 12 + chunk_len
    return data


def _rewrite_playlist(data: bytes, playlist_url: str, server) -> bytes:
    """Rewrite an HLS playlist so every segment (absolute or relative) is
    fetched THROUGH this proxy, where the PNG shell is stripped. Without
    this the player hits the CDN directly and gets PNG-wrapped garbage."""
    from urllib.parse import quote
    base = f"http://127.0.0.1:{server.server_address[1]}"
    out = []
    for line in data.decode("utf-8", "replace").splitlines():
        sline = line.strip()
        if sline and not sline.startswith("#"):
            target = sline if sline.startswith("http") else \
                playlist_url.rsplit("/", 1)[0] + "/" + sline
            out.append(f"{base}/m3u8-proxy?url={quote(target, safe='')}")
        elif 'URI="' in line:
            def _sub(m):
                uri = m.group(1)
                target = uri if uri.startswith("http") else \
                    playlist_url.rsplit("/", 1)[0] + "/" + uri
                return f'URI="{base}/m3u8-proxy?url={quote(target, safe="")}"'
            out.append(re.sub(r'URI="([^"]*)"', _sub, line))
        else:
            out.append(line)
    return ("\n".join(out) + "\n").encode()


class _ProxyRequestHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        server = self.server  # type: ignore[attr-defined]
        try:
            s = server._session
            path = self.path

            if path.startswith("/m3u8-proxy"):
                from urllib.parse import urlparse, parse_qs
                qs = parse_qs(urlparse(path).query)
                target_url = qs.get("url", [""])[0]
                headers_raw = qs.get("headers", ["{}"])[0]
                try:
                    extra_headers = json.loads(headers_raw)
                except Exception:
                    extra_headers = {}
                if not target_url:
                    self.send_response(400)
                    self.end_headers()
                    return
                resp = s.get(target_url, headers=extra_headers, timeout=30)
                data = _unmask_png(resp.content)
                self.send_response(resp.status_code)
                self.send_header("Content-Type", "video/MP2T")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Connection", "close")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(data)
                return

            url = server._target_url + path
            resp = s.get(url, headers={"Referer": TRYEMBED_HOST, "Origin": TRYEMBED_HOST}, timeout=30)
            data = _unmask_png(resp.content)
            rewritten = False
            if data.startswith(b"#EXTM3U") or data.lstrip().startswith(b"#EXTM3U"):
                data = _rewrite_playlist(data, url, self.server)
                rewritten = True
            self.send_response(resp.status_code)
            if rewritten:
                self.send_header("Content-Type", "application/vnd.apple.mpegurl")
            elif data is resp.content:
                for key, val in resp.headers.items():
                    if key.lower() in ("content-type", "content-range", "accept-ranges"):
                        self.send_header(key, val)
            else:
                self.send_header("Content-Type", "video/MP2T")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            if resp.status_code == 200:
                self.wfile.write(data)
        except Exception as e:
            self.send_response(502)
            self.end_headers()
            self.wfile.write(str(e).encode())

    def log_message(self, *a):
        pass


class _ProxyServer(HTTPServer):
    def __init__(self, target_url: str, session: requests.Session):
        self._target_url = target_url.rstrip("/")
        self._session = session
        super().__init__(("127.0.0.1", 0), _ProxyRequestHandler)


def _start_proxy(target_url: str, session: requests.Session) -> tuple[str, HTTPServer]:
    server = _ProxyServer(target_url, session)
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    return f"http://127.0.0.1:{port}", server


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


class TryEmbedProvider(BaseProvider):
    name = "TryEmbed"
    slug = "tryembed"
    url = TRYEMBED_HOST
    category = "anime"

    def get_supported_qualities(self) -> list[str]:
        return ["1080p", "720p", "360p", "best"]

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
                    url=f"{TRYEMBED_HOST}/embed/anime/{aid}/1/sub",
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

    def _episode_category(self, ep_num, ep_data, default_season="Season 1"):
        key = str(ep_num)
        if key.lstrip('-').isdigit():
            season = ep_data.get("season", 1) if isinstance(ep_data.get("season"), int) else 1
            return f"Season {season}"
        return "Specials"

    def get_episodes(self, result: SearchResult) -> list[Episode]:
        aid = result.data.get("anilist_id", "")
        if not aid:
            m = re.search(r"/anime/(\d+)", result.url)
            if m:
                aid = m.group(1)
        if not aid:
            return []
        episodes = []
        try:
            s = _new_session()
            r = s.get(f"{ANI_ZIP_API}?anilist_id={aid}", timeout=15)
            if r.status_code == 200:
                data = r.json()
                eps = data.get("episodes", {})
                def sort_key(item):
                    k = item[0]
                    if str(k).lstrip('-').isdigit():
                        return (0, int(k))
                    return (1, str(k))
                for ep_num, ep_data in sorted(eps.items(), key=sort_key):
                    ep_title = ep_data.get("title", {}).get("en", "") if isinstance(ep_data.get("title"), dict) else (ep_data.get("title") or "")
                    cat = self._episode_category(ep_num, ep_data)
                    airdate_raw = ep_data.get("airDate") or ep_data.get("airdate") or ""
                    airdate_label = ""
                    if airdate_raw:
                        try:
                            ad = date.fromisoformat(airdate_raw)
                            today = date.today()
                            if ad > today:
                                airdate_label = f" [airs {airdate_raw}]"
                            elif ad == today:
                                airdate_label = " [airs today]"
                        except ValueError:
                            pass
                    title = f"Episode {ep_num}" + (f" - {ep_title}" if ep_title else "") + airdate_label
                    episodes.append(Episode(
                        title=title,
                        url=f"{TRYEMBED_HOST}/embed/anime/{aid}/{ep_num}/sub",
                        number=str(ep_num),
                        site_name=self.name,
                        anime_name=result.title.split(" (")[0].strip(),
                        category=cat,
                        data={"anilist_id": aid, "airdate": airdate_raw},
                    ))
            if not episodes:
                total = result.data.get("episodes", 0)
                if total:
                    for i in range(1, int(total) + 1):
                        episodes.append(Episode(
                            title=f"Episode {i}",
                            url=f"{TRYEMBED_HOST}/embed/anime/{aid}/{i}/sub",
                            number=str(i),
                            site_name=self.name,
                            anime_name=result.title,
                            data={"anilist_id": aid},
                        ))
        except Exception as e:
            logger.warning("TryEmbed get_episodes failed: %s", e)
        return episodes

    def extract_stream(self, episode: Episode, audio_pref: str = "sub", quality_pref: str = "best") -> Optional[StreamSource]:
        embed_url = episode.url
        if not embed_url.startswith("http"):
            embed_url = f"{TRYEMBED_HOST}{embed_url}" if embed_url.startswith("/") else embed_url
        try:
            s = _new_session()
            r = s.get(embed_url, timeout=15)
            if r.status_code != 200:
                return None

            raw_payload = re.search(r'window\.RAW_PAYLOAD="([^"]+)"', r.text)
            embed_nonce = re.search(r'window\.EMBED_NONCE="([^"]+)"', r.text)
            if not raw_payload or not embed_nonce:
                return None

            payload = json.loads(base64.b64decode(raw_payload.group(1)))
            meta = payload.get("meta", {})
            anilist_id = meta.get("anilist_id", "")
            episode_num = meta.get("episode", 1)
            audio = meta.get("audio", audio_pref)
            nonce = embed_nonce.group(1)

            api_url = f"{TRYEMBED_HOST}/api/stream_data"
            params = {
                "id": anilist_id,
                "episode": episode_num,
                "audio": audio,
                "nonce": nonce,
            }
            if audio_pref != "sub":
                params["audio"] = audio_pref

            api_resp = s.get(
                api_url,
                params=params,
                headers={
                    "X-Embed-Nonce": nonce,
                    "Referer": embed_url,
                    "Origin": TRYEMBED_HOST,
                    "Accept": "application/json, text/plain, */*",
                    "Accept-Language": "en-US,en;q=0.9",
                    "Sec-Fetch-Dest": "empty",
                    "Sec-Fetch-Mode": "cors",
                    "Sec-Fetch-Site": "same-origin",
                },
                timeout=30,
            )
            if api_resp.status_code != 200:
                return None

            stream_data = api_resp.json()
            providers = stream_data.get("providers", [])
            if not providers:
                return None

            target_quality = int(quality_pref.replace("p", "")) if quality_pref != "best" else 99999
            chosen_token = None
            chosen_quality = "unknown"
            for prov in providers:
                for q in prov.get("qualities", []):
                    qh = q.get("height", 0)
                    token = q.get("token", "")
                    if not (token and qh):
                        continue
                    if chosen_token is None and token:
                        chosen_token = token
                        chosen_quality = q.get("name", f"{qh}p")
                    if qh <= target_quality:
                        cur_q = chosen_quality.replace("p", "")
                        if cur_q.isdigit() and int(cur_q) < qh:
                            chosen_token = token
                            chosen_quality = q.get("name", f"{qh}p")
                        elif not cur_q.isdigit():
                            chosen_token = token
                            chosen_quality = q.get("name", f"{qh}p")

            if not chosen_token:
                return None

            m3u8_path = f"/s/{chosen_token}.m3u8"
            m3u8_url = f"{TRYEMBED_HOST}{m3u8_path}"

            proxy_base, proxy_server = _start_proxy(TRYEMBED_HOST, s)
            proxy_url = f"{proxy_base}{m3u8_path}"

            subtitles = []
            caption_sources = [
                stream_data.get("selectedProvider", {}).get("captions", []),
                stream_data.get("captions", []),
            ]
            for prov in stream_data.get("providers", []):
                if prov.get("captions"):
                    caption_sources.append(prov["captions"])
            seen = set()
            for src in caption_sources:
                for c in src:
                    url = c.get("url") or c.get("file", "")
                    if url and url not in seen:
                        seen.add(url)
                        subtitles.append({
                            "url": url,
                            "label": c.get("label", ""),
                            "lang": c.get("lang", c.get("label", "")).lower(),
                        })

            return StreamSource(
                url=proxy_url,
                site_name=self.name,
                quality=quality_pref if quality_pref != "best" else chosen_quality,
                is_direct=True,
                headers={"Referer": f"{TRYEMBED_HOST}/"},
                subtitles=subtitles or None,
                proxy_server=proxy_server,
            )
        except Exception as e:
            logger.warning("TryEmbed extract_stream failed: %s", e)
            return None
