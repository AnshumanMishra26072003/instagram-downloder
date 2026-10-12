"""
IGDown — Instagram media downloader backend (single serverless function).

Public content only: uses yt-dlp for metadata extraction, no login / no cookies.

All API traffic goes through one endpoint, `/api/ig`, so the file maps 1:1 to a
Vercel Python function (api/ig.py -> /api/ig) with no rewrite rules needed.

    POST /api/ig          {"action":"fetch", "url":"<link or @handle>"}
    GET  /api/ig?action=download&u=<b64url>&name=<filename>
    GET  /api/ig?action=health

It is a plain FastAPI (ASGI) app, so it also runs locally with:
    uvicorn api.ig:app --reload
"""

from __future__ import annotations

import base64
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
import yt_dlp
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parent.parent
INDEX_FILE = ROOT / "index.html"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# Hosts we will proxy bytes from (keeps the download endpoint from becoming an
# open proxy / SSRF vector).
ALLOWED_HOSTS = (".cdninstagram.com", ".fbcdn.net", "instagram.com", ".instagram.com")

SHORTCODE_RE = re.compile(r"instagram\.com/(?:[^/?#]+/)?(?:p|reel|reels|tv)/([A-Za-z0-9_-]+)")
STORY_RE = re.compile(r"instagram\.com/stories/([A-Za-z0-9._]+)")
HANDLE_RE = re.compile(r"^@?([A-Za-z0-9._]{2,30})$")
BARE_SHORTCODE_RE = re.compile(r"^[A-Za-z0-9_-]{9,14}$")

YDL_BASE: dict[str, Any] = {
    "quiet": True,
    "no_warnings": True,
    "skip_download": True,
    "socket_timeout": 25,
    "retries": 2,
    "nocheckcertificate": True,
    "http_headers": {"User-Agent": UA, "Referer": "https://www.instagram.com/"},
}

app = FastAPI(title="IGDown API", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

_CACHE: dict[str, tuple[float, dict]] = {}
_CACHE_TTL = 300


# --------------------------------------------------------------------------- #
# Input normalisation
# --------------------------------------------------------------------------- #
def normalise(raw: str) -> dict:
    raw = (raw or "").strip()
    if not raw:
        raise ValueError("Please paste an Instagram link or @username.")

    if "instagram.com" in raw or raw.startswith("http"):
        if "http" not in raw:
            raw = "https://" + raw
        m = SHORTCODE_RE.search(raw)
        if m:
            return {"kind": "post", "url": f"https://www.instagram.com/p/{m.group(1)}/", "shortcode": m.group(1)}
        s = STORY_RE.search(raw)
        if s:
            return {"kind": "stories", "url": raw.split("?")[0], "username": s.group(1)}
        p = urlparse(raw)
        handle = p.path.strip("/").split("/")[0]
        if handle and HANDLE_RE.match(handle):
            return {"kind": "profile", "url": f"https://www.instagram.com/{handle}/", "username": handle}
        raise ValueError("That does not look like an Instagram link.")

    if BARE_SHORTCODE_RE.match(raw):
        return {"kind": "post", "url": f"https://www.instagram.com/p/{raw}/", "shortcode": raw}

    if HANDLE_RE.match(raw):
        handle = raw.lstrip("@")
        return {"kind": "profile", "url": f"https://www.instagram.com/{handle}/", "username": handle}

    raise ValueError("Please paste an Instagram link or @username.")


# --------------------------------------------------------------------------- #
# Extraction helpers
# --------------------------------------------------------------------------- #
def _is_video_only(f: dict) -> bool:
    return f.get("acodec") == "none" and f.get("vcodec") not in (None, "none")


def _build_formats(entry: dict) -> list[dict]:
    out: list[dict] = []
    best_url = entry.get("url")
    best_ext = (entry.get("ext") or "mp4").lower()
    if best_url:
        out.append({
            "label": "Original quality (MP4, with audio)" if best_ext == "mp4" else f"Image ({best_ext.upper()})",
            "quality": "hd" if best_ext == "mp4" else "image",
            "ext": best_ext,
            "url": best_url,
            "size": entry.get("filesize") or entry.get("filesize_approx"),
            "has_audio": True,
            "primary": True,
        })

    seen: set[int] = set()
    for f in entry.get("formats") or []:
        if not _is_video_only(f) or not f.get("url"):
            continue
        h = f.get("height")
        if not h or h in seen:
            continue
        seen.add(h)
        out.append({
            "label": f"{h}p (video only — no audio)",
            "quality": f"{h}p",
            "ext": (f.get("ext") or "mp4").lower(),
            "url": f["url"],
            "size": f.get("filesize") or f.get("filesize_approx"),
            "has_audio": False,
            "primary": False,
        })

    head = [o for o in out if o["primary"]]
    tail = sorted([o for o in out if not o["primary"]], key=lambda o: o.get("quality", ""), reverse=True)
    return head + tail


def _build_item(entry: dict) -> dict:
    thumbs = entry.get("thumbnails") or [{}]
    return {
        "id": entry.get("id"),
        "title": (entry.get("title") or entry.get("description") or "Instagram media").strip()[:140],
        "caption": (entry.get("description") or "").strip()[:1000],
        "author": {
            "name": entry.get("uploader") or entry.get("channel") or "",
            "username": entry.get("channel") or entry.get("uploader_id") or "",
        },
        "thumbnail": entry.get("thumbnail") or thumbs[-1].get("url"),
        "duration": entry.get("duration"),
        "formats": _build_formats(entry),
    }


def _extract(url: str) -> dict:
    opts = dict(YDL_BASE)
    opts["format"] = "best"
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
        return ydl.sanitize_info(info)


def _do_fetch(raw: str) -> dict:
    try:
        target = normalise(raw)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    cached = _CACHE.get(target["url"])
    if cached and time.time() - cached[0] < _CACHE_TTL:
        return cached[1]

    try:
        info = _extract(target["url"])
    except yt_dlp.utils.DownloadError as e:
        msg = str(e).lower()
        if target["kind"] == "profile":
            detail = ("Fetching a whole profile or DP needs a login, which this tool does not do. "
                      "Paste a specific post or Reel link instead (••• → Copy link).")
        elif any(k in msg for k in ("login", "rate", "not available", "requested content", "private")):
            detail = ("Instagram did not return this media. It may be private, removed, "
                      "or the server is being rate-limited — try again shortly.")
        else:
            detail = "Could not read that link. Check that it is a public Instagram post or Reel."
        raise HTTPException(status_code=502, detail=detail)
    except Exception:
        raise HTTPException(status_code=502, detail="Failed to fetch this link. Please try again.")

    entries = info.get("entries") if info.get("_type") == "playlist" else None
    items = []
    for e in (entries if entries else [info]):
        if not e:
            continue
        item = _build_item(e)
        if item["formats"]:
            items.append(item)

    if not items:
        raise HTTPException(
            status_code=404,
            detail=("No downloadable media found. Stories, Highlights and profile grids "
                    "require a login and are not supported by this tool."),
        )

    payload = {
        "ok": True,
        "kind": "carousel" if len(items) > 1 else "single",
        "count": len(items),
        "source": target["url"],
        "items": items,
    }
    _CACHE[target["url"]] = (time.time(), payload)
    return payload


async def _do_download(request: Request, u: str, name: str) -> StreamingResponse:
    try:
        padded = u + "=" * (-len(u) % 4)
        src = base64.urlsafe_b64decode(padded).decode("utf-8")
    except Exception:
        raise HTTPException(status_code=400, detail="Bad download token.")

    host = (urlparse(src).hostname or "").lower()
    if not any(host == h.lstrip(".") or host.endswith(h) for h in ALLOWED_HOSTS):
        raise HTTPException(status_code=403, detail="Host not allowed.")

    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", name)[:80] or "instagram"
    headers = {"User-Agent": UA, "Referer": "https://www.instagram.com/"}
    if request.headers.get("range"):
        headers["Range"] = request.headers["range"]

    client = httpx.AsyncClient(timeout=httpx.Timeout(30, read=120), follow_redirects=True)
    try:
        resp = await client.send(client.build_request("GET", src, headers=headers), stream=True)
    except Exception:
        await client.aclose()
        raise HTTPException(status_code=502, detail="Could not reach the media server.")

    if resp.status_code >= 400:
        await resp.aclose()
        await client.aclose()
        raise HTTPException(status_code=502, detail="Media server refused the request.")

    out_headers = {
        "Content-Disposition": f'attachment; filename="{safe}"',
        "Accept-Ranges": "bytes",
        "Cache-Control": "no-store",
    }
    for h in ("content-type", "content-length", "content-range"):
        if resp.headers.get(h):
            out_headers[h.title()] = resp.headers[h]

    async def streamer():
        try:
            async for chunk in resp.aiter_bytes(65536):
                yield chunk
        finally:
            await resp.aclose()
            await client.aclose()

    return StreamingResponse(streamer(), status_code=resp.status_code, headers=out_headers)


# --------------------------------------------------------------------------- #
# Routes — registered for "/api/ig", "/ig" and "/" so the app works no matter
# whether the platform passes the full path or the stripped one.
# --------------------------------------------------------------------------- #
class FetchBody(BaseModel):
    action: str = "fetch"
    url: str = ""
    username: str = ""


PATHS = ["/api/ig", "/ig", "/"]


@app.get("/api/health")
@app.get("/health")
async def health():
    return {"ok": True, "service": "igdown"}


@app.post("/api/ig")
@app.post("/ig")
@app.post("/")
async def api_post(body: FetchBody):
    action = (body.action or "fetch").lower()
    if action == "fetch":
        return _do_fetch(body.url or body.username)
    raise HTTPException(status_code=400, detail=f"Unknown action: {action}")


@app.get("/api/ig")
@app.get("/ig")
@app.get("/")
async def api_get(
    request: Request,
    action: str = Query(""),
    u: str = Query(""),
    name: str = Query("instagram"),
    url: str = Query(""),
):
    action = (action or "").lower()
    if action == "download":
        return await _do_download(request, u, name)
    if action == "health":
        return {"ok": True, "service": "igdown"}
    if action == "fetch":
        return _do_fetch(url)
    # No action -> serve the UI (local dev). On Vercel, static index.html wins.
    if INDEX_FILE.exists():
        return FileResponse(INDEX_FILE)
    return JSONResponse({"ok": True, "service": "igdown"})
