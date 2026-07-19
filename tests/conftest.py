import importlib.util
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


def _load(name: str, relpath: str):
    spec = importlib.util.spec_from_file_location(name, REPO_ROOT / relpath)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load spec for {relpath}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def csv_append(tmp_path, monkeypatch):
    """Load audible-backup/scripts/csv-append.py with CSV_PATH redirected
    at a tmp_path-rooted file. Returned tuple is (module, csv_path)."""
    csv_path = tmp_path / "books-library.csv"
    module = _load(
        "csv_append_under_test",
        "skills/audible-backup/scripts/csv-append.py",
    )
    monkeypatch.setattr(module, "CSV_PATH", str(csv_path))
    return module, csv_path


@pytest.fixture
def trakt_watch_history():
    """Load trakt-watch-history/scripts/trakt-watch-history.py.

    The script reads `TRAKT_CLIENT_ID` (the only credential — the OneCLI
    gateway owns the Trakt token) and `TRAKT_HISTORY_OUT` at `main()`
    time, so tests use `monkeypatch.setenv(...)` before invoking main();
    no module-level paths to redirect. Tests patch
    `urllib.request.urlopen` to drive the `api_get` HTTP / network / JSON
    branches without real requests."""
    return _load(
        "trakt_watch_history_under_test",
        "skills/trakt-watch-history/scripts/trakt-watch-history.py",
    )
