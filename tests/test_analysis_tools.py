from __future__ import annotations

import json
import os
import socket
import urllib.request
from pathlib import Path
from typing import Any

from lightworker.analysis_tools import (
    AnalysisTools,
    CredentialVault,
    sanitize_and_capture_credentials,
)
from lightworker.config import AnalysisConfig
from lightworker.models import RunRecord
from lightworker.storage import RunStore


def test_credentials_are_redacted_and_bound_to_mentioned_host(tmp_path: Path):
    secret = "sk-exampleSecretValue1234567890"
    sanitized, credentials = sanitize_and_capture_credentials(
        [f'api-key="{secret}" docs https://api.example.com/docs']
    )
    vault = CredentialVault(tmp_path / "state")
    vault.merge("root-run", credentials)

    assert secret not in sanitized[0]
    assert "[redacted]" in sanitized[0]
    assert vault.get("root-run", "api.example.com") == secret
    assert vault.get("root-run", "other.example.com") is None
    mode = os.stat(tmp_path / "state" / "credentials" / "root-run.json").st_mode & 0o777
    assert mode == 0o600


def test_http_tool_blocks_private_network_targets(tmp_path: Path):
    store = RunStore(tmp_path / "state")
    store.create(RunRecord(run_id="analysis-run", task="test", repo="/repo"))
    tool = AnalysisTools(
        config=AnalysisConfig(),
        store=store,
        run_id="analysis-run",
        root_run_id="analysis-run",
        vault=CredentialVault(tmp_path / "state"),
    )

    result = json.loads(tool.http_request("https://127.0.0.1/private"))

    assert result["ok"] is False
    assert "private or non-global" in result["error"]


def test_http_tool_injects_host_bound_credential_without_logging_it(
    tmp_path: Path,
    monkeypatch: Any,
):
    secret = "sk-exampleSecretValue1234567890"
    store = RunStore(tmp_path / "state")
    store.create(RunRecord(run_id="analysis-run", task="test", repo="/repo"))
    vault = CredentialVault(tmp_path / "state")
    vault.merge("root-run", {"api.example.com": secret})
    captured: dict[str, Any] = {}

    class Response:
        status = 200
        headers = {"Content-Type": "application/json"}

        def read(self, _: int) -> bytes:
            return b'{"status":200,"message":[{"title":"evidence"}]}'

        def __enter__(self):
            return self

        def __exit__(self, *args: Any):
            return None

    class Opener:
        def open(self, request: urllib.request.Request, timeout: int):
            captured["authorization"] = request.get_header("Authorization")
            captured["timeout"] = timeout
            captured["method"] = request.method
            return Response()

    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))],
    )
    monkeypatch.setattr(urllib.request, "build_opener", lambda *handlers: Opener())
    tool = AnalysisTools(
        config=AnalysisConfig(),
        store=store,
        run_id="analysis-run",
        root_run_id="root-run",
        vault=vault,
    )

    result = json.loads(
        tool.http_request(
            "https://api.example.com/v1/search",
            method="POST",
            json_body={"query": "market"},
        )
    )

    assert result["ok"] is True
    assert result["credential_attached"] is True
    assert captured["authorization"] == f"Bearer {secret}"
    assert captured["method"] == "POST"
    audit = store.artifact_path("analysis-run", "logs/http-1.log").read_text()
    assert secret not in audit
    assert "evidence" in audit


def test_html_response_is_simplified_for_model_context(tmp_path: Path):
    store = RunStore(tmp_path / "state")
    store.create(RunRecord(run_id="analysis-run", task="test", repo="/repo"))
    tool = AnalysisTools(
        config=AnalysisConfig(),
        store=store,
        run_id="analysis-run",
        root_run_id="analysis-run",
        vault=CredentialVault(tmp_path / "state"),
    )
    result = tool._simplify_html_result(
        {
            "ok": True,
            "status": 200,
            "url": "https://example.com/news",
            "body": (
                "<html><head><title>Market News</title>"
                '<meta name="description" content="Daily evidence"></head>'
                "<body><script>hugeNoise()</script><p>LPG inventories fell.</p>"
                '<a href="/source">Source</a></body></html>'
            ),
        }
    )

    assert result["html_simplified"] is True
    assert "Market News" in result["body"]
    assert "LPG inventories fell" in result["body"]
    assert "hugeNoise" not in result["body"]
    assert "https://example.com/source" in result["body"]


def test_web_search_falls_back_to_google_news_rss(tmp_path: Path, monkeypatch: Any):
    store = RunStore(tmp_path / "state")
    store.create(RunRecord(run_id="analysis-run", task="test", repo="/repo"))
    tool = AnalysisTools(
        config=AnalysisConfig(),
        store=store,
        run_id="analysis-run",
        root_run_id="analysis-run",
        vault=CredentialVault(tmp_path / "state"),
    )
    responses = iter(
        [
            json.dumps({"ok": True, "status": 202, "body": "human challenge"}),
            json.dumps(
                {
                    "ok": True,
                    "status": 200,
                    "body": (
                        "<rss><channel><item><title>Iran update</title>"
                        "<link>https://news.example/item</link>"
                        "<description>Verified report</description>"
                        "<pubDate>Mon, 10 Aug 2026 10:00:00 GMT</pubDate>"
                        '<source url="https://news.example">Example News</source>'
                        "</item></channel></rss>"
                    ),
                }
            ),
        ]
    )
    monkeypatch.setattr(tool, "http_request", lambda *args, **kwargs: next(responses))

    result = json.loads(tool.web_search("US Iran latest"))

    assert result["ok"] is True
    assert result["provider"] == "google_news_rss_fallback"
    assert result["results"][0]["source"] == "Example News"
