"""
UltraWall runtime aggregation API.

This service intentionally uses no database and writes no image/cache files. Every
feed/search response is assembled from live public imagery sources and returned as
volatile JSON view data for the browser.
"""

from __future__ import annotations

import hashlib
import os
import random
import re
import time
import uuid
from collections import Counter
from typing import Any
from urllib.parse import quote_plus, urlparse

import requests
from flask import Flask, jsonify, make_response, request
from flask_cors import CORS


PORT = int(os.environ.get("PORT", "5000"))
REQUEST_TIMEOUT_SECONDS = float(os.environ.get("UPW_FETCH_TIMEOUT", "7"))
PAGE_SIZE = int(os.environ.get("UPW_PAGE_SIZE", "24"))

PINTEREST_KEYWORDS = [
    "premium mobile wallpaper",
    "anime wallpaper",
    "cyberpunk wallpaper",
    "dark aesthetic wallpaper",
    "nature wallpaper",
    "minimal wallpaper",
    "abstract 4k wallpaper",
]

EXTERNAL_QUERIES = [
    "mobile wallpaper",
    "desktop wallpaper",
    "cyberpunk city",
    "anime style art",
    "abstract background",
    "nature landscape",
]

CURATION_PRIORITY_TAGS = {"anime", "cyberpunk"}

# In-memory preference vectors keyed by a lightweight user marker. This is reset
# when the Flask process restarts and never persists to disk or any database.
USER_INTERACTION_DATA: dict[str, Counter[str]] = {}

app = Flask(__name__)
CORS(app)


def _now_ms() -> int:
    return int(time.time() * 1000)


def normalize_keyword(value: Any) -> str:
    """Normalize incoming category/keyword tokens for stable preference weights."""
    token = re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()
    return token[:60]


def split_keywords(value: Any) -> list[str]:
    """Accept strings/lists from clients and turn them into clean keyword tokens."""
    if isinstance(value, (list, tuple, set)):
        raw = " ".join(str(v) for v in value)
    else:
        raw = str(value or "")
    parts = re.split(r"[,#|/]+|\s{2,}", raw)
    return [k for k in (normalize_keyword(part) for part in parts) if k]


def user_marker() -> str:
    """Resolve the current user/session marker without requiring authentication."""
    explicit = (
        request.headers.get("X-UltraWall-User")
        or request.headers.get("X-Session-Id")
        or request.args.get("user")
    )
    if explicit:
        return hashlib.sha256(explicit.encode("utf-8")).hexdigest()[:24]

    cookie_marker = request.cookies.get("uwp_session")
    if cookie_marker:
        return cookie_marker

    ip = request.headers.get("X-Forwarded-For", request.remote_addr or "guest").split(",")[0].strip()
    ua = request.headers.get("User-Agent", "unknown")
    return hashlib.sha256(f"{ip}|{ua}".encode("utf-8")).hexdigest()[:24]


def with_session_cookie(payload: dict[str, Any]):
    """Return JSON while seeding an anonymous marker cookie for future ranking."""
    marker = user_marker()
    response = make_response(jsonify(payload))
    if not request.cookies.get("uwp_session"):
        response.set_cookie(
            "uwp_session",
            marker,
            max_age=60 * 60 * 24 * 365,
            httponly=False,
            samesite="Lax",
        )
    return response


def http_get_json(url: str, headers: dict[str, str] | None = None) -> Any:
    """Small resilient JSON fetch helper for live public source aggregation."""
    response = requests.get(
        url,
        headers=headers or {},
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    return response.json()


def stable_id(source: str, image_url: str, title: str = "") -> str:
    digest = hashlib.sha1(f"{source}|{image_url}|{title}".encode("utf-8")).hexdigest()[:16]
    return f"{source}-{digest}"


def format_item(
    *,
    image_url: str,
    title: str,
    source: str,
    keywords: list[str] | None = None,
    width: int | None = None,
    height: int | None = None,
) -> dict[str, Any]:
    """Shape every volatile runtime record exactly for the frontend stream."""
    clean_keywords = [k for k in (keywords or []) if k]
    return {
        "id": stable_id(source, image_url, title),
        "imageUrl": image_url,
        "img": image_url,
        "originalUrl": image_url,
        "title": title or "Premium Wallpaper",
        "likesCount": pseudo_likes(image_url),
        "source": source,
        "keywords": clean_keywords,
        "wallpaperTags": clean_keywords,
        "width": width,
        "height": height,
    }


def pseudo_likes(seed: str) -> int:
    """Produce stable public-like counts without storing counters anywhere."""
    return 120 + (int(hashlib.md5(seed.encode("utf-8")).hexdigest()[:6], 16) % 9800)


def image_passes_quality_guard(width: Any, height: Any) -> bool:
    """
    External web crawler guard boundary.

    Pinterest is deliberately exempt. Other providers must be true wallpaper
    candidates: portrait mobile at least 1080x1920 or landscape desktop at least
    1920x1080. Squares, banners, avatars, memes, and tiny/blurry assets fall out.
    """
    try:
        w = int(width or 0)
        h = int(height or 0)
    except (TypeError, ValueError):
        return False
    if w <= 0 or h <= 0:
        return False
    ratio = w / h
    is_portrait_mobile = h >= 1920 and w >= 1080 and ratio <= 0.8
    is_landscape_desktop = w >= 1920 and h >= 1080 and ratio >= 1.25
    return is_portrait_mobile or is_landscape_desktop


def title_from_url(url: str, fallback: str) -> str:
    path = urlparse(url).path.rsplit("/", 1)[-1]
    stem = re.sub(r"\.[a-z0-9]{2,5}$", "", path, flags=re.I)
    stem = re.sub(r"[-_]+", " ", stem).strip()
    return stem.title()[:80] if stem else fallback


def extract_pinterest_images(query: str, limit: int) -> list[dict[str, Any]]:
    """
    Pinterest extraction path.

    The public page stream is treated as a graphic stream. Per requirements, no
    resolution, aspect ratio, size, or format scoring is applied to Pinterest
    records; every discovered image URL is passed straight into the output shape.
    """
    url = f"https://www.pinterest.com/search/pins/?q={quote_plus(query)}"
    headers = {
        "User-Agent": "Mozilla/5.0 UltraWallRuntime/1.0",
        "Accept": "text/html,application/xhtml+xml",
    }
    try:
        html = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT_SECONDS).text
    except requests.RequestException:
        return []

    urls = []
    for match in re.finditer(r"https://i\.pinimg\.com/[^\"'\\<>\s]+", html):
        image_url = match.group(0).replace("\\u002F", "/").split("?")[0]
        if image_url not in urls:
            urls.append(image_url)
        if len(urls) >= limit:
            break

    keywords = split_keywords(query)
    return [
        format_item(
            image_url=image_url,
            title=title_from_url(image_url, "Pinterest Wallpaper"),
            source="pinterest",
            keywords=["pinterest", *keywords],
        )
        for image_url in urls
    ]


def fetch_pexels(query: str, limit: int) -> list[dict[str, Any]]:
    """Fetch curated imagery and apply strict wallpaper quality guards."""
    api_key = os.environ.get("PEXELS_API_KEY")
    if not api_key:
        return []
    url = f"https://api.pexels.com/v1/search?query={quote_plus(query)}&per_page={min(limit, 80)}&orientation=all"
    try:
        data = http_get_json(url, headers={"Authorization": api_key})
    except requests.RequestException:
        return []

    keywords = split_keywords(query)
    items = []
    for photo in data.get("photos", []):
        width, height = photo.get("width"), photo.get("height")
        if not image_passes_quality_guard(width, height):
            continue
        src = photo.get("src") or {}
        image_url = src.get("original") or src.get("large2x") or src.get("large")
        if not image_url:
            continue
        items.append(
            format_item(
                image_url=image_url,
                title=(photo.get("alt") or "Pexels Wallpaper")[:90],
                source="pexels",
                keywords=["pexels", *keywords],
                width=width,
                height=height,
            )
        )
    return items


def fetch_unsplash_source(query: str, limit: int) -> list[dict[str, Any]]:
    """
    Lightweight public Unsplash Source fallback.

    It cannot expose dimensions before redirect resolution, so it only emits
    high-intent wallpaper URLs with explicit crop dimensions that satisfy the
    external crawler guard by construction.
    """
    keywords = split_keywords(query)
    sizes = [(1080, 1920), (1920, 1080)]
    items = []
    for index in range(limit):
        width, height = sizes[index % len(sizes)]
        sig = hashlib.sha1(f"{query}-{index}-{width}x{height}".encode("utf-8")).hexdigest()[:10]
        image_url = (
            "https://source.unsplash.com/"
            f"{width}x{height}/?{quote_plus(query + ' wallpaper')}&sig={sig}"
        )
        if image_passes_quality_guard(width, height):
            items.append(
                format_item(
                    image_url=image_url,
                    title=f"{query.title()} Wallpaper",
                    source="unsplash",
                    keywords=["unsplash", *keywords],
                    width=width,
                    height=height,
                )
            )
    return items


def fallback_wallpapers(query: str, limit: int) -> list[dict[str, Any]]:
    """Deterministic volatile fallback when live providers are unavailable."""
    keywords = split_keywords(query or "premium wallpaper")
    seed = normalize_keyword(query) or "premium wallpaper"
    items = []
    sizes = [(1080, 1920), (1920, 1080)]
    for index in range(limit):
        width, height = sizes[index % 2]
        image_url = f"https://picsum.photos/seed/{quote_plus(seed)}-{index}/{width}/{height}"
        items.append(
            format_item(
                image_url=image_url,
                title=f"{(query or 'Premium').title()} Wallpaper {index + 1}",
                source="runtime-fallback",
                keywords=["fallback", *keywords],
                width=width,
                height=height,
            )
        )
    return items


def dedupe_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    unique = []
    for item in items:
        key = item.get("imageUrl") or item.get("id")
        if not key or key in seen:
            continue
        seen.add(key)
        unique.append(item)
    return unique


def aggregate_runtime_items(query: str = "", page: int = 1) -> list[dict[str, Any]]:
    """
    Compile Pinterest and guarded external crawler streams on demand.

    No directories, cache logs, local snapshots, or Firestore writes are created.
    """
    requested = max(PAGE_SIZE * max(page, 1), PAGE_SIZE)
    pinterest_query = query or random.choice(PINTEREST_KEYWORDS)
    external_query = query or random.choice(EXTERNAL_QUERIES)

    items: list[dict[str, Any]] = []
    items.extend(extract_pinterest_images(pinterest_query, requested // 2 + 8))
    items.extend(fetch_pexels(external_query, requested))
    items.extend(fetch_unsplash_source(external_query, max(8, requested // 3)))

    if len(items) < PAGE_SIZE:
        items.extend(fallback_wallpapers(query or external_query, PAGE_SIZE * 2))

    return dedupe_items(items)


def item_matches_interest(item: dict[str, Any], interests: set[str]) -> bool:
    haystack = " ".join(
        [
            str(item.get("title", "")),
            str(item.get("source", "")),
            " ".join(str(k) for k in item.get("keywords", [])),
        ]
    ).lower()
    return any(interest in haystack for interest in interests)


def curate_for_user(items: list[dict[str, Any]], marker: str) -> list[dict[str, Any]]:
    """
    Bubble high-frequency Anime/Cyberpunk matches into top slots.

    Fresh or neutral users receive the default mixed trending layout unchanged.
    """
    profile = USER_INTERACTION_DATA.get(marker, Counter())
    priority_interests = {
        tag for tag in CURATION_PRIORITY_TAGS
        if profile.get(tag, 0) >= 2 or profile.get(tag.title(), 0) >= 2
    }
    if not priority_interests:
        return items

    boosted = [item for item in items if item_matches_interest(item, priority_interests)]
    regular = [item for item in items if item not in boosted]
    return boosted + regular


def page_slice(items: list[dict[str, Any]], page: int) -> list[dict[str, Any]]:
    start = max(page - 1, 0) * PAGE_SIZE
    return items[start:start + PAGE_SIZE]


@app.get("/api/feed")
def feed():
    """Paginated volatile mixed feed with optional user-aware curation."""
    page = max(int(request.args.get("page", "1") or 1), 1)
    query = request.args.get("q", "").strip()
    marker = user_marker()
    items = aggregate_runtime_items(query=query, page=page)
    curated = curate_for_user(items, marker)
    sliced = page_slice(curated, page)
    return with_session_cookie({
        "items": sliced,
        "page": page,
        "limit": PAGE_SIZE,
        "hasMore": len(curated) > page * PAGE_SIZE,
        "userProfile": dict(USER_INTERACTION_DATA.get(marker, Counter())),
        "generatedAt": _now_ms(),
    })


@app.get("/api/search")
def search():
    """Search controller that dynamically compiles both runtime pipelines."""
    query = request.args.get("q", "").strip()
    if not query:
        return with_session_cookie({"items": [], "q": query, "generatedAt": _now_ms()})
    marker = user_marker()
    items = aggregate_runtime_items(query=query, page=1)
    curated = curate_for_user(items, marker)
    return with_session_cookie({
        "items": page_slice(curated, 1),
        "q": query,
        "generatedAt": _now_ms(),
    })


@app.post("/api/track")
def track():
    """
    Record local preference weights from frontend events.

    Expected JSON may include category, keywords, title, source, or context. The
    counters live only in USER_INTERACTION_DATA for this running process.
    """
    marker = user_marker()
    payload = request.get_json(silent=True) or {}
    tokens: list[str] = []
    tokens.extend(split_keywords(payload.get("category")))
    tokens.extend(split_keywords(payload.get("keywords")))
    tokens.extend(split_keywords(payload.get("title")))
    tokens.extend(split_keywords(payload.get("context")))

    if not tokens:
        tokens = ["general"]

    profile = USER_INTERACTION_DATA.setdefault(marker, Counter())
    for token in tokens:
        profile[token] += 1

    return with_session_cookie({
        "ok": True,
        "userMarker": marker,
        "profile": dict(profile),
        "generatedAt": _now_ms(),
    })


@app.get("/api/health")
def health():
    return jsonify({
        "ok": True,
        "service": "UltraWall Runtime Aggregator",
        "port": PORT,
        "usersTrackedInMemory": len(USER_INTERACTION_DATA),
    })


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT, debug=os.environ.get("FLASK_DEBUG") == "1")
