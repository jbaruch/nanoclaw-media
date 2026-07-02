"""Tests for skills/youtube-comment-check/scripts/fetch-youtube-comments.py.

Locks down the native YouTube Data API v3 fetch contract (nanoclaw-admin#339):

  - commentThreads.list (allThreadsRelatedToChannelId) threads are
    filtered to the `--days` window and grouped by video
  - videos.list supplies titles; output carries id/title/url/comments
  - `comment_count == 0` is a valid quiet-week result (exit 0)
  - missing YOUTUBE_API_KEY exits 2; an API error envelope exits 1
  - pagination follows nextPageToken

The script reads YOUTUBE_API_BASE from the env, so a local fixture
server stands in for googleapis.com. Window timestamps are computed
relative to now (recent = now-1h, old = now-8d) so the 7-day filter is
exercised deterministically without freezing the clock.
"""

import importlib.util
import json
import threading
import urllib.parse
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import cast

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "skills/youtube-comment-check/scripts/fetch-youtube-comments.py"

CHANNEL = "UCZ8-VX2SiAIBE7guw7NG-Sg"


def _load():
    spec = importlib.util.spec_from_file_location("fetch_youtube_comments_under_test", SCRIPT_PATH)
    if spec is None or spec.loader is None:
        raise ImportError("cannot load module spec")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _thread(video_id, author, text, when):
    return {
        "snippet": {
            "topLevelComment": {
                "snippet": {
                    "videoId": video_id,
                    "authorDisplayName": author,
                    # API returns both; the script prefers textOriginal (plain).
                    "textOriginal": text,
                    "textDisplay": f"<b>{text}</b>",
                    "publishedAt": when.strftime("%Y-%m-%dT%H:%M:%SZ"),
                }
            }
        }
    }


class _CommentServer(HTTPServer):
    """HTTPServer carrying the per-test fixture state the handler reads.

    Declaring these as typed attributes (rather than stamping them onto a
    bare HTTPServer instance) lets pyright resolve `self.server.<attr>`
    inside the handler."""

    def __init__(self, server_address, handler_class):
        super().__init__(server_address, handler_class)
        self.requests_seen: list[tuple[str, dict]] = []
        self.comment_pages: list[dict] = [{"items": []}]
        self.comment_idx: int = 0
        self.videos_response: dict = {"items": []}
        self.comment_status: int = 200
        self.comment_error_body: str = ""


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 — BaseHTTPRequestHandler API
        # self.server is typed as BaseServer by the base handler; narrow it
        # to the fixture server that actually carries the test state.
        server = cast(_CommentServer, self.server)
        parsed = urllib.parse.urlparse(self.path)
        endpoint = parsed.path.rsplit("/", 1)[-1]
        server.requests_seen.append((endpoint, urllib.parse.parse_qs(parsed.query)))
        if endpoint == "commentThreads":
            if server.comment_status != 200:
                # Error-body path: send the configured status + raw body
                # (used to prove the API key is redacted from diagnostics).
                raw = server.comment_error_body.encode("utf-8")
                self.send_response(server.comment_status)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
                return
            idx = min(server.comment_idx, len(server.comment_pages) - 1)
            payload = server.comment_pages[idx]
            server.comment_idx += 1
        elif endpoint == "videos":
            payload = server.videos_response
        else:
            payload = {}
        body = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):  # match BaseHTTPRequestHandler.log_message
        return


@pytest.fixture
def server(monkeypatch):
    httpd = _CommentServer(("127.0.0.1", 0), _Handler)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    monkeypatch.setenv("YOUTUBE_API_BASE", f"http://127.0.0.1:{port}/youtube/v3")
    monkeypatch.setenv("YOUTUBE_API_KEY", "yt_test")
    try:
        yield httpd
    finally:
        httpd.shutdown()
        httpd.server_close()


def _run(module, capsys):
    rc = module.main(["--channel-id", CHANNEL, "--days", "7"])
    out = capsys.readouterr()
    return rc, out


def test_groups_recent_comments_by_video_and_filters_old(server, capsys):
    now = datetime.now(timezone.utc)
    recent = now - timedelta(hours=1)
    old = now - timedelta(days=8)
    server.comment_pages = [
        {
            "items": [
                _thread("vid1", "Alice", "great talk", recent),
                _thread("vid1", "Bob", "thanks!", recent),
                _thread("vid2", "Carol", "subscribed", recent),
                _thread("vid1", "Dave", "old comment", old),  # outside window
            ]
        }
    ]
    server.videos_response = {
        "items": [
            {"id": "vid1", "snippet": {"title": "Kotlin Coroutines"}},
            {"id": "vid2", "snippet": {"title": "Gradle Tips"}},
        ]
    }
    module = _load()
    rc, out = _run(module, capsys)

    assert rc == 0, out.err
    payload = json.loads(out.out.strip())
    assert payload["window_days"] == 7
    assert payload["comment_count"] == 3  # old one filtered out
    by_id = {v["id"]: v for v in payload["videos"]}
    assert by_id["vid1"]["title"] == "Kotlin Coroutines"
    assert by_id["vid1"]["url"] == "https://www.youtube.com/watch?v=vid1"
    assert len(by_id["vid1"]["comments"]) == 2
    assert by_id["vid2"]["comments"][0]["author"] == "Carol"
    # textOriginal (plain) is used, not textDisplay (HTML markup).
    assert by_id["vid2"]["comments"][0]["text"] == "subscribed"
    # videos.list batched the ids it actually needed (window-filtered).
    endpoints = [e for e, _ in server.requests_seen]
    assert endpoints.count("videos") == 1


def test_quiet_week_is_success_with_zero_count(server, capsys):
    module = _load()
    rc, out = _run(module, capsys)
    assert rc == 0
    payload = json.loads(out.out.strip())
    assert payload["comment_count"] == 0
    assert payload["videos"] == []


def test_pagination_follows_next_page_token(server, capsys):
    now = datetime.now(timezone.utc)
    recent = now - timedelta(hours=2)
    server.comment_pages = [
        {"items": [_thread("vid1", "A", "one", recent)], "nextPageToken": "PAGE2"},
        {"items": [_thread("vid1", "B", "two", recent)]},
    ]
    server.videos_response = {"items": [{"id": "vid1", "snippet": {"title": "T"}}]}
    module = _load()
    rc, out = _run(module, capsys)
    assert rc == 0
    payload = json.loads(out.out.strip())
    assert payload["comment_count"] == 2
    assert [e for e, _ in server.requests_seen].count("commentThreads") == 2


def test_page_cap_truncation_fails_without_advancing(server, capsys):
    now = datetime.now(timezone.utc)
    recent = now - timedelta(hours=1)
    # Every page returns a nextPageToken → the loop exhausts the cap with a
    # token still pending. A partial fetch must fail (exit 1, no stdout) so
    # the skill does NOT stamp its success cursor and retries.
    module = _load()
    server.comment_pages = [
        {"items": [_thread(f"vid{i}", f"U{i}", "x", recent)], "nextPageToken": "MORE"}
        for i in range(module.MAX_THREAD_PAGES)
    ]
    rc, out = _run(module, capsys)
    assert rc == 1
    assert [e for e, _ in server.requests_seen].count("commentThreads") == module.MAX_THREAD_PAGES
    assert "page cap" in out.err
    assert out.out.strip() == ""


def test_missing_api_key_exits_2(server, capsys, monkeypatch):
    monkeypatch.delenv("YOUTUBE_API_KEY", raising=False)
    module = _load()
    rc, out = _run(module, capsys)
    assert rc == 2
    assert "YOUTUBE_API_KEY is not set" in out.err
    assert out.out.strip() == ""


def test_api_error_envelope_exits_1(server, capsys):
    server.comment_pages = [{"error": {"code": 403, "message": "quotaExceeded"}}]
    module = _load()
    rc, out = _run(module, capsys)
    assert rc == 1
    assert "YouTube API error" in out.err
    assert out.out.strip() == ""


def test_videos_list_chunks_over_50_ids(server, capsys):
    now = datetime.now(timezone.utc)
    recent = now - timedelta(hours=1)
    # 60 distinct videos, one recent comment each → videos.list (50-id cap)
    # must split into two requests.
    server.comment_pages = [
        {"items": [_thread(f"vid{i}", f"User{i}", "nice", recent) for i in range(60)]}
    ]
    server.videos_response = {
        "items": [{"id": f"vid{i}", "snippet": {"title": f"Title {i}"}} for i in range(60)]
    }
    module = _load()
    rc, out = _run(module, capsys)
    assert rc == 0, out.err
    payload = json.loads(out.out.strip())
    assert payload["comment_count"] == 60
    assert len(payload["videos"]) == 60
    assert [e for e, _ in server.requests_seen].count("videos") == 2


def test_api_key_redacted_from_error_body(server, capsys):
    server.comment_status = 403
    server.comment_error_body = '{"error":{"message":"bad key yt_test in request"}}'
    module = _load()
    rc, out = _run(module, capsys)
    assert rc == 1
    assert "yt_test" not in out.err  # redacted before reaching stderr
    assert "***" in out.err


def test_api_key_redacted_from_error_envelope(server, capsys):
    # HTTP 200 carrying an {"error": ...} envelope that echoes the key.
    server.comment_pages = [{"error": {"message": "invalid key yt_test"}}]
    module = _load()
    rc, out = _run(module, capsys)
    assert rc == 1
    assert "YouTube API error" in out.err
    assert "yt_test" not in out.err
    assert "***" in out.err


def test_nonpositive_days_exits_2(server, capsys):
    module = _load()
    rc = module.main(["--channel-id", CHANNEL, "--days", "0"])
    out = capsys.readouterr()
    assert rc == 2
    assert "--days must be positive" in out.err
    assert out.out.strip() == ""
