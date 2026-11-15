"""Resolve downloadable video streams from IGN pages.

IGN's own yt-dlp extractor relies on a retired API, so this module reads the
Next.js page payload (``__NEXT_DATA__``) that IGN ships with every page:

- video pages (``/videos/<slug>``) expose ``page.video.videoMetadata.m3uUrl``
- wiki pages embed a list of video elements in ``page.page.htmlEntities``;
  each element only carries a video slug, so the matching video page is fetched
  to obtain the stream URL

No browser is required: two plain HTTP requests are enough.
"""

import json
import re
from concurrent.futures import ThreadPoolExecutor

import requests

IGN_HOST_RE = re.compile(r"^https?://(?:[\w-]+\.)*ign\.com(?:/|$)", re.IGNORECASE)
_NEXT_DATA_RE = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.DOTALL)
_M3U_RE = re.compile(r'"m3uUrl"\s*:\s*"([^"]*master\.m3u8[^"]*)"')
_VIDEO_ELEMENT_RE = re.compile(
    r'"name"\s*:\s*"video"\s*,\s*"values"\s*:\s*\{[^{}]*?"slug"\s*:\s*"([^"]+)"'
)

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

_REQUEST_TIMEOUT = 25


class IgnExtractionError(Exception):
    """Raised when no downloadable IGN video can be located."""


def normalize_ign_url(url):
    """Return a fully qualified IGN URL, adding https:// when it is missing."""
    url = (url or "").strip()
    if not url:
        return url
    if not re.match(r"^https?://", url, re.IGNORECASE):
        url = "https://" + url.lstrip("/")
    return url


def is_ign_url(url):
    """True when the value points at an IGN page."""
    return bool(IGN_HOST_RE.match(normalize_ign_url(url)))


def _get_html(url):
    response = requests.get(url, headers=_HEADERS, timeout=_REQUEST_TIMEOUT)
    response.raise_for_status()
    return response.content.decode("utf-8", "replace")


def _next_data(html):
    match = _NEXT_DATA_RE.search(html)
    if not match:
        return {}
    try:
        return json.loads(match.group(1))
    except (ValueError, TypeError):
        return {}


def _page_payload(next_data):
    page_props = (next_data.get("props") or {}).get("pageProps") or {}
    return page_props.get("page") or {}


def _clean_url(value):
    if not value:
        return None
    return value.replace("\\u0026", "&").replace("\\/", "/")


def _title_from_payload(page):
    title = (page.get("title") or "").strip()
    if title:
        return title
    schema = page.get("schema") or {}
    return (schema.get("name") or page.get("pageTitle") or "").strip()


def _video_entry_from_page(html, page_url):
    """Read a playable stream from an IGN ``/videos/<slug>`` page."""
    page = _page_payload(_next_data(html))
    metadata = (page.get("video") or {}).get("videoMetadata") or {}

    stream_url = _clean_url(metadata.get("m3uUrl"))
    if not stream_url:
        match = _M3U_RE.search(html)
        stream_url = _clean_url(match.group(1)) if match else None

    if not stream_url:
        # Older IGN videos occasionally ship direct MP4 renditions only.
        assets = [asset for asset in (metadata.get("assets") or []) if asset.get("url")]
        if assets:
            best = max(assets, key=lambda asset: asset.get("height") or 0)
            stream_url = _clean_url(best.get("url"))

    if not stream_url:
        stream_url = _clean_url(metadata.get("livestreamUrl"))

    if not stream_url:
        return None

    return {
        "video_url": page_url,
        "id": page.get("videoId") or "",
        "title": _title_from_payload(page) or "IGN Video",
        "thumbnail": page.get("image") or "",
        "m3u": stream_url,
    }


def _video_links_from_page(html):
    """Collect the video page URLs referenced by a wiki/overview page."""
    page = _page_payload(_next_data(html))

    links = []
    for element in (page.get("page") or {}).get("htmlEntities") or []:
        if element.get("name") != "video":
            continue
        values = element.get("values") or {}
        video_url = _clean_url(values.get("url"))
        slug = values.get("slug")
        if not video_url and slug:
            video_url = f"https://www.ign.com/videos/{slug}"
        if video_url and video_url not in links:
            links.append(video_url)

    if not links:
        for match in _VIDEO_ELEMENT_RE.finditer(html):
            video_url = f"https://www.ign.com/videos/{match.group(1)}"
            if video_url not in links:
                links.append(video_url)

    return links


def scan_ign_videos(url, max_workers=6):
    """Return every downloadable video found on an IGN page.

    Each item is a dict with ``video_url``, ``id``, ``title``, ``thumbnail`` and
    ``m3u`` (a stream URL ready for yt-dlp).
    """
    url = normalize_ign_url(url)
    if not is_ign_url(url):
        raise IgnExtractionError("Please enter a valid IGN link (ign.com).")

    try:
        html = _get_html(url)
    except requests.HTTPError as exc:
        raise IgnExtractionError(
            "IGN returned an error for this page. Check the link and try again."
        ) from exc
    except requests.RequestException as exc:
        raise IgnExtractionError(
            "Could not reach IGN. Check your connection and try again."
        ) from exc

    direct = _video_entry_from_page(html, url)
    if direct:
        return [direct]

    video_urls = _video_links_from_page(html)
    if not video_urls:
        raise IgnExtractionError("No video found on this IGN page.")

    def fetch(video_url):
        try:
            return _video_entry_from_page(_get_html(video_url), video_url)
        except requests.RequestException:
            return None

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        entries = list(pool.map(fetch, video_urls))

    videos = []
    seen = set()
    for entry in entries:
        if not entry or entry["video_url"] in seen:
            continue
        seen.add(entry["video_url"])
        videos.append(entry)

    if not videos:
        raise IgnExtractionError(
            "Video links were found, but their streams could not be read."
        )

    return videos
