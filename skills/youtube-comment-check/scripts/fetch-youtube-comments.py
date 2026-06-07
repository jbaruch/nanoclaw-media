#!/usr/bin/env python3
"""Fetch recent comment threads on a YouTube channel over the native
YouTube Data API v3 (nanoclaw-admin#339).

Composio's YouTube toolkit has no comment-threads tool, so this skill
cannot run on Composio at all (the `Composio Tool Access` rule's
"Out of Composio's reach" note). It calls the YouTube Data API v3
directly with `YOUTUBE_API_KEY`:

  1. commentThreads.list?allThreadsRelatedToChannelId=<channel> — recent
     threads across every video on the channel, newest first, paginated.
  2. videos.list?id=<ids> — titles for the videos those comments landed
     on (one batched call).

Comments are filtered to the last `--days` (default 7) by each thread's
top-level-comment `publishedAt`, then grouped by video.

Usage
-----
    fetch-youtube-comments.py --channel-id <id> [--days 7]

Output
------
On success: single-line JSON on stdout, exit 0:
    {"window_days": 7, "comment_count": N,
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
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

# rstrip the trailing slash so the `{API_BASE}/{path}` join never doubles.
API_BASE = os.environ.get("YOUTUBE_API_BASE", "https://www.googleapis.com/youtube/v3").rstrip("/")
PER_CALL_TIMEOUT_SECONDS = 60.0
# Defensive page cap: a personal channel's last-week comments fit in one
# 100-item page; cap pagination so a busy channel can't loop unbounded.
MAX_THREAD_PAGES = 5
# videos.list caps the `id` parameter at 50 ids per request.
VIDEOS_LIST_MAX_IDS = 50
WATCH_URL = "https://www.youtube.com/watch?v="


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
        # 7-day window fits well under the cap, so this is a busy-channel
        # signal the operator should act on (raise the cap / narrow the run).
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
    parser.add_argument("--days", type=int, default=7)
    args = parser.parse_args(argv)

    if args.days <= 0:
        sys.stderr.write(
            f"fetch-youtube-comments: --days must be positive (got {args.days}); "
            "a non-positive window would disable the cutoff filter.\n"
        )
        return 2

    api_key = os.environ.get("YOUTUBE_API_KEY", "")
    if not api_key:
        sys.stderr.write(
            "fetch-youtube-comments: YOUTUBE_API_KEY is not set in the container env. "
            "The host must forward it (see nanoclaw container-runner CONTAINER_VARS).\n"
        )
        return 2

    cutoff = datetime.now(timezone.utc) - timedelta(days=args.days)
    try:
        comments = _fetch_recent_threads(args.channel_id, cutoff, api_key)
        titles = _fetch_titles(sorted({c["video_id"] for c in comments if c["video_id"]}), api_key)
    except YouTubeError as exc:
        sys.stderr.write(f"fetch-youtube-comments: {exc}\n")
        return 1

    result = {
        "window_days": args.days,
        "comment_count": len(comments),
        "videos": _group_by_video(comments, titles),
    }
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
