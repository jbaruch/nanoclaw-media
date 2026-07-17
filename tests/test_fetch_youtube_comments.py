"""Tests for skills/youtube-comment-check/scripts/fetch-youtube-comments.py.

Locks down the native YouTube Data API v3 fetch contract (nanoclaw-admin#339):

  - commentThreads.list (allThreadsRelatedToChannelId) threads are
    filtered to the `--days` window and grouped by video
  - videos.list supplies titles; output carries id/title/url/comments
  - `comment_count == 0` is a valid quiet-week result (exit 0)
  - missing YOUTUBE_API_KEY exits 2; an API error envelope exits 1
  - pagination follows nextPageToken

The script reads YOUTUBE_API_BASE from the env, so a local fixture
server stands in for googleapis.com. The script's clock is frozen at
FROZEN_NOW via its `_utcnow` seam, and window fixtures are fixed
offsets from it (recent = FROZEN_NOW-1h, old = FROZEN_NOW-8d), so the
7-day filter is exercised with no dependence on the real wall clock.
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

# Fixed reference "now" — a past date, never the real clock
# (coding-policy testing-standards: control the clock).
FROZEN_NOW = datetime(2026, 6, 15, 12, 0, 0, tzinfo=timezone.utc)


def _load():
    spec = importlib.util.spec_from_file_location("fetch_youtube_comments_under_test", SCRIPT_PATH)
    if spec is None or spec.loader is None:
        raise ImportError("cannot load module spec")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    # Freeze the script's clock for every test; the module object is a
    # per-test throwaway, so the assignment needs no teardown. setattr
    # (not plain assignment, hence the B010 suppression) is what lets
    # pyright accept the dynamically-loaded module's unknown attribute.
    setattr(module, "_utcnow", lambda: FROZEN_NOW)  # noqa: B010
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
    recent = FROZEN_NOW - timedelta(hours=1)
    old = FROZEN_NOW - timedelta(days=8)
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
    recent = FROZEN_NOW - timedelta(hours=2)
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
    recent = FROZEN_NOW - timedelta(hours=1)
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
    recent = FROZEN_NOW - timedelta(hours=1)
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


def test_nonpositive_max_days_exits_2(server, capsys):
    module = _load()
    rc = module.main(["--channel-id", CHANNEL, "--max-days", "0"])
    out = capsys.readouterr()
    assert rc == 2
    assert "--max-days must be positive" in out.err
    assert out.out.strip() == ""


# ---------------------------------------------------------------------------
# _window_days_from_cursor — the fetch window spans since the last success
# (jbaruch/nanoclaw#803) so a missed week is re-covered, not lost.
# ---------------------------------------------------------------------------


def _write_cursor(tmp_path, last_run):
    cursor = tmp_path / "cursor.json"
    cursor.write_text(json.dumps({"schema_version": 1, "last_run": last_run}))
    return cursor


def test_window_default_when_no_cursor_path():
    module = _load()
    assert module._window_days_from_cursor(None, FROZEN_NOW, 7, 35) == (7, "default")


def test_window_default_when_cursor_absent(tmp_path):
    module = _load()
    missing = tmp_path / "nope.json"
    assert module._window_days_from_cursor(str(missing), FROZEN_NOW, 7, 35) == (7, "default")


def test_window_default_when_cursor_blank(tmp_path):
    module = _load()
    cursor = tmp_path / "cursor.json"
    cursor.write_text("   \n")
    assert module._window_days_from_cursor(str(cursor), FROZEN_NOW, 7, 35) == (7, "default")


def test_window_spans_since_last_success(tmp_path):
    module = _load()
    cursor = _write_cursor(
        tmp_path, (FROZEN_NOW - timedelta(days=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
    )
    assert module._window_days_from_cursor(str(cursor), FROZEN_NOW, 7, 35) == (10, "cursor")


def test_window_ceils_partial_day(tmp_path):
    # Weekly near-miss: cursor stamped ~6d23h ago rounds up to a 7-day window.
    module = _load()
    last_run = (FROZEN_NOW - timedelta(days=6, hours=23)).strftime("%Y-%m-%dT%H:%M:%SZ")
    cursor = _write_cursor(tmp_path, last_run)
    assert module._window_days_from_cursor(str(cursor), FROZEN_NOW, 7, 35) == (7, "cursor")


def test_window_capped_at_max_days(tmp_path):
    module = _load()
    cursor = _write_cursor(
        tmp_path, (FROZEN_NOW - timedelta(days=100)).strftime("%Y-%m-%dT%H:%M:%SZ")
    )
    assert module._window_days_from_cursor(str(cursor), FROZEN_NOW, 7, 35) == (35, "cursor_capped")


def test_window_default_on_future_cursor(tmp_path):
    module = _load()
    cursor = _write_cursor(
        tmp_path, (FROZEN_NOW + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    )
    assert module._window_days_from_cursor(str(cursor), FROZEN_NOW, 7, 35) == (7, "default")


def test_window_unreadable_on_schema_mismatch(tmp_path):
    module = _load()
    cursor = tmp_path / "cursor.json"
    cursor.write_text(json.dumps({"schema_version": 99, "last_run": "2026-06-01T00:00:00Z"}))
    assert module._window_days_from_cursor(str(cursor), FROZEN_NOW, 7, 35) == (
        7,
        "cursor_unreadable",
    )


def test_window_unreadable_on_malformed_json(tmp_path):
    module = _load()
    cursor = tmp_path / "cursor.json"
    cursor.write_text("{not json")
    assert module._window_days_from_cursor(str(cursor), FROZEN_NOW, 7, 35) == (
        7,
        "cursor_unreadable",
    )


def test_window_unreadable_on_naive_datetime(tmp_path):
    module = _load()
    cursor = _write_cursor(tmp_path, "2026-06-01T00:00:00")  # no Z / offset
    assert module._window_days_from_cursor(str(cursor), FROZEN_NOW, 7, 35) == (
        7,
        "cursor_unreadable",
    )


def test_main_uses_cursor_window_to_recover_missed_week(server, capsys, tmp_path):
    # Cursor 20d old (a run that has been failing/gated): a comment 10d old
    # falls outside the fixed 7-day default but inside the cursor window and
    # MUST be fetched; one 25d old stays outside.
    cursor = _write_cursor(
        tmp_path, (FROZEN_NOW - timedelta(days=20)).strftime("%Y-%m-%dT%H:%M:%SZ")
    )
    server.comment_pages = [
        {
            "items": [
                _thread("vid1", "Alice", "ten days ago", FROZEN_NOW - timedelta(days=10)),
                _thread("vid1", "Bob", "too old", FROZEN_NOW - timedelta(days=25)),
            ]
        }
    ]
    server.videos_response = {"items": [{"id": "vid1", "snippet": {"title": "T"}}]}
    module = _load()
    rc = module.main(["--channel-id", CHANNEL, "--days", "7", "--cursor", str(cursor)])
    out = capsys.readouterr()
    assert rc == 0, out.err
    payload = json.loads(out.out.strip())
    assert payload["window_days"] == 20
    assert payload["window_source"] == "cursor"
    assert payload["comment_count"] == 1
    assert payload["videos"][0]["comments"][0]["text"] == "ten days ago"
