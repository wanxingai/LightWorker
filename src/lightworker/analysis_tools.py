"""Safe host-side tools for read-only research and analysis tasks."""

from __future__ import annotations

import ipaddress
import json
import os
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from .config import AnalysisConfig
from .policy import redact_text
from .repo_tools import tool_info
from .storage import RunStore

API_KEY_PATTERN = re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b")
URL_PATTERN = re.compile(r"https?://[^\s\]\[()<>{}\"'，。；、]+", re.IGNORECASE)
SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9_-]+$")


def sanitize_and_capture_credentials(texts: list[str]) -> tuple[list[str], dict[str, str]]:
    """Redact API keys and bind one supplied key to hosts mentioned alongside it."""
    keys: list[str] = []
    hosts: set[str] = set()
    for text in texts:
        keys.extend(match.group(0) for match in API_KEY_PATTERN.finditer(text))
        for raw_url in URL_PATTERN.findall(text):
            host = (urllib.parse.urlsplit(raw_url).hostname or "").lower()
            if host:
                hosts.add(host)
    unique_keys = list(dict.fromkeys(keys))
    credentials = {host: unique_keys[0] for host in hosts} if len(unique_keys) == 1 else {}
    return [redact_text(text) for text in texts], credentials


class CredentialVault:
    """Small local credential store; secrets are never exposed as model tool arguments."""

    def __init__(self, state_dir: Path):
        self.directory = state_dir.expanduser().resolve() / "credentials"

    def merge(self, root_run_id: str, credentials: dict[str, str]) -> None:
        if not credentials:
            return
        path = self._path(root_run_id)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            current = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
        except (OSError, json.JSONDecodeError):
            current = {}
        if not isinstance(current, dict):
            current = {}
        current.update({host.lower(): secret for host, secret in credentials.items()})
        temporary = path.with_name(f".{path.name}.tmp")
        temporary.write_text(json.dumps(current, ensure_ascii=False), encoding="utf-8")
        os.chmod(temporary, 0o600)
        temporary.replace(path)

    def get(self, root_run_id: str, host: str) -> str | None:
        path = self._path(root_run_id)
        if not path.is_file():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        value = payload.get(host.lower()) if isinstance(payload, dict) else None
        return str(value) if value else None

    def _path(self, root_run_id: str) -> Path:
        if not SAFE_IDENTIFIER.fullmatch(root_run_id):
            raise ValueError("unsafe credential scope")
        return self.directory / f"{root_run_id}.json"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str):
        return None


class AnalysisTools:
    def __init__(
        self,
        *,
        config: AnalysisConfig,
        store: RunStore,
        run_id: str,
        root_run_id: str,
        vault: CredentialVault,
    ):
        self.config = config
        self.store = store
        self.run_id = run_id
        self.root_run_id = root_run_id
        self.vault = vault
        self.request_count = 0
        self.tools = [self.http_request] if config.allow_http else []

    @tool_info(
        "http_request",
        "Fetch public HTTPS JSON or text for read-only analysis. GET and POST are supported. "
        "Credentials supplied by the user are attached automatically only to their bound host; "
        "never include secrets in arguments. Private networks, redirects, and unsafe schemes are blocked.",
        [
            {"name": "url", "description": "Public HTTPS URL", "type": "string", "required": True},
            {"name": "method", "description": "GET or POST", "type": "string", "required": False},
            {"name": "params", "description": "Optional query parameters", "type": "object", "required": False},
            {"name": "json_body", "description": "Optional JSON request body", "type": "object", "required": False},
        ],
    )
    def http_request(
        self,
        url: str,
        method: str = "GET",
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> str:
        self.request_count += 1
        if self.request_count > self.config.max_requests:
            return self._result({"ok": False, "error": "analysis HTTP request limit exceeded"})
        try:
            target = self._validated_url(url, params or {})
            parsed = urllib.parse.urlsplit(target)
            verb = method.strip().upper()
            if verb not in {"GET", "POST"}:
                raise ValueError("only GET and POST are allowed")
            payload = None
            headers = {
                "Accept": "application/json, text/plain;q=0.9, text/html;q=0.7",
                "User-Agent": "LightWorker/0.1 analysis",
            }
            if json_body is not None:
                payload = json.dumps(json_body, ensure_ascii=False).encode("utf-8")
                headers["Content-Type"] = "application/json"
            credential = self.vault.get(self.root_run_id, parsed.hostname or "")
            if credential:
                headers["Authorization"] = f"Bearer {credential}"
            request = urllib.request.Request(target, data=payload, headers=headers, method=verb)
            opener = urllib.request.build_opener(_NoRedirect())
            try:
                with opener.open(request, timeout=self.config.request_timeout_seconds) as response:
                    status = int(response.status)
                    content_type = str(response.headers.get("Content-Type") or "")
                    raw = response.read(self.config.max_response_bytes + 1)
            except urllib.error.HTTPError as exc:
                status = int(exc.code)
                content_type = str(exc.headers.get("Content-Type") or "") if exc.headers else ""
                raw = exc.read(self.config.max_response_bytes + 1)
            truncated = len(raw) > self.config.max_response_bytes
            raw = raw[: self.config.max_response_bytes]
            body = redact_text(raw.decode("utf-8", errors="replace"))
            result = {
                "ok": 200 <= status < 300,
                "status": status,
                "url": target,
                "content_type": content_type,
                "credential_attached": bool(credential),
                "truncated": truncated,
                "body": body,
            }
        except (ValueError, OSError, urllib.error.URLError) as exc:
            result = {"ok": False, "error": redact_text(str(exc)), "url": redact_text(url)}
        self._audit(method, url, result)
        return self._result(result)

    def _validated_url(self, url: str, params: dict[str, Any]) -> str:
        if len(url) > 4096:
            raise ValueError("URL is too long")
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme.lower() != "https" or not parsed.hostname:
            raise ValueError("only public HTTPS URLs are allowed")
        host = parsed.hostname.lower()
        if parsed.username or parsed.password:
            raise ValueError("URL credentials are forbidden")
        if self.config.allowed_hosts and host not in {item.lower() for item in self.config.allowed_hosts}:
            raise ValueError(f"host is not allowed: {host}")
        port = parsed.port or 443
        for info in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM):
            address = ipaddress.ip_address(info[4][0])
            if not address.is_global:
                raise ValueError("private or non-global network targets are forbidden")
        query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
        query.extend((str(key), str(value)) for key, value in params.items())
        return urllib.parse.urlunsplit(
            (parsed.scheme, parsed.netloc, parsed.path or "/", urllib.parse.urlencode(query), "")
        )

    def _audit(self, method: str, url: str, result: dict[str, Any]) -> None:
        safe_result = dict(result)
        if "body" in safe_result:
            safe_result["body"] = str(safe_result["body"])[:8000]
        self.store.write_text(
            self.run_id,
            f"logs/http-{self.request_count}.log",
            redact_text(
                json.dumps(
                    {"method": method.upper(), "url": url, "result": safe_result},
                    ensure_ascii=False,
                    indent=2,
                )
            ),
        )

    @staticmethod
    def _result(value: dict[str, Any]) -> str:
        return json.dumps(value, ensure_ascii=False)
