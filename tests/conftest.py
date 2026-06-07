import importlib.util
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


def _load(name: str, relpath: str):
    spec = importlib.util.spec_from_file_location(name, REPO_ROOT / relpath)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def trakt_watch_history():
    """Load trakt-watch-history/scripts/trakt-watch-history.py.

    The script reads `TRAKT_CLIENT_ID` and `TRAKT_ACCESS_TOKEN` at
    `main()` time, so tests use `monkeypatch.setenv(...)` before invoking
    main(); no module-level paths to redirect. Tests patch
    `urllib.request.urlopen` to drive the `api_get` HTTP / network / JSON
    branches without real requests."""
    return _load(
        "trakt_watch_history_under_test",
        "skills/trakt-watch-history/scripts/trakt-watch-history.py",
    )
