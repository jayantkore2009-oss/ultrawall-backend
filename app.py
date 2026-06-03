"""
UltraWall backend runtime feed.

Zero database, zero file cache: this service returns curated, direct high-resolution
wallpaper records that the frontend can render and download reliably.
"""

from __future__ import annotations

import hashlib
import os
import random
import time
from collections import Counter
from typing import Any

from flask import Flask, jsonify, make_response, request
from flask_cors import CORS


PORT = int(os.environ.get("PORT", "5000"))
PAGE_SIZE = int(os.environ.get("UPW_PAGE_SIZE", "24"))
USER_INTERACTION_DATA: dict[str, Counter[str]] = {}

app = Flask(__name__)
CORS(app, supports_credentials=True)


CURATED_CATEGORIES: dict[str, dict[str, Any]] = {
    "Aesthetic Neon": {
        "tags": ["Aesthetic", "Neon", "Glow", "Night", "Premium"],
        "seeds": ["neon-rain-street", "violet-signage", "glass-city-night", "electric-arcade", "pink-blue-alley"],
    },
    "Minimalist Dark": {
        "tags": ["Minimalist", "Dark", "Clean", "Matte", "Premium"],
        "seeds": ["black-minimal-ridge", "dark-silk-fold", "charcoal-orbit", "quiet-black-grid", "shadow-gradient"],
    },
    "Cyberpunk": {
        "tags": ["Cyberpunk", "City", "Futuristic", "Neon", "4K"],
        "seeds": ["cyberpunk-megacity", "blade-runner-rain", "future-tokyo", "hologram-district", "chrome-night"],
    },
    "4K Anime Landscape": {
        "tags": ["Anime", "Landscape", "4K", "Cinematic", "Scenery"],
        "seeds": ["anime-mountain-dawn", "ghibli-lake-sunset", "anime-cloud-valley", "sakura-night-sky", "fantasy-railway"],
    },
    "Cinematic Nature": {
        "tags": ["Nature", "Cinematic", "Landscape", "Atmospheric", "4K"],
        "seeds": ["alpine-golden-hour", "mist-forest-cinema", "aurora-lake", "storm-coast", "desert-moonrise"],
    },
}


DIRECT_IMAGE_CDN = "https://images.weserv.nl/?url=https://image.pollinations.ai/prompt/"


def now_ms() -> int:
    return int(time.time() * 1000)


def normalize_token(value: Any) -> str:
    return " ".join(str(value or "").replace("#", " ").replace(",", " ").split()).strip()


def split_keywords(value: Any) -> list[str]:
    if isinstance(value, (list, tuple, set)):
        raw = " ".join(str(v) for v in value)
    else:
        raw = str(value or "")
    tokens = []
    for token in raw.replace("#", " ").replace(",", " ").replace("|", " ").split():
        clean = normalize_token(token)
        if clean:
            tokens.append(clean)
    return tokens


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


def compact_id(value: str) -> str:
    digest = hashlib.sha1(value.encode("utf-8")).hexdigest()[:16]
    return f"uwp-{digest}"


def direct_wallpaper_url(prompt: str, width: int, height: int) -> str:
    """
    Direct high-resolution image URL.

    The generated prompt is routed through an image CDN so the browser receives a
    stable image payload URL suitable for rendering and Blob-based downloads.
    """
    safe_prompt = prompt.replace(" ", "%20").replace(",", "%2C")
    return (
        f"{DIRECT_IMAGE_CDN}{safe_prompt}"
        f"&w={width}&h={height}&fit=cover&output=jpg&q=95"
    )


def title_for(category: str, seed: str, orientation: str) -> str:
    words = {
        "neon-rain-street": "Neon Rain Street",
        "violet-signage": "Violet Signage District",
        "glass-city-night": "Glass City Night",
        "electric-arcade": "Electric Arcade Glow",
        "pink-blue-alley": "Pink Blue Alley",
        "black-minimal-ridge": "Black Minimal Ridge",
        "dark-silk-fold": "Dark Silk Fold",
        "charcoal-orbit": "Charcoal Orbit",
        "quiet-black-grid": "Quiet Black Grid",
        "shadow-gradient": "Shadow Gradient",
        "cyberpunk-megacity": "Cyberpunk Megacity",
        "blade-runner-rain": "Rainlit Future Boulevard",
        "future-tokyo": "Future Tokyo Skyline",
        "hologram-district": "Hologram District",
        "chrome-night": "Chrome Night Metropolis",
        "anime-mountain-dawn": "Anime Mountain Dawn",
        "ghibli-lake-sunset": "Painted Lake Sunset",
        "anime-cloud-valley": "Anime Cloud Valley",
        "sakura-night-sky": "Sakura Night Sky",
        "fantasy-railway": "Fantasy Railway Horizon",
        "alpine-golden-hour": "Alpine Golden Hour",
        "mist-forest-cinema": "Misty Forest Cinema",
        "aurora-lake": "Aurora Lake Reflection",
        "storm-coast": "Storm Coast Drama",
        "desert-moonrise": "Desert Moonrise",
    }
    return f"{words.get(seed, seed.replace('-', ' ').title())} {orientation}"


def build_wallpaper(category: str, seed: str, index: int, orientation: str) -> dict[str, Any]:
    width, height = (1080, 1920) if orientation == "Portrait" else (2560, 1440)
    base = CURATED_CATEGORIES[category]
    tags = [category, *base["tags"], orientation, "Wallpaper"]
    prompt = (
        f"{title_for(category, seed, orientation)}, legendary Pinterest style wallpaper, "
        f"ultra sharp, premium detail, high resolution, no text, no logo"
    )
    image_url = direct_wallpaper_url(prompt, width, height)
    return {
        "id": compact_id(f"{category}|{seed}|{orientation}|{index}"),
        "imageUrl": image_url,
        "img": image_url,
        "originalUrl": image_url,
        "thumb": image_url,
        "title": title_for(category, seed, orientation),
        "wallpaperTitle": title_for(category, seed, orientation),
        "likesCount": 0,
        "likes": 0,
        "views": 0,
        "source": "curated-pinterest-style",
        "keywords": tags,
        "wallpaperTags": tags,
        "width": width,
        "height": height,
    }


def curated_catalog() -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for category, data in CURATED_CATEGORIES.items():
        for seed_index, seed in enumerate(data["seeds"]):
            items.append(build_wallpaper(category, seed, seed_index, "Portrait"))
            items.append(build_wallpaper(category, seed, seed_index, "Landscape"))
    return items


def matches_query(item: dict[str, Any], query: str) -> bool:
    if not query:
        return True
    haystack = " ".join([
        item.get("title", ""),
        item.get("source", ""),
        " ".join(item.get("keywords", [])),
    ]).lower()
    return all(part in haystack for part in query.lower().split())


def curate_for_user(items: list[dict[str, Any]], marker: str) -> list[dict[str, Any]]:
    profile = USER_INTERACTION_DATA.get(marker, Counter())
    priority = [key for key, count in profile.items() if count >= 2]
    if not priority:
        return items
    boosted = []
    regular = []
    for item in items:
        haystack = " ".join([item["title"], " ".join(item["keywords"])]).lower()
        if any(token.lower() in haystack for token in priority):
            boosted.append(item)
        else:
            regular.append(item)
    return boosted + regular


def page_items(items: list[dict[str, Any]], page: int) -> list[dict[str, Any]]:
    start = max(page - 1, 0) * PAGE_SIZE
    return items[start:start + PAGE_SIZE]


@app.get("/api/feed")
def feed():
    page = max(int(request.args.get("page", "1") or 1), 1)
    query = request.args.get("q", "").strip()
    marker = user_marker()
    items = [item for item in curated_catalog() if matches_query(item, query)]
    random.Random(query.lower() or "global-premium-feed").shuffle(items)
    items = curate_for_user(items, marker)
    return json_with_session({
        "items": page_items(items, page),
        "page": page,
        "limit": PAGE_SIZE,
        "hasMore": page * PAGE_SIZE < len(items),
        "generatedAt": now_ms(),
    })


@app.get("/api/search")
def search():
    query = request.args.get("q", "").strip()
    marker = user_marker()
    items = [item for item in curated_catalog() if matches_query(item, query)]
    items = curate_for_user(items, marker)
    return json_with_session({"items": page_items(items, 1), "q": query, "generatedAt": now_ms()})


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
    return jsonify({"ok": True, "service": "UltraWall curated feed", "port": PORT})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT, debug=os.environ.get("FLASK_DEBUG") == "1")
