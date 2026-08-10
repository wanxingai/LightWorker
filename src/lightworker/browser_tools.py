"""Ephemeral browser automation with Playwright default and optional DrissionPage backend."""

from __future__ import annotations

import ipaddress
import json
import re
import socket
import tempfile
import threading
import urllib.parse
from abc import ABC, abstractmethod
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from .config import BrowserConfig
from .models import ApprovalPolicy, ToolCategory
from .storage import RunStore
from .tool_protocol import tool_info

SENSITIVE_ACTION = re.compile(
    r"(?:submit|send|buy|purchase|pay|delete|remove|confirm|publish|post|login|sign.?in|授权|提交|发送|购买|支付|删除|确认|发布|登录)",
    re.IGNORECASE,
)


def _sensitive_selector(arguments: dict[str, Any]) -> bool:
    return bool(SENSITIVE_ACTION.search(str(arguments.get("selector") or "")))


class BrowserBackend(ABC):
    @abstractmethod
    def open(self, url: str) -> dict[str, Any]: ...

    @abstractmethod
    def click(self, selector: str) -> dict[str, Any]: ...

    @abstractmethod
    def type_text(self, selector: str, text: str, clear: bool) -> dict[str, Any]: ...

    @abstractmethod
    def select(self, selector: str, value: str) -> dict[str, Any]: ...

    @abstractmethod
    def extract(self, selector: str, limit: int) -> dict[str, Any]: ...

    @abstractmethod
    def screenshot(self, path: Path, full_page: bool) -> dict[str, Any]: ...

    @abstractmethod
    def tabs(self) -> dict[str, Any]: ...

    @abstractmethod
    def close(self) -> None: ...


class PlaywrightBackend(BrowserBackend):
    def __init__(self, config: BrowserConfig):
        if config.persistent_profiles:
            raise RuntimeError("persistent browser profiles require a later explicit approval workflow")
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise RuntimeError(
                "Playwright backend is not installed; reinstall LightWorker dependencies and run "
                "'playwright install chromium'"
            ) from exc
        self.config = config
        self._profile = tempfile.TemporaryDirectory(prefix="lightworker-browser-")
        self._playwright = sync_playwright().start()
        self.context = self._playwright.chromium.launch_persistent_context(
            self._profile.name,
            headless=config.headless,
            accept_downloads=config.allow_downloads,
        )
        self.context.set_default_timeout(config.timeout_seconds * 1000)
        self.context.route("**/*", self._route)
        self.page = self.context.pages[0] if self.context.pages else self.context.new_page()

    def _route(self, route: Any) -> None:
        url = str(route.request.url)
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme in {"data", "blob", "about"}:
            route.continue_()
            return
        try:
            validate_public_url(url, self.config.allowed_hosts)
        except ValueError:
            route.abort("blockedbyclient")
            return
        route.continue_()

    def open(self, url: str) -> dict[str, Any]:
        target = validate_public_url(url, self.config.allowed_hosts)
        response = self.page.goto(target, wait_until="domcontentloaded")
        final_url = validate_public_url(self.page.url, self.config.allowed_hosts)
        return {
            "ok": True,
            "url": final_url,
            "title": self.page.title(),
            "status": response.status if response else None,
        }

    def click(self, selector: str) -> dict[str, Any]:
        self.page.locator(selector).first.click()
        return {"ok": True, "url": self.page.url, "title": self.page.title()}

    def type_text(self, selector: str, text: str, clear: bool) -> dict[str, Any]:
        locator = self.page.locator(selector).first
        if clear:
            locator.fill(text)
        else:
            locator.type(text)
        return {"ok": True, "selector": selector, "characters": len(text)}

    def select(self, selector: str, value: str) -> dict[str, Any]:
        selected = self.page.locator(selector).first.select_option(value)
        return {"ok": True, "selected": selected}

    def extract(self, selector: str, limit: int) -> dict[str, Any]:
        locator = self.page.locator(selector)
        count = min(locator.count(), limit)
        values = []
        for index in range(count):
            item = locator.nth(index)
            values.append(
                {
                    "text": item.inner_text()[:8000],
                    "tag": item.evaluate("element => element.tagName.toLowerCase()"),
                }
            )
        return {"ok": True, "selector": selector, "count": count, "items": values}

    def screenshot(self, path: Path, full_page: bool) -> dict[str, Any]:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.page.screenshot(path=str(path), full_page=full_page)
        return {"ok": True, "path": str(path), "url": self.page.url}

    def tabs(self) -> dict[str, Any]:
        return {
            "ok": True,
            "tabs": [
                {"index": index, "url": page.url, "title": page.title()}
                for index, page in enumerate(self.context.pages)
            ],
        }

    def close(self) -> None:
        try:
            self.context.close()
        finally:
            self._playwright.stop()
            self._profile.cleanup()


class DrissionPageBackend(BrowserBackend):
    """Optional backend; commercial users must obtain any required DrissionPage license."""

    def __init__(self, config: BrowserConfig):
        if config.persistent_profiles:
            raise RuntimeError("persistent browser profiles require a later explicit approval workflow")
        try:
            from DrissionPage import Chromium, ChromiumOptions
        except ImportError as exc:
            raise RuntimeError(
                "DrissionPage backend is not installed; install the 'drissionpage' extra"
            ) from exc
        self.config = config
        self._profile = tempfile.TemporaryDirectory(prefix="lightworker-drission-")
        options = ChromiumOptions()
        options.set_user_data_path(self._profile.name)
        options.set_local_port(_free_port())
        options.headless(config.headless)
        self.browser = Chromium(options)
        self.page = self.browser.latest_tab
        if hasattr(self.page, "set") and hasattr(self.page.set, "timeouts"):
            self.page.set.timeouts(base=config.timeout_seconds)

    def open(self, url: str) -> dict[str, Any]:
        target = validate_public_url(url, self.config.allowed_hosts)
        self.page.get(target)
        final_url = validate_public_url(str(self.page.url), self.config.allowed_hosts)
        return {"ok": True, "url": final_url, "title": str(self.page.title)}

    def click(self, selector: str) -> dict[str, Any]:
        self.page.ele(selector).click()
        return {"ok": True, "url": str(self.page.url), "title": str(self.page.title)}

    def type_text(self, selector: str, text: str, clear: bool) -> dict[str, Any]:
        element = self.page.ele(selector)
        if clear:
            element.clear()
        element.input(text)
        return {"ok": True, "selector": selector, "characters": len(text)}

    def select(self, selector: str, value: str) -> dict[str, Any]:
        element = self.page.ele(selector)
        element.select.by_value(value)
        return {"ok": True, "selected": value}

    def extract(self, selector: str, limit: int) -> dict[str, Any]:
        items = self.page.eles(selector)[:limit]
        return {
            "ok": True,
            "selector": selector,
            "count": len(items),
            "items": [{"text": str(item.text)[:8000], "tag": str(item.tag)} for item in items],
        }

    def screenshot(self, path: Path, full_page: bool) -> dict[str, Any]:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.page.get_screenshot(path=str(path.parent), name=path.name, full_page=full_page)
        return {"ok": True, "path": str(path), "url": str(self.page.url)}

    def tabs(self) -> dict[str, Any]:
        tabs = list(self.browser.get_tabs())
        return {
            "ok": True,
            "tabs": [
                {"index": index, "url": str(page.url), "title": str(page.title)}
                for index, page in enumerate(tabs)
            ],
        }

    def close(self) -> None:
        try:
            self.browser.quit()
        finally:
            self._profile.cleanup()


class BrowserTools:
    def __init__(
        self,
        *,
        config: BrowserConfig,
        store: RunStore,
        run_id: str,
        resource_semaphore: threading.Semaphore | None = None,
    ):
        self.config = config
        self.store = store
        self.run_id = run_id
        self.backend: BrowserBackend | None = None
        self.resource_semaphore = resource_semaphore
        self._slot_acquired = False
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="lightworker-browser")
        self._state_lock = threading.RLock()
        self._closed = False
        self.screenshot_count = 0
        self.tools = [
            self.browser_open,
            self.browser_click,
            self.browser_type,
            self.browser_select,
            self.browser_extract,
            self.browser_tabs,
            self.browser_screenshot,
        ]

    def _backend(self) -> BrowserBackend:
        if self.backend is None:
            backend = DrissionPageBackend if self.config.backend == "drissionpage" else PlaywrightBackend
            if self.resource_semaphore is not None:
                self.resource_semaphore.acquire()
                self._slot_acquired = True
            try:
                self.backend = backend(self.config)
            except Exception:
                if self._slot_acquired and self.resource_semaphore is not None:
                    self.resource_semaphore.release()
                    self._slot_acquired = False
                raise
        return self.backend

    def _close_backend(self) -> None:
        if self.backend is not None:
            try:
                self.backend.close()
            finally:
                self.backend = None
                if self._slot_acquired and self.resource_semaphore is not None:
                    self.resource_semaphore.release()
                    self._slot_acquired = False

    def close(self) -> None:
        with self._state_lock:
            if self._closed:
                return
            self._closed = True
        try:
            self._executor.submit(self._close_backend).result()
        finally:
            self._executor.shutdown(wait=True, cancel_futures=True)

    def _call_backend(self, operation: str, *args: Any) -> dict[str, Any]:
        function = getattr(self._backend(), operation)
        return function(*args)

    def _result(self, operation: str, *args: Any) -> str:
        try:
            with self._state_lock:
                if self._closed:
                    raise RuntimeError("browser session is closed")
                future = self._executor.submit(self._call_backend, operation, *args)
            return json.dumps(future.result(), ensure_ascii=False)
        except Exception as exc:
            return json.dumps(
                {"ok": False, "error": str(exc), "error_type": type(exc).__name__},
                ensure_ascii=False,
            )

    @tool_info(
        "browser_open",
        "Open a public HTTP(S) page in an ephemeral browser profile. Private addresses are blocked.",
        [{"name": "url", "description": "Public HTTP(S) URL", "type": "string", "required": True}],
        category=ToolCategory.BROWSER,
        network_required=True,
        timeout_seconds=180,
    )
    def browser_open(self, url: str) -> str:
        return self._result("open", url)

    @tool_info(
        "browser_click",
        "Click the first matching element. Sensitive submit-like selectors require approval.",
        [
            {
                "name": "selector",
                "description": "Playwright-compatible selector",
                "type": "string",
                "required": True,
            }
        ],
        category=ToolCategory.BROWSER,
        is_read_only=False,
        external_side_effect=True,
        network_required=True,
        approval_policy=ApprovalPolicy.CONDITIONAL,
        approval_check=_sensitive_selector,
    )
    def browser_click(self, selector: str) -> str:
        return self._result("click", selector)

    @tool_info(
        "browser_type",
        "Type non-secret text into the first matching element. This does not press Enter or submit.",
        [
            {"name": "selector", "description": "Element selector", "type": "string", "required": True},
            {"name": "text", "description": "Non-secret text", "type": "string", "required": True},
            {
                "name": "clear",
                "description": "Clear existing value first",
                "type": "boolean",
                "required": False,
            },
        ],
        category=ToolCategory.BROWSER,
        is_read_only=False,
        external_side_effect=False,
        network_required=True,
    )
    def browser_type(self, selector: str, text: str, clear: bool = True) -> str:
        return self._result("type_text", selector, text, clear)

    @tool_info(
        "browser_select",
        "Select an option value without submitting the form.",
        [
            {
                "name": "selector",
                "description": "Select element selector",
                "type": "string",
                "required": True,
            },
            {"name": "value", "description": "Option value", "type": "string", "required": True},
        ],
        category=ToolCategory.BROWSER,
        is_read_only=False,
        network_required=True,
    )
    def browser_select(self, selector: str, value: str) -> str:
        return self._result("select", selector, value)

    @tool_info(
        "browser_extract",
        "Extract visible text from matching elements in the current page.",
        [
            {"name": "selector", "description": "Element selector", "type": "string", "required": True},
            {"name": "limit", "description": "Maximum elements", "type": "integer", "required": False},
        ],
        category=ToolCategory.BROWSER,
    )
    def browser_extract(self, selector: str = "body", limit: int = 20) -> str:
        return self._result("extract", selector, max(1, min(limit, 100)))

    @tool_info("browser_tabs", "List open ephemeral browser tabs.", [], category=ToolCategory.BROWSER)
    def browser_tabs(self) -> str:
        return self._result("tabs")

    @tool_info(
        "browser_screenshot",
        "Capture the current page into the run's browser artifacts.",
        [{"name": "full_page", "description": "Capture the full page", "type": "boolean", "required": False}],
        category=ToolCategory.BROWSER,
        is_read_only=False,
        is_write=True,
    )
    def browser_screenshot(self, full_page: bool = True) -> str:
        with self._state_lock:
            self.screenshot_count += 1
            relative = f"browser/screenshot-{self.screenshot_count}.png"
        path = self.store.artifact_path(self.run_id, relative)
        result = self._result("screenshot", path, full_page)
        try:
            payload = json.loads(result)
        except json.JSONDecodeError:
            return result
        if payload.get("ok"):
            payload["artifact"] = relative
            payload["path"] = relative
        return json.dumps(payload, ensure_ascii=False)


def validate_public_url(url: str, allowed_hosts: list[str] | None = None) -> str:
    if len(url) > 4096:
        raise ValueError("URL is too long")
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise ValueError("only public HTTP(S) URLs are allowed")
    if parsed.username or parsed.password:
        raise ValueError("URL credentials are forbidden")
    host = parsed.hostname.lower()
    if allowed_hosts and host not in {item.lower() for item in allowed_hosts}:
        raise ValueError(f"browser host is not allowed: {host}")
    port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
    for info in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM):
        address = ipaddress.ip_address(info[4][0])
        if not address.is_global:
            raise ValueError("private or non-global browser targets are forbidden")
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path or "/", parsed.query, ""))


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])
