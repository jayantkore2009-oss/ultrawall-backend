"""
UltraWall Pinterest scraper backend – Premium v2
=================================================
No third-party wallpaper APIs. No database. No filesystem cache.
Runtime HTML scraping from public Pinterest search pages with accurate
metadata extraction (alt text / JSON-LD / microdata). Every scraped
wallpaper starts with 0 views and 0 likes.
"""

from __future__ import annotations

import hashlib
import json
import random
import re
import time
import uuid
from collections import Counter
from typing import Any
from urllib.parse import quote_plus, unquote

import requests
from bs4 import BeautifulSoup
from flask import Flask, jsonify, make_response, request
from flask_cors import CORS

PORT = 5000
PAGE_SIZE = 30
PINTEREST_SEARCH_URL = "https://www.pinterest.com/search/pins/?q={query}"
PINTEREST_BOARD_HOST = "https://in.pinterest.com"

USER_INTERACTION_DATA: dict[str, Counter[str]] = {}

# ---------------------------------------------------------------------------
# 1. PREMIUM / HIGH-END PINTEREST QUERIES (aesthetic, cyberpunk, etc.)
# ---------------------------------------------------------------------------
TRENDING_PINTEREST_QUERIES = [
    "4K Cyberpunk Minimalist Wallpaper",
    "Aesthetic Dark Minimalist 4K",
    "Premium 4K Anime Landscape Light",
    "Neon Cinematic 4K Wallpaper",
    "Dark Moody Aesthetic 4K",
    "Retrowave Synthwave 4K",
    "Vaporwave Aesthetic 4K",
    "Minimalist Dark Nature 4K",
    "Sci-Fi Cityscape 4K",
    "Japanese Art Aesthetic 4K",
]

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64; rv:126.0) Gecko/20100101 Firefox/126.0",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
]

# Browser-like cookie jar for session persistence
_session_map: dict[str, str] = {}

app = Flask(__name__)
CORS(app, supports_credentials=True)


# ---------------------------------------------------------------------------
# 2. HELPERS
# ---------------------------------------------------------------------------
def now_ms() -> int:
    return int(time.time() * 1000)


def user_marker() -> str:
    explicit = request.headers.get("X-UltraWall-User") or request.headers.get(
        "X-Session-Id"
    ) or request.args.get("user")
    if explicit:
        return hashlib.sha256(explicit.encode("utf-8")).hexdigest()[:24]
    cookie_marker = request.cookies.get("uwp_session")
    if cookie_marker:
        return cookie_marker
    ip = (
        request.headers.get("X-Forwarded-For", request.remote_addr or "guest")
        .split(",")[0]
        .strip()
    )
    ua = request.headers.get("User-Agent", "unknown")
    return hashlib.sha256(f"{ip}|{ua}".encode("utf-8")).hexdigest()[:24]


def json_with_session(payload: dict[str, Any]):
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


def split_keywords(value: Any) -> list[str]:
    if isinstance(value, (list, tuple, set)):
        raw = " ".join(str(v) for v in value)
    else:
        raw = str(value or "")
    return [part.strip().title() for part in re.split(r"[,#|/\s]+", raw) if part.strip()]


def clean_query(query: str) -> str:
    clean = re.sub(r"\s+", " ", str(query or "").replace("-", " ")).strip()
    return clean or random.choice(TRENDING_PINTEREST_QUERIES)


def stable_id(image_url: str, query: str) -> str:
    digest = hashlib.sha1(f"{query}|{image_url}".encode("utf-8")).hexdigest()[:18]
    return f"pin-{digest}"


def pinterest_headers() -> dict[str, str]:
    """Generate request headers that mimic a real browser."""
    return {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Referer": "https://www.pinterest.com/",
        "Connection": "keep-alive",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "same-origin",
        "Sec-Fetch-User": "?1",
        "Upgrade-Insecure-Requests": "1",
        "DNT": "1",
    }


def normalize_pinimg_url(url: str) -> str:
    """Normalise a Pinterest image URL to the original /736x/ size variant."""
    clean = unquote(str(url or ""))
    clean = clean.replace("\\u002F", "/").replace("\\/", "/").replace("&", "&")
    clean = clean.split("?")[0].strip("\\\"' ")
    if not clean.startswith("https://i.pinimg.com/"):
        return ""
    # Upgrade small thumbnails to 736x
    clean = re.sub(r"/(60x60|75x75|170x|236x|474x)/", "/736x/", clean)
    if not re.search(r"\.(jpg|jpeg|png|webp)$", clean, re.I):
        return ""
    return clean


# ---------------------------------------------------------------------------
# 3. ADVANCED METADATA EXTRACTION
# ---------------------------------------------------------------------------
def extract_metadata_from_json_ld(soup: BeautifulSoup) -> list[dict[str, Any]]:
    """
    Parse Pinterest's embedded JSON-LD / application/ld+json scripts to get
    accurate titles and descriptions per pin.
    """
    results: list[dict[str, Any]] = []
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string)
            if isinstance(data, dict):
                data = [data]
            for item in data:
                name = (item.get("name") or "").strip()
                desc = (item.get("description") or "").strip()
                image = (item.get("image") or {}).get("url") or item.get("image") or ""
                if name or desc:
                    results.append(
                        {
                            "title": name or desc,
                            "description": desc or name,
                            "image": image,
                        }
                    )
        except (json.JSONDecodeError, AttributeError):
            continue
    return results


def extract_metadata_from_alt_tags(soup: BeautifulSoup) -> dict[str, str]:
    """
    Extract per-image alt text from <img> tags. Pinterest often embeds
    descriptive alt text that matches the image content exactly.
    Returns a mapping of normalised image URL -> alt title.
    """
    alt_map: dict[str, str] = {}
    for img in soup.find_all("img"):
        alt = (img.get("alt") or "").strip()
        src = normalize_pinimg_url(img.get("src") or "")
        srcset = img.get("srcset") or ""
        if not alt or not src:
            continue
        # Skip generic alt text
        if alt.lower() in {"", "image", "photo", "pin", "wallpaper", "img."}:
            continue
        if len(alt) < 4:
            continue
        alt_map[src] = alt
        # Also check srcset for additional mapping
        if srcset:
            for part in srcset.split(","):
                url_part = part.strip().split(" ")[0]
                normalised = normalize_pinimg_url(url_part)
                if normalised and normalised not in alt_map:
                    alt_map[normalised] = alt
    return alt_map


def extract_metadata_from_json_data(soup: BeautifulSoup) -> dict[str, dict[str, Any]]:
    """
    Extract Pinterest's internal <script data-pin-data> or JSON data blocks
    that contain per-pin metadata including titles, descriptions, and tags.
    """
    pin_data: dict[str, dict[str, Any]] = {}
    patterns = [
        r'"description"\s*:\s*"([^"]+)"',
        r'"title"\s*:\s*"([^"]+)"',
        r'"alt"\s*:\s*"([^"]+)"',
        r'"grid_description"\s*:\s*"([^"]+)"',
    ]
    # Find all script tags that might contain pin data
    for script in soup.find_all("script"):
        text = script.string or ""
        if "description" in text and "image" in text:
            # Try to extract image URL and its associated description
            img_matches = re.finditer(
                r'"image"\s*:\s*"(https[^"]+?(?:originals|736x)[^"]+?(?:jpg|jpeg|png|webp))"',
                text,
                re.I,
            )
            for img_match in img_matches:
                img_url = normalize_pinimg_url(img_match.group(1))
                if not img_url:
                    continue
                # Look for description/title near this image
                chunk = text[max(0, img_match.start() - 500) : img_match.end() + 500]
                for p in patterns:
                    desc_match = re.search(p, chunk, re.I)
                    if desc_match:
                        desc_text = desc_match.group(1).strip()
                        if len(desc_text) > 4 and desc_text.lower() not in {
                            "",
                            "image",
                            "photo",
                            "pin",
                        }:
                            pin_data.setdefault(img_url, {})
                            if p == '"title"':
                                pin_data[img_url]["title"] = desc_text
                            elif p == '"grid_description"':
                                pin_data[img_url]["description"] = desc_text
                                if "title" not in pin_data[img_url]:
                                    pin_data[img_url]["title"] = desc_text
                            else:
                                pin_data[img_url][
                                    "description"
                                ] = pin_data[img_url].get("description", desc_text)
                                if "title" not in pin_data[img_url]:
                                    pin_data[img_url]["title"] = desc_text
                            break
    return pin_data


def extract_pinterest_image_urls(html: str) -> list[str]:
    """Extract raw direct Pinterest image URLs."""
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


def build_wallpaper_record(
    image_url: str,
    query: str,
    alt_text_map: dict[str, str] | None = None,
    json_ld_items: list[dict[str, Any]] | None = None,
    json_pin_data: dict[str, dict[str, Any]] | None = None,
    index: int = 0,
) -> dict[str, Any]:
    """
    Build a single wallpaper record with extracted metadata.

    Priority for title extraction:
    1. Alt text from <img> tags (most accurate per-pin)
    2. JSON-LD structured data
    3. Inline JSON pin data
    4. Query-derived fallback (least preferred)
    """
    title = ""
    description = ""
    tags: list[str] = []

    # 1. Check alt text map
    if alt_text_map and image_url in alt_text_map:
        title = alt_text_map[image_url]

    # 2. Check JSON pin data
    if not title and json_pin_data and image_url in json_pin_data:
        title = json_pin_data[image_url].get("title") or ""
        description = json_pin_data[image_url].get("description") or ""

    # 3. Check JSON-LD items
    if not title and json_ld_items and index < len(json_ld_items):
        title = json_ld_items[index].get("title") or ""

    # 4. Fallback to query-derived title
    if not title:
        title = title_from_query(query)

    # Clean up title
    title = re.sub(r"\s+", " ", title).strip()
    if not title or len(title) < 4:
        title = title_from_query(query)

    # Generate tags from title + description + query
    tag_sources = [title]
    if description:
        tag_sources.append(description)
    tag_sources.append(query)
    tags = split_keywords(" ".join(tag_sources))
    # Remove duplicates, keep order
    tags = list(dict.fromkeys(tags))
    if "Wallpaper" not in tags:
        tags.append("Wallpaper")
    if "Pinterest" not in tags:
        tags.append("Pinterest")

    return {
        "id": stable_id(image_url, query),
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


def title_from_query(query: str) -> str:
    """Generate a fallback title from the search query."""
    words = [
        word
        for word in clean_query(query).split()
        if word.lower() not in {"search", "pins", "hd", "ultra"}
    ]
    title = " ".join(words).title()
    if "Wallpaper" not in title:
        title += " Wallpaper"
    title = re.sub(r"\bWallpaper\s+Wallpaper\b", "Wallpaper", title).strip()
    return title if title else "Premium Wallpaper"


# ---------------------------------------------------------------------------
# 4. MAIN SCRAPE FUNCTION
# ---------------------------------------------------------------------------
def scrape_pinterest(
    query: str, page: int = 1, limit: int = PAGE_SIZE
) -> list[dict[str, Any]]:
    """
    Fetch a public Pinterest search page, extract images AND their real
    metadata (alt text, JSON-LD, inline data), and return wallpaper records
    with accurate per-image titles.
    """
    clean_q = clean_query(query)
    search_query = clean_q if page <= 1 else f"{clean_q} premium 4k wallpaper page {page}"
    url = PINTEREST_SEARCH_URL.format(query=quote_plus(search_query))

    headers = pinterest_headers()
    # Add a random delay to avoid rate-limiting
    time.sleep(random.uniform(0.3, 0.9))

    try:
        session = requests.Session()
        session.headers.update(headers)
        # Set a random session cookie
        session.cookies.set(
            "_pinterest_sess", uuid.uuid4().hex[:32], domain=".pinterest.com"
        )
        response = session.get(url, timeout=15)
        response.raise_for_status()
    except requests.RequestException:
        return []

    html = response.text
    soup = BeautifulSoup(html, "lxml")

    # 1. Extract alt text from <img> tags
    alt_text_map = extract_metadata_from_alt_tags(soup)

    # 2. Extract JSON-LD metadata
    json_ld_items = extract_metadata_from_json_ld(soup)

    # 3. Extract inline JSON pin data
    json_pin_data = extract_metadata_from_json_data(soup)

    # 4. Extract all raw image URLs
    raw_urls = extract_pinterest_image_urls(html)
    if not raw_urls:
        return []

    # 5. Apply pagination slice
    offset = max(page - 1, 0) * limit
    selected = raw_urls[offset : offset + limit]
    if len(selected) < limit:
        selected = raw_urls[:limit]

    # 6. Build wallpaper records with per-image metadata
    records = []
    for i, image_url in enumerate(selected):
        record = build_wallpaper_record(
            image_url=image_url,
            query=clean_q,
            alt_text_map=alt_text_map,
            json_ld_items=json_ld_items,
            json_pin_data=json_pin_data,
            index=i,
        )
        records.append(record)

    return records


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


def curate_for_user(
    items: list[dict[str, Any]], marker: str
) -> list[dict[str, Any]]:
    profile = USER_INTERACTION_DATA.get(marker, Counter())
    if not profile:
        return items
    priority = {key.lower() for key, count in profile.items() if count >= 2}
    if not priority:
        return items
    boosted, normal = [], []
    for item in items:
        haystack = " ".join(
            [item.get("title", ""), " ".join(item.get("keywords", []))]
        ).lower()
        (boosted if any(token in haystack for token in priority) else normal).append(
            item
        )
    return boosted + normal


def feed_queries(query: str, page: int) -> list[str]:
    if query:
        return [query]
    start = max(0, (page - 1) % len(TRENDING_PINTEREST_QUERIES))
    rotated = TRENDING_PINTEREST_QUERIES[start:] + TRENDING_PINTEREST_QUERIES[:start]
    return rotated[:3]


# ---------------------------------------------------------------------------
# 5. API ENDPOINTS
# ---------------------------------------------------------------------------
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
    return json_with_session(
        {
            "items": items,
            "page": page,
            "limit": limit,
            "hasMore": bool(items),
            "generatedAt": now_ms(),
        }
    )


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
    return json_with_session(
        {"ok": True, "profile": dict(profile), "generatedAt": now_ms()}
    )


@app.get("/api/health")
def health():
    return jsonify(
        {"ok": True, "service": "UltraWall Pinterest Scraper v2", "port": PORT}
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT, debug=False)