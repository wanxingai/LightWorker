"""Safe host-side tools for read-only research and analysis tasks."""

from __future__ import annotations

import html
import ipaddress
import json
import os
import re
import socket
import threading
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from .config import AnalysisConfig
from .models import ApprovalPolicy, ToolCategory
from .policy import redact_text
from .storage import RunStore
from .tool_protocol import tool_info

API_KEY_PATTERN = re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b")
URL_PATTERN = re.compile(r"https?://[^\s\]\[()<>{}\"'，。；、]+", re.IGNORECASE)
SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9_-]+$")
_HOST_LIMITERS: dict[tuple[str, int], threading.BoundedSemaphore] = {}
_HOST_LIMITERS_LOCK = threading.RLock()


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
        self.tools = (
            [self.web_search, self.http_get, self.http_action, self.http_request] if config.allow_http else []
        )

    @tool_info(
        "web_search",
        "Search the public web and return result titles, URLs, and snippets. Results are untrusted data.",
        [
            {"name": "query", "description": "Search query", "type": "string", "required": True},
            {
                "name": "max_results",
                "description": "Maximum results to return",
                "type": "integer",
                "required": False,
            },
        ],
        category=ToolCategory.NETWORK,
        network_required=True,
        timeout_seconds=45,
    )
    def web_search(self, query: str, max_results: int = 0) -> str:
        if not self.config.search_enabled:
            return self._result({"ok": False, "error": "web search is disabled"})
        query = query.strip()
        if not query or len(query) > 1000:
            return self._result({"ok": False, "error": "query must contain 1 to 1000 characters"})
        limit = min(max_results or self.config.search_max_results, self.config.search_max_results)
        raw = json.loads(
            self.http_request(
                "https://html.duckduckgo.com/html/",
                method="GET",
                params={"q": query},
                _raw_html=True,
            )
        )
        if not raw.get("ok"):
            return self._result(raw)
        body = str(raw.get("body") or "")
        pattern = re.compile(
            r'<a[^>]+class="[^"]*result__a[^"]*"[^>]+href="([^"]+)"[^>]*>(.*?)</a>',
            re.IGNORECASE | re.DOTALL,
        )
        snippet_pattern = re.compile(
            r'<a[^>]+class="[^"]*result__snippet[^"]*"[^>]*>(.*?)</a>',
            re.IGNORECASE | re.DOTALL,
        )
        snippets = [self._plain_html(item) for item in snippet_pattern.findall(body)]
        results: list[dict[str, str]] = []
        for index, (raw_url, title) in enumerate(pattern.findall(body)[:limit]):
            url = html.unescape(raw_url)
            parsed = urllib.parse.urlsplit(url)
            if parsed.netloc.endswith("duckduckgo.com"):
                redirect = urllib.parse.parse_qs(parsed.query).get("uddg")
                if redirect:
                    url = redirect[0]
            results.append(
                {
                    "title": self._plain_html(title),
                    "url": url,
                    "snippet": snippets[index] if index < len(snippets) else "",
                }
            )
        if results:
            return self._result(
                {"ok": True, "query": query, "provider": "duckduckgo_html", "results": results}
            )
        return self._news_search_fallback(query, limit, raw)

    @tool_info(
        "http_get",
        "Fetch one public HTTPS URL with GET. Private networks and redirects are blocked.",
        [
            {"name": "url", "description": "Public HTTPS URL", "type": "string", "required": True},
            {
                "name": "params",
                "description": "Optional query parameters",
                "type": "object",
                "required": False,
            },
        ],
        category=ToolCategory.NETWORK,
        network_required=True,
    )
    def http_get(self, url: str, params: dict[str, Any] | None = None) -> str:
        return self.http_request(url=url, method="GET", params=params)

    @tool_info(
        "http_action",
        "Send an approved POST request to a public HTTPS API. This can create an external side effect.",
        [
            {"name": "url", "description": "Public HTTPS URL", "type": "string", "required": True},
            {
                "name": "params",
                "description": "Optional query parameters",
                "type": "object",
                "required": False,
            },
            {
                "name": "json_body",
                "description": "Optional JSON request body",
                "type": "object",
                "required": False,
            },
        ],
        category=ToolCategory.NETWORK,
        is_read_only=False,
        is_write=True,
        external_side_effect=True,
        network_required=True,
        approval_policy=ApprovalPolicy.ALWAYS,
    )
    def http_action(
        self,
        url: str,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> str:
        return self.http_request(url=url, method="POST", params=params, json_body=json_body)

    @tool_info(
        "http_request",
        "Compatibility HTTP client. GET is read-only; POST is an external action and requires approval. "
        "Credentials supplied by the user are attached automatically only to their bound host; "
        "never include secrets in arguments. Private networks, redirects, and unsafe schemes are blocked.",
        [
            {"name": "url", "description": "Public HTTPS URL", "type": "string", "required": True},
            {"name": "method", "description": "GET or POST", "type": "string", "required": False},
            {
                "name": "params",
                "description": "Optional query parameters",
                "type": "object",
                "required": False,
            },
            {
                "name": "json_body",
                "description": "Optional JSON request body",
                "type": "object",
                "required": False,
            },
        ],
        category=ToolCategory.NETWORK,
        network_required=True,
        approval_policy=ApprovalPolicy.CONDITIONAL,
        approval_check=lambda arguments: str(arguments.get("method") or "GET").upper() != "GET",
    )
    def http_request(
        self,
        url: str,
        method: str = "GET",
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        _raw_html: bool = False,
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
            limiter = _host_limiter(parsed.hostname or "", self.config.max_concurrent_per_host)
            with limiter:
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
        if not _raw_html and "html" in str(result.get("content_type") or "").lower():
            result = self._simplify_html_result(result)
        return self._result(result)

    def _news_search_fallback(
        self,
        query: str,
        limit: int,
        first_result: dict[str, Any],
    ) -> str:
        """Use a keyless RSS fallback when DuckDuckGo presents its bot challenge."""
        fallback = json.loads(
            self.http_request(
                "https://news.google.com/rss/search",
                method="GET",
                params={"q": query, "hl": "en-US", "gl": "US", "ceid": "US:en"},
                _raw_html=True,
            )
        )
        if fallback.get("ok"):
            try:
                root = ET.fromstring(str(fallback.get("body") or ""))
            except ET.ParseError:
                root = None
            if root is not None:
                results: list[dict[str, str]] = []
                for item in root.findall("./channel/item")[:limit]:
                    source = item.find("source")
                    results.append(
                        {
                            "title": str(item.findtext("title") or "").strip(),
                            "url": str(item.findtext("link") or "").strip(),
                            "snippet": self._plain_html(str(item.findtext("description") or "")),
                            "published": str(item.findtext("pubDate") or "").strip(),
                            "source": str(source.text or "").strip() if source is not None else "",
                            "source_url": str(source.get("url") or "").strip()
                            if source is not None
                            else "",
                        }
                    )
                if results:
                    return self._result(
                        {
                            "ok": True,
                            "query": query,
                            "provider": "google_news_rss_fallback",
                            "results": results,
                            "notice": (
                                "DuckDuckGo required a human challenge; results are a news RSS fallback."
                            ),
                        }
                    )
        return self._result(
            {
                "ok": False,
                "query": query,
                "error": "public search providers returned no usable results",
                "duckduckgo_status": first_result.get("status"),
                "fallback_status": fallback.get("status"),
            }
        )

    def _simplify_html_result(self, result: dict[str, Any]) -> dict[str, Any]:
        """Keep useful page evidence while preventing raw markup from exhausting context."""
        body = str(result.get("body") or "")
        title_match = re.search(r"<title\b[^>]*>(.*?)</title>", body, re.IGNORECASE | re.DOTALL)
        description_match = re.search(
            r'<meta\b(?=[^>]*\bname=["\']description["\'])(?=[^>]*\bcontent=["\']([^"\']*)["\'])[^>]*>',
            body,
            re.IGNORECASE | re.DOTALL,
        )
        links: list[str] = []
        base_url = str(result.get("url") or "")
        for raw_link in re.findall(r'<a\b[^>]*\bhref=["\']([^"\']+)["\']', body, re.IGNORECASE):
            link = urllib.parse.urljoin(base_url, html.unescape(raw_link.strip()))
            if link.startswith(("https://", "http://")) and link not in links:
                links.append(link)
            if len(links) >= 30:
                break
        visible = re.sub(
            r"<(script|style|noscript|svg|template)\b.*?</\1>",
            " ",
            body,
            flags=re.IGNORECASE | re.DOTALL,
        )
        visible = re.sub(r"<!--.*?-->", " ", visible, flags=re.DOTALL)
        visible = self._plain_html(visible)
        sections = []
        if title_match:
            sections.append("Title: " + self._plain_html(title_match.group(1)))
        if description_match:
            sections.append("Description: " + self._plain_html(description_match.group(1)))
        sections.append("Visible text: " + visible[:24_000])
        if links:
            sections.append("Links:\n" + "\n".join(f"- {link}" for link in links))
        simplified = dict(result)
        simplified["body"] = "\n".join(sections)
        simplified["html_simplified"] = True
        simplified["raw_body_artifact"] = f"logs/http-{self.request_count}.log"
        return simplified

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

    @staticmethod
    def _plain_html(value: str) -> str:
        return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", value)).split())


def _host_limiter(host: str, limit: int) -> threading.BoundedSemaphore:
    key = (host.lower(), limit)
    with _HOST_LIMITERS_LOCK:
        limiter = _HOST_LIMITERS.get(key)
        if limiter is None:
            limiter = threading.BoundedSemaphore(limit)
            _HOST_LIMITERS[key] = limiter
        return limiter
