"""Regression for cloud state crossing GitHub's inline Contents size limit."""
import base64
import copy
import io
import json
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from quant_assistant.cloud_snapshot import build_snapshot, encode_snapshot
from quant_assistant.cloud_state import CloudStateStore, new_cloud_state
from quant_assistant.data.fetcher import DataFetcher
from quant_assistant.telegram_trade_bot import TelegramTradeBot
from test_telegram_trade_feedback import make_service, FakeTelegramFetcher

SHA = "a" * 40


def response(value):
    return io.BytesIO(json.dumps(value).encode())


def test_small_file_and_missing_file():
    fetcher = DataFetcher()
    with patch.object(fetcher, "_github_contents_request", return_value={
        "sha": SHA, "encoding": "base64", "content": "e30=\n"
    }) as request:
        assert fetcher.get_github_repository_file("token", "owner/repo", "state.json") == {
            "sha": SHA, "content": b"{}"}
        assert request.call_count == 1
    with patch.object(fetcher, "_github_contents_request", return_value=None):
        assert fetcher.get_github_repository_file("token", "owner/repo", "state.json") is None


def test_large_legacy_state_load_bot_and_round_trip_without_market_data():
    with tempfile.TemporaryDirectory() as temp:
        state = new_cloud_state(encode_snapshot(build_snapshot(make_service(Path(temp)))))
    # Legacy advice deliberately lacks every newly added market diagnostic.
    state["advice_records"] = [{"advice_id": "legacy", "notes": "x" * 1_060_000}]
    original = copy.deepcopy(state)
    content = json.dumps(state).encode()
    assert len(content) > 1_048_576
    metadata = {"sha": SHA, "encoding": "none", "content": "", "size": len(content)}
    blob = {"sha": SHA, "encoding": "base64", "content": base64.b64encode(content).decode()}
    fetcher = DataFetcher()
    store = CloudStateStore("token", "owner/repo", fetcher=fetcher)
    with patch("urllib.request.urlopen", side_effect=lambda request, **kw: response(
        blob if "/git/blobs/" in request.full_url else metadata
    )) as request:
        loaded, sha = store.load()
        assert loaded == original
        assert sha == SHA
        assert request.call_args[0][0].full_url.endswith("/git/blobs/" + SHA)
        assert request.call_args[0][0].get_header("Authorization") == "Bearer token"
        with tempfile.TemporaryDirectory() as temp:
            bot = TelegramTradeBot("token", "123", store, FakeTelegramFetcher([]),
                                   Path(temp) / "ledger.sqlite3", Path(temp) / "portfolio.json")
            assert bot.run_once() == {"status": "idle"}
            assert not (Path(temp) / "ledger.sqlite3").exists()
    with patch.object(fetcher, "put_github_repository_file", return_value="new") as put:
        assert store.save(loaded, sha, "test") == "new"
        assert json.loads(put.call_args.args[3]) == loaded
        assert put.call_args.args[-1] == SHA
        assert loaded["account_snapshot_b64"] == original["account_snapshot_b64"]
        assert loaded["advice_records"] == original["advice_records"]


@pytest.mark.parametrize("blob", [None, {"sha": "b" * 40, "encoding": "base64", "content": "e30="},
    {"sha": SHA, "encoding": "none", "content": ""},
    {"sha": SHA, "encoding": "base64", "content": "%%%"}])
def test_bad_blob_fails_closed(blob):
    fetcher = DataFetcher()
    with patch.object(fetcher, "_github_contents_request", side_effect=[
        {"sha": SHA, "encoding": "none", "content": ""}, blob
    ]):
        with pytest.raises(RuntimeError, match="GitHub"):
            fetcher.get_github_repository_file("token", "owner/repo", "state.json")


def test_blob_sha_cannot_change_request_destination():
    with patch("urllib.request.urlopen") as request:
        with pytest.raises(RuntimeError, match="SHA"):
            DataFetcher._github_contents_request("token", "owner/repo", "state.json",
                                                  blob_sha="https://elsewhere.invalid")
        request.assert_not_called()
