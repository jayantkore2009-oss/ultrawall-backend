"""
UltraWall Pinterest scraper backend.

Architecture rules:
- No third-party wallpaper APIs.
- No database and no filesystem cache.
- Runtime HTML scraping from public Pinterest search pages.
- Every scraped wallpaper starts with 0 views and 0 likes.
"""

from __future__ import annotations

import hashlib
import random
import re
import time
from collections import Counter
from typing import Any
from urllib.parse import quote_plus, unquote

import requests
from flask import Flask, jsonify, make_response, request
from flask_cors import CORS


PORT = 5000
PAGE_SIZE = 30
PINTEREST_SEARCH_URL = "https://www.pinterest.com/search/pins/?q={query}"

USER_INTERACTION_DATA: dict[str, Counter[str]] = {}

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1",
]

TRENDING_PINTEREST_QUERIES = [
    "4K Cyberpunk Wallpaper",
    "Aesthetic Dark Wallpaper",
    "Anime Landscape 4K",
    "Aesthetic Neon Wallpaper",
    "Minimalist Dark Wallpaper",
    "Cinematic Nature Wallpaper",
]

app = Flask(__name__)
CORS(app, supports_credentials=True)


def now_ms() -> int:
    return int(time.time() * 1000)


def user_marker() -> str:
    explicit = request.headers.get("X-UltraWall-User") or request.headers.get("X-Session-Id") or request.args.get("user")
    if explicit:
        return hashlib.sha256(explicit.encode("utf-8")).hexdigest()[:24]
    cookie_marker = request.cookies.get("uwp_session")
    if cookie_marker:
        return cookie_marker
    ip = request.headers.get("X-Forwarded-For", request.remote_addr or "guest").split(",")[0].strip()
    ua = request.headers.get("User-Agent", "unknown")
    return hashlib.sha256(f"{ip}|{ua}".encode("utf-8")).hexdigest()[:24]


def json_with_session(payload: dict[str, Any]):
    marker = user_marker()
    response = make_response(jsonify(payload))
    if not request.cookies.get("uwp_session"):
        response.set_cookie("uwp_session", marker, max_age=60 * 60 * 24 * 365, httponly=False, samesite="Lax")
    return response


def split_keywords(value: Any) -> list[str]:
    if isinstance(value, (list, tuple, set)):
        raw = " ".join(str(v) for v in value)
    else:
        raw = str(value or "")
    return [part.strip().title() for part in re.split(r"[,#|/\s]+", raw) if part.strip()]


def clean_query(query: str) -> str:
    clean = re.sub(r"\s+", " ", str(query or "").replace("-", " ")).strip()
    return clean or random.choice(TRENDING_PINTEREST_QUERIES)


def title_from_query(query: str) -> str:
    words = [word for word in clean_query(query).split() if word.lower() not in {"search", "pins"}]
    title = " ".join(words).title()
    if "Wallpaper" not in title:
        title += " Wallpaper"
    return re.sub(r"\bWallpaper\s+Wallpaper\b", "Wallpaper", title).strip()


def tags_from_query(query: str) -> list[str]:
    tags = split_keywords(query)
    if "Wallpaper" not in tags:
        tags.append("Wallpaper")
    if "Pinterest" not in tags:
        tags.append("Pinterest")
    return list(dict.fromkeys(tags))


def stable_id(image_url: str, query: str) -> str:
    digest = hashlib.sha1(f"{query}|{image_url}".encode("utf-8")).hexdigest()[:18]
    return f"pin-{digest}"


def pinterest_headers() -> dict[str, str]:
    return {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.pinterest.com/",
        "Connection": "keep-alive",
    }


def normalize_pinimg_url(url: str) -> str:
    """
    Clean Pinterest image URLs from escaped HTML/JSON strings.

    Keeps direct Pinterest media URLs only. 736x and originals are both accepted;
    smaller thumbnails are upgraded to 736x when possible.
    """
    clean = unquote(str(url or ""))
    clean = clean.replace("\\u002F", "/").replace("\\/", "/").replace("&amp;", "&")
    clean = clean.split("?")[0].strip("\\\"' ")
    if not clean.startswith("https://i.pinimg.com/"):
        return ""
    clean = re.sub(r"/(60x60|75x75|170x|236x|474x)/", "/736x/", clean)
    if not re.search(r"\.(jpg|jpeg|png|webp)$", clean, re.I):
        return ""
    return clean


def extract_pinterest_image_urls(html: str) -> list[str]:
    """
    Extract raw direct image URLs from Pinterest HTML/embedded JSON.

    Pinterest often escapes URLs inside JSON blobs, so the regex accepts normal
    slashes and escaped slash variants.
    """
    patterns = [
        r"https://i\.pinimg\.com/(?:originals|736x)/[^\"'\\<>\s]+?\.(?:jpg|jpeg|png|webp)",
        r"https:\\/\\/i\.pinimg\.com\\/(?:originals|736x|474x|236x)[^\"'<>]+?\.(?:jpg|jpeg|png|webp)",
    ]
    urls: list[str] = []
    seen: set[str] = set()
    for pattern in patterns:
        for match in re.finditer(pattern, html, flags=re.I):
            image_url = normalize_pinimg_url(match.group(0))
            if image_url and image_url not in seen:
                seen.add(image_url)
                urls.append(image_url)
    return urls


def scrape_pinterest(query: str, page: int = 1, limit: int = PAGE_SIZE) -> list[dict[str, Any]]:
    """
    Fetch a public Pinterest search page and return cleaned wallpaper records.

    The page parameter rotates query wording and slices results so infinite scroll
    can request fresh batches without relying on third-party APIs.
    """
    clean = clean_query(query)
    search_query = clean if page <= 1 else f"{clean} premium 4k wallpaper page {page}"
    url = PINTEREST_SEARCH_URL.format(query=quote_plus(search_query))
    try:
        response = requests.get(url, headers=pinterest_headers(), timeout=12)
        response.raise_for_status()
    except requests.RequestException:
        return []

    raw_urls = extract_pinterest_image_urls(response.text)
    offset = max(page - 1, 0) * limit
    selected = raw_urls[offset:offset + limit]
    if len(selected) < limit:
        selected = raw_urls[:limit]

    title = title_from_query(clean)
    tags = tags_from_query(clean)
    return [
        {
            "id": stable_id(image_url, clean),
            "url": image_url,
            "imageUrl": image_url,
            "img": image_url,
            "originalUrl": image_url,
            "thumb": image_url,
            "title": title,
            "wallpaperTitle": title,
            "tags": tags,
            "wallpaperTags": tags,
            "keywords": tags,
            "views": 0,
            "likes": 0,
            "likesCount": 0,
            "source": "pinterest",
        }
        for image_url in selected
    ]


def dedupe(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    unique: list[dict[str, Any]] = []
    for item in items:
        key = item.get("url") or item.get("imageUrl") or item.get("id")
        if not key or key in seen:
            continue
        seen.add(key)
        unique.append(item)
    return unique


def curate_for_user(items: list[dict[str, Any]], marker: str) -> list[dict[str, Any]]:
    profile = USER_INTERACTION_DATA.get(marker, Counter())
    if not profile:
        return items
    priority = {key.lower() for key, count in profile.items() if count >= 2}
    if not priority:
        return items
    boosted, normal = [], []
    for item in items:
        haystack = " ".join([item.get("title", ""), " ".join(item.get("keywords", []))]).lower()
        (boosted if any(token in haystack for token in priority) else normal).append(item)
    return boosted + normal


def feed_queries(query: str, page: int) -> list[str]:
    if query:
        return [query]
    start = (page - 1) % len(TRENDING_PINTEREST_QUERIES)
    rotated = TRENDING_PINTEREST_QUERIES[start:] + TRENDING_PINTEREST_QUERIES[:start]
    return rotated[:3]


@app.get("/api/feed")
def feed():
    page = max(int(request.args.get("page", "1") or 1), 1)
    limit = max(1, min(int(request.args.get("limit", PAGE_SIZE) or PAGE_SIZE), 60))
    query = request.args.get("q", "").strip()
    marker = user_marker()

    items: list[dict[str, Any]] = []
    for active_query in feed_queries(query, page):
        items.extend(scrape_pinterest(active_query, page=page, limit=limit))
        if len(items) >= limit:
            break

    items = curate_for_user(dedupe(items), marker)[:limit]
    return json_with_session({
        "items": items,
        "page": page,
        "limit": limit,
        "hasMore": bool(items),
        "generatedAt": now_ms(),
    })


@app.get("/api/search")
def search():
    query = request.args.get("q", "").strip()
    limit = max(1, min(int(request.args.get("limit", PAGE_SIZE) or PAGE_SIZE), 60))
    items = scrape_pinterest(query, page=1, limit=limit) if query else []
    items = curate_for_user(dedupe(items), user_marker())[:limit]
    return json_with_session({"items": items, "q": query, "generatedAt": now_ms()})


@app.post("/api/track")
def track():
    marker = user_marker()
    payload = request.get_json(silent=True) or {}
    tokens = []
    tokens.extend(split_keywords(payload.get("category")))
    tokens.extend(split_keywords(payload.get("keywords")))
    tokens.extend(split_keywords(payload.get("title")))
    tokens.extend(split_keywords(payload.get("context")))
    if not tokens:
        tokens = ["General"]
    profile = USER_INTERACTION_DATA.setdefault(marker, Counter())
    for token in tokens:
        profile[token] += 1
    return json_with_session({"ok": True, "profile": dict(profile), "generatedAt": now_ms()})


@app.get("/api/health")
def health():
    return jsonify({"ok": True, "service": "UltraWall Pinterest Scraper", "port": PORT})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT, debug=False)
