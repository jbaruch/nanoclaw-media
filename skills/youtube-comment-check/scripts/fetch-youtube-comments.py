#!/usr/bin/env python3
"""Fetch recent comment threads on a YouTube channel over the native
YouTube Data API v3 (nanoclaw-admin#339).

The comment-threads surface has no equivalent outside the native API,
so this calls the YouTube Data API v3 directly with `YOUTUBE_API_KEY`:

  1. commentThreads.list?allThreadsRelatedToChannelId=<channel> — recent
     threads across every video on the channel, newest first, paginated.
  2. videos.list?id=<ids> — titles for the videos those comments landed
     on (one batched call).

Comments are filtered by each thread's top-level-comment `publishedAt`
to a window that, when a `--cursor` is given, spans since the last
successful run — so a week the check failed or was gated out is
re-covered instead of lost outside a fixed 7-day window
(jbaruch/nanoclaw#803). Without a usable cursor the window is `--days`
(default 7). The lookback is bounded by `--max-days`. Comments are then
grouped by video.

Usage
-----
    fetch-youtube-comments.py --channel-id <id> [--days 7]
        [--cursor <path>] [--max-days 35]

Output
------
On success: single-line JSON on stdout, exit 0:
    {"window_days": N, "window_source": "cursor|cursor_capped|default|cursor_unreadable",
     "comment_count": N,
     "videos": [{"id", "title", "url",
                 "comments": [{"author", "text", "published_at"}]}]}
`comment_count == 0` is a valid quiet-week result, not an error.

On missing `YOUTUBE_API_KEY`, missing `--channel-id`, an API/HTTP error,
or a YouTube error envelope: a diagnostic on stderr and a non-zero exit,
no stdout — so the skill surfaces the error and does NOT advance its
success cursor.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

# rstrip the trailing slash so the `{API_BASE}/{path}` join never doubles.
API_BASE = os.environ.get("YOUTUBE_API_BASE", "https://www.googleapis.com/youtube/v3").rstrip("/")
PER_CALL_TIMEOUT_SECONDS = 60.0
# Defensive page cap: a personal channel's last-week comments fit in one
# 100-item page; cap pagination so a busy channel can't loop unbounded.
MAX_THREAD_PAGES = 5
# videos.list caps the `id` parameter at 50 ids per request.
VIDEOS_LIST_MAX_IDS = 50
WATCH_URL = "https://www.youtube.com/watch?v="
# Cursor shape written by stamp-cursor.py; the fetch reads it (never writes).
CURSOR_SCHEMA_VERSION = 1
# Upper bound on a cursor-derived lookback so a long outage can't widen the
# window without limit (and keeps the fetch under MAX_THREAD_PAGES for a
# personal channel). A gap longer than this loses the oldest comments — an
# extreme edge the operator would already be seeing surfaced errors for.
DEFAULT_MAX_LOOKBACK_DAYS = 35


class YouTubeError(RuntimeError):
    """A YouTube Data API error envelope or HTTP failure. The message is
    operator-actionable (status + reason)."""


def _redact(text: str, api_key: str) -> str:
    """Strip the API key from a message before it reaches stderr.

    `key` rides in the request URL query string, so a urllib exception
    string or an echoed error body could otherwise leak it (`no-secrets`).
    Both the raw key and its percent-encoded form are scrubbed, since a
    diagnostic may carry either.
    """
    if not api_key:
        return text
    for variant in (api_key, urllib.parse.quote(api_key, safe="")):
        text = text.replace(variant, "***")
    return text


def _get(path: str, params: dict, api_key: str) -> dict:
    query = urllib.parse.urlencode({**params, "key": api_key})
    url = f"{API_BASE}/{path}?{query}"
    req = urllib.request.Request(url, headers={"accept": "application/json"}, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=PER_CALL_TIMEOUT_SECONDS) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = _redact(exc.read().decode("utf-8", "replace")[:300], api_key)
        raise YouTubeError(f"HTTP {exc.code} on {path}: {detail}") from exc
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        raise YouTubeError(_redact(f"{type(exc).__name__} on {path}: {exc}", api_key)) from exc
    if isinstance(body, dict) and body.get("error"):
        raise YouTubeError(
            f"YouTube API error on {path}: {_redact(json.dumps(body['error'])[:300], api_key)}"
        )
    return body


def _utcnow() -> datetime:
    """Current UTC time; the single clock read, split out so tests can
    freeze it (coding-policy testing-standards: control the clock)."""
    return datetime.now(timezone.utc)


def _window_days_from_cursor(
    cursor_path: str | None,
    now: datetime,
    default_days: int,
    max_days: int,
) -> tuple[int, str]:
    """Resolve the fetch window (whole days) from the success cursor.

    The cursor (`stamp-cursor.py`, schema v1) records the previous
    successful run's completion time. Fetching *since* that instant —
    rather than a fixed `--days` — re-covers a week the check failed or
    was gated out, whose comments would otherwise fall outside a fixed
    7-day window forever (jbaruch/nanoclaw#803). Bounded by `max_days`
    so a long outage can't widen the lookback without limit.

    Returns `(days, source)`. `source` is one of `cursor`,
    `cursor_capped`, `default` (no cursor path, blank/absent file, or a
    non-positive age), or `cursor_unreadable` (any read/parse/schema/tz
    problem — fail back to the default window rather than crash the
    fetch; the precheck already fail-opens a corrupt cursor to a wake).
    """
    if not cursor_path:
        return default_days, "default"
    try:
        text = Path(cursor_path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return default_days, "default"
    except (OSError, UnicodeDecodeError):
        return default_days, "cursor_unreadable"
    if not text.strip():
        return default_days, "default"
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return default_days, "cursor_unreadable"
    if not isinstance(data, dict) or data.get("schema_version") != CURSOR_SCHEMA_VERSION:
        return default_days, "cursor_unreadable"
    last_run_raw = data.get("last_run")
    if not isinstance(last_run_raw, str):
        return default_days, "cursor_unreadable"
    normalised = last_run_raw[:-1] + "+00:00" if last_run_raw.endswith("Z") else last_run_raw
    try:
        last_run = datetime.fromisoformat(normalised)
    except ValueError:
        return default_days, "cursor_unreadable"
    if last_run.tzinfo is None:
        return default_days, "cursor_unreadable"
    age = now - last_run
    if age <= timedelta(0):
        # Cursor at/after now (clock skew / future stamp) — nothing older
        # to widen for; the default window is the safe floor.
        return default_days, "default"
    days = max(1, math.ceil(age.total_seconds() / 86400.0))
    if days > max_days:
        return max_days, "cursor_capped"
    return days, "cursor"


def _fetch_recent_threads(channel_id: str, cutoff: datetime, api_key: str) -> list:
    """Return top-level comments newer than `cutoff` across the channel.

    Each entry: {video_id, author, text, published_at}.
    """
    out = []
    page_token = None
    for _ in range(MAX_THREAD_PAGES):
        params = {
            "part": "snippet",
            "allThreadsRelatedToChannelId": channel_id,
            "order": "time",
            "maxResults": 100,
        }
        if page_token:
            params["pageToken"] = page_token
        body = _get("commentThreads", params, api_key)
        for item in body.get("items", []):
            top = item.get("snippet", {}).get("topLevelComment", {}).get("snippet", {})
            published = top.get("publishedAt")
            if not published:
                continue
            try:
                when = datetime.fromisoformat(published.replace("Z", "+00:00"))
            except ValueError:
                continue
            if when < cutoff:
                continue
            video_id = top.get("videoId")
            if not video_id:
                # No video to attribute/title the comment to — skip rather
                # than group it under a null key.
                continue
            out.append(
                {
                    "video_id": video_id,
                    # Coerce to "" so the stdout contract's string fields never
                    # carry null if the API omits author/text.
                    "author": top.get("authorDisplayName") or "",
                    # textOriginal is plain text; textDisplay carries HTML
                    # markup/entities that would render wrong in Telegram.
                    "text": top.get("textOriginal") or top.get("textDisplay") or "",
                    "published_at": published,
                }
            )
        page_token = body.get("nextPageToken")
        if not page_token:
            break
    else:
        # Loop exhausted the page cap with a token still pending — the fetch
        # is incomplete. Treat it as a failed/partial fetch (raise → exit
        # non-zero, no stdout) rather than returning a partial payload with
        # exit 0, which would let the skill stamp its success cursor and
        # suppress retries for the un-fetched comments. A personal channel's
        # bounded lookback window fits well under the cap, so this is a
        # busy-channel signal the operator should act on (raise the cap /
        # narrow the window).
        if page_token:
            raise YouTubeError(
                f"hit the {MAX_THREAD_PAGES}-page cap with more comment threads pending; "
                "treat as a partial fetch — raise MAX_THREAD_PAGES or narrow the window."
            )
    return out


def _fetch_titles(video_ids: list, api_key: str) -> dict:
    """Map video_id -> title via videos.list, chunked at the API's 50-id
    cap on the `id` parameter."""
    ids = [v for v in video_ids if v]
    titles: dict = {}
    for start in range(0, len(ids), VIDEOS_LIST_MAX_IDS):
        chunk = ids[start : start + VIDEOS_LIST_MAX_IDS]
        body = _get(
            "videos",
            {"part": "snippet", "id": ",".join(chunk), "maxResults": VIDEOS_LIST_MAX_IDS},
            api_key,
        )
        for item in body.get("items", []):
            if item.get("id"):
                titles[item["id"]] = item.get("snippet", {}).get("title", "")
    return titles


def _group_by_video(comments: list, titles: dict) -> list:
    by_video: dict = {}
    for c in comments:
        vid = c["video_id"]
        by_video.setdefault(vid, []).append(
            {"author": c["author"], "text": c["text"], "published_at": c["published_at"]}
        )
    videos = []
    for vid, items in by_video.items():
        videos.append(
            {
                "id": vid,
                "title": titles.get(vid, ""),
                "url": f"{WATCH_URL}{vid}" if vid else "",
                "comments": items,
            }
        )
    return videos


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Fetch recent YouTube channel comments")
    parser.add_argument("--channel-id", required=True)
    parser.add_argument(
        "--days",
        type=int,
        default=7,
        help="Fallback lookback window in days when no cursor is available (default: %(default)s).",
    )
    parser.add_argument(
        "--cursor",
        default=None,
        help=(
            "Path to the success cursor. When present and readable, the window "
            "spans since the last successful run instead of --days, so a missed "
            "week is re-covered. Falls back to --days otherwise."
        ),
    )
    parser.add_argument(
        "--max-days",
        type=int,
        default=DEFAULT_MAX_LOOKBACK_DAYS,
        help="Upper bound on a cursor-derived window (default: %(default)s).",
    )
    args = parser.parse_args(argv)

    if args.days <= 0:
        sys.stderr.write(
            f"fetch-youtube-comments: --days must be positive (got {args.days}); "
            "a non-positive window would disable the cutoff filter.\n"
        )
        return 2

    if args.max_days <= 0:
        sys.stderr.write(
            f"fetch-youtube-comments: --max-days must be positive (got {args.max_days}).\n"
        )
        return 2

    api_key = os.environ.get("YOUTUBE_API_KEY", "")
    if not api_key:
        sys.stderr.write(
            "fetch-youtube-comments: YOUTUBE_API_KEY is not set in the container env. "
            "The host must forward it (see nanoclaw container-runner CONTAINER_VARS).\n"
        )
        return 2

    now = _utcnow()
    window_days, window_source = _window_days_from_cursor(
        args.cursor, now, args.days, args.max_days
    )
    cutoff = now - timedelta(days=window_days)
    try:
        comments = _fetch_recent_threads(args.channel_id, cutoff, api_key)
        titles = _fetch_titles(sorted({c["video_id"] for c in comments if c["video_id"]}), api_key)
    except YouTubeError as exc:
        sys.stderr.write(f"fetch-youtube-comments: {exc}\n")
        return 1

    result = {
        "window_days": window_days,
        "window_source": window_source,
        "comment_count": len(comments),
        "videos": _group_by_video(comments, titles),
    }
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
