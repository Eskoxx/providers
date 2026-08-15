"""Search fallback ladder for providers that key on AniList ids.

AniList GraphQL (graphql.anilist.co) goes down frequently. This module
provides a fallback chain that still resolves anilist_ids:

  1. Kitsu REST search  -> ani.zip kitsu_id  -> anilist_id (+ episode count)
  2. anikoto scrape     -> episode data-mal -> ani.zip mal_id -> anilist_id
  3. local disk cache   (repeat searches work even with all APIs down)

ani.zip is a cross-database mapping service (ids + episode counts) and is
historically the most reliable piece of this chain. Kitsu is a public
no-key REST API. anikoto is a pure scrape (no external API dependency).
"""

import json
import os
import threading
import time
from typing import Callable, Optional

import requests

from anime_watch.core import SESSION, SCRAPE_TIMEOUT

ANILIST_API = "https://graphql.anilist.co"
ANI_ZIP_API = "https://api.ani.zip/mappings"
KITSU_API = "https://kitsu.io/api/edge/anime"

_CACHE_TTL = 30 * 24 * 3600
_CACHE_LOCK = threading.Lock()
_search_cache: dict = {}
_ep_count_cache: dict = {}
_cache_loaded = False

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


def _cache_path() -> str:
    return os.path.join(os.path.expanduser("~"), ".cache", "anime_watch",
                        "search_fallback.json")


def _load_cache():
    global _search_cache, _ep_count_cache, _cache_loaded
    if _cache_loaded:
        return
    _cache_loaded = True
    try:
        with open(_cache_path(), encoding="utf-8") as f:
            data = json.load(f)
        _search_cache = data.get("search", {})
        _ep_count_cache = data.get("episodes", {})
    except (OSError, ValueError):
        pass


def _save_cache():
    try:
        os.makedirs(os.path.dirname(_cache_path()), exist_ok=True)
        tmp = _cache_path() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"search": _search_cache, "episodes": _ep_count_cache}, f)
        os.replace(tmp, _cache_path())
    except OSError:
        pass


# ---------- AniList (primary, used by the providers themselves) ----------

def search_anilist(query: str) -> list[dict]:
    """AniList GraphQL search -> [{title, anilist_id, episodes, year, image}]."""
    results = []
    try:
        r = SESSION.post(
            ANILIST_API,
            json={"query": SEARCH_QUERY, "variables": {"search": query}},
            timeout=15,
        )
        if r.status_code != 200:
            return results
        for media in r.json().get("data", {}).get("Page", {}).get("media", []):
            titles = media.get("title", {})
            title = titles.get("romaji") or titles.get("english") or "Unknown"
            aid = media.get("id")
            if not aid:
                continue
            year = (media.get("startDate") or {}).get("year", "") or ""
            results.append({
                "title": title,
                "anilist_id": str(aid),
                "episodes": media.get("episodes") or 0,
                "year": year,
                "image": media.get("coverImage", {}).get("large", "") or "",
            })
    except requests.RequestException:
        pass
    return results


# ---------- Kitsu fallback ----------

def _kitsu_search(query: str) -> list[dict]:
    """Kitsu REST search -> ani.zip kitsu_id -> anilist_id."""
    results = []
    try:
        r = SESSION.get(
            KITSU_API,
            params={"filter[text]": query, "page[limit]": 20},
            timeout=SCRAPE_TIMEOUT,
        )
        if r.status_code != 200:
            return results
        for item in r.json().get("data", [])[:20]:
            kitsu_id = item.get("id")
            if not kitsu_id:
                continue
            attrs = item.get("attributes", {})
            titles = attrs.get("titles") or {}
            title = titles.get("en") or attrs.get("canonicalTitle") or "Unknown"
            year = attrs.get("startDate") or ""
            if year:
                year = year[:4]
            try:
                m = SESSION.get(f"{ANI_ZIP_API}?kitsu_id={kitsu_id}", timeout=SCRAPE_TIMEOUT)
                if m.status_code != 200:
                    continue
                data = m.json()
                aid = (data.get("mappings") or {}).get("anilist_id")
                if not aid:
                    continue
                results.append({
                    "title": title,
                    "anilist_id": str(aid),
                    "episodes": data.get("episodeCount") or 0,
                    "year": year,
                    "image": (attrs.get("posterImage") or {}).get("original") or "",
                })
            except requests.RequestException:
                continue
    except requests.RequestException:
        pass
    return results


# ---------- anikoto scrape fallback ----------

def _anikoto_search(query: str) -> list[dict]:
    """anikoto scrape -> episode data-mal -> ani.zip mal_id -> anilist_id.

    anikoto's search results don't carry anilist ids, but the first
    episode's ajax entry has a data-mal attribute; ani.zip converts it."""
    results = []
    try:
        from .anikoto import AnikotoProvider
        prov = AnikotoProvider()
        found = prov.search(query)
        if not found:
            return results
        q_words = [w for w in query.lower().split() if len(w) >= 3]
        if q_words:
            found.sort(key=lambda r: sum(
                1 for w in q_words if w in r.title.lower()), reverse=True)
        # Resolve each candidate via ani.zip and prefer the entry with the
        # most episodes — anikoto's ordering can surface OVAs/specials first.
        best = None
        for res in found[:5]:
            eps = prov.get_episodes(res)
            if not eps:
                continue
            mal_id = (eps[0].data or {}).get("mal_id")
            if not mal_id:
                continue
            try:
                m = SESSION.get(f"{ANI_ZIP_API}?mal_id={mal_id}", timeout=SCRAPE_TIMEOUT)
                if m.status_code != 200:
                    continue
                data = m.json()
                aid = (data.get("mappings") or {}).get("anilist_id")
                if not aid:
                    continue
                count = data.get("episodeCount") or 0
                if best is None or count > best["episodes"]:
                    best = {
                        "title": res.title.split(" (")[0].strip(),
                        "anilist_id": str(aid),
                        "episodes": count,
                        "year": "",
                        "image": res.image or "",
                    }
            except requests.RequestException:
                continue
        if best:
            results.append(best)
    except Exception:
        pass
    return results


# ---------- cache ----------

def cached_search(slug: str, query: str) -> list[dict]:
    _load_cache()
    entry = _search_cache.get(slug, {}).get(query.lower())
    if entry and time.time() - entry.get("ts", 0) < _CACHE_TTL:
        return list(entry.get("results", []))
    return []


def remember_search(slug: str, query: str, results: list[dict]) -> None:
    _load_cache()
    with _CACHE_LOCK:
        _search_cache.setdefault(slug, {})[query.lower()] = {
            "ts": time.time(), "results": results,
        }
        _save_cache()


def cached_episode_count(slug: str, anilist_id: str) -> int:
    _load_cache()
    entry = _ep_count_cache.get(slug, {}).get(str(anilist_id))
    if entry and time.time() - entry.get("ts", 0) < _CACHE_TTL:
        return int(entry.get("count") or 0)
    return 0


def remember_episode_count(slug: str, anilist_id: str, count: int) -> None:
    if count <= 0:
        return
    _load_cache()
    with _CACHE_LOCK:
        _ep_count_cache.setdefault(slug, {})[str(anilist_id)] = {
            "ts": time.time(), "count": count,
        }
        _save_cache()


def resolve_episode_count(slug: str, anilist_id: str) -> int:
    """Episode count for an anilist id from ani.zip (reliable), then cache."""
    count = cached_episode_count(slug, anilist_id)
    if count:
        return count
    try:
        m = SESSION.get(f"{ANI_ZIP_API}?anilist_id={anilist_id}", timeout=SCRAPE_TIMEOUT)
        if m.status_code == 200:
            data = m.json()
            count = data.get("episodeCount") or 0
            remember_episode_count(slug, anilist_id, count)
    except requests.RequestException:
        pass
    return count


# ---------- ladder ----------

def search_ladder(slug: str, query: str) -> list[dict]:
    """Full fallback ladder. Returns anilist-keyed result dicts or []."""
    for fn in (_kitsu_search, _anikoto_search):
        try:
            results = fn(query)
        except Exception:
            results = []
        if results:
            seen = set()
            deduped = []
            for it in results:
                aid = it.get("anilist_id")
                if aid in seen:
                    continue
                seen.add(aid)
                deduped.append(it)
            if deduped:
                remember_search(slug, query, deduped)
                return deduped
    return cached_search(slug, query)
