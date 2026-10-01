"""Minimal JSON-RPC-like helper executed inside the task container.

This module deliberately uses only the Python standard library. The host
orchestrator sends one JSON object on stdin and receives one JSON object on
stdout. No shell is involved.
"""

from __future__ import annotations

import ast
import fnmatch
import json
import os
import re
import select
import shlex
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path, PurePosixPath
from typing import Any

WORKSPACE = Path("/workspace")
REQUIREMENT_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._-]*(?:\[[A-Za-z0-9_,.-]+\])?(?:\s*(?:===|==|~=|!=|<=|>=|<|>)\s*[A-Za-z0-9*+!._-]+(?:\s*,\s*(?:===|==|~=|!=|<=|>=|<|>)\s*[A-Za-z0-9*+!._-]+)*)?$"
)


class HelperError(RuntimeError):
    pass


def main() -> int:
    try:
        request = json.load(sys.stdin)
        if not isinstance(request, dict):
            raise HelperError("request must be an object")
        action = str(request.get("action") or "")
        params = request.get("params") or {}
        policy = request.get("policy") or {}
        if not isinstance(params, dict) or not isinstance(policy, dict):
            raise HelperError("params and policy must be objects")
        handler = ACTIONS.get(action)
        if handler is None:
            raise HelperError(f"unsupported action: {action}")
        response = handler(params, policy)
        print(json.dumps({"ok": True, **response}, ensure_ascii=False))
        return 0
    except Exception as exc:
        print(
            json.dumps({"ok": False, "error": str(exc), "error_type": type(exc).__name__}, ensure_ascii=False)
        )
        return 1


def health(params: dict[str, Any], policy: dict[str, Any]) -> dict[str, Any]:
    return {
        "workspace": str(WORKSPACE),
        "uid": os.getuid(),
        "protocol_version": 2,
        "capabilities": sorted(ACTIONS),
    }


def ptc_eval(params: dict[str, Any], policy: dict[str, Any]) -> dict[str, Any]:
    """Called only by the Docker helper. A suspended call is bridged by the host policy layer."""
    source = require_string(params, "source")
    if len(source) > 32768:
        raise HelperError("code program exceeds size limit")
    tree = ast.parse(source)
    allowed_nodes = (
        ast.Module,
        ast.Assign,
        ast.Expr,
        ast.For,
        ast.If,
        ast.Name,
        ast.Load,
        ast.Store,
        ast.Constant,
        ast.List,
        ast.Tuple,
        ast.Dict,
        ast.Subscript,
        ast.Slice,
        ast.Call,
        ast.keyword,
        ast.BinOp,
        ast.UnaryOp,
        ast.BoolOp,
        ast.Compare,
        ast.Add,
        ast.Sub,
        ast.Mult,
        ast.Div,
        ast.FloorDiv,
        ast.Mod,
        ast.USub,
        ast.Not,
        ast.And,
        ast.Or,
        ast.Eq,
        ast.NotEq,
        ast.Lt,
        ast.Gt,
        ast.LtE,
        ast.GtE,
        ast.In,
        ast.NotIn,
        ast.IfExp,
    )
    functions = {"call", "len", "range", "str", "int", "float", "sum", "min", "max", "sorted"}
    for node in ast.walk(tree):
        if not isinstance(node, allowed_nodes):
            raise HelperError(f"unsupported code construct: {type(node).__name__}")
        if isinstance(node, ast.Name) and node.id.startswith("_"):
            raise HelperError("private identifiers are blocked")
        if isinstance(node, ast.Call) and (
            not isinstance(node.func, ast.Name) or node.func.id not in functions
        ):
            raise HelperError("only the documented code-mode functions can be called")
    history = params.get("results", [])
    cursor = 0

    class PendingCall(Exception):
        def __init__(self, request):
            self.request = request

    def call(name, arguments):
        nonlocal cursor
        if not isinstance(name, str) or not isinstance(arguments, dict):
            raise HelperError("call requires a tool name and argument object")
        if cursor >= len(history):
            raise PendingCall({"name": name, "arguments": arguments})
        previous = history[cursor]
        cursor += 1
        if previous["name"] != name or previous["arguments"] != arguments:
            raise HelperError("code replay changed an already executed call")
        return previous["value"]

    def bounded_range(*args):
        value = range(*args)
        if len(value) > 1000:
            raise HelperError("code loop exceeds iteration bound")
        return value

    namespace = {
        "__builtins__": {},
        "call": call,
        "range": bounded_range,
        "len": len,
        "str": str,
        "int": int,
        "float": float,
        "sum": sum,
        "min": min,
        "max": max,
        "sorted": sorted,
    }
    try:
        exec(compile(tree, "<docker-code-mode>", "exec"), namespace)
    except PendingCall as pending:
        return {"tool_request": pending.request}
    return {"result": namespace.get("result")}


def _terminal_dir(params):
    identity = require_string(params, "terminal_id")
    if not re.fullmatch(r"[a-f0-9]{32}", identity):
        raise HelperError("invalid terminal identifier")
    directory = Path("/tmp/lightworker-terminals") / identity
    return directory


def terminal_start(params, policy):
    argv = params.get("argv")
    if not isinstance(argv, list) or not argv or not all(isinstance(x, str) for x in argv):
        raise HelperError("argv must be a non-empty string array")
    validate_shell_command(argv, policy)
    directory = _terminal_dir(params)
    directory.mkdir(parents=True, exist_ok=False)
    fifo = directory / "input"
    os.mkfifo(fifo, 0o600)
    # The runner, process and FIFO live only inside the isolated container.
    runner = """import json, os, subprocess, sys
from pathlib import Path
directory = Path(sys.argv[1])
argv = json.loads(sys.argv[2])
fd = os.open(directory / 'input', os.O_RDWR)
with os.fdopen(fd, 'rb', buffering=0) as stream, (directory / 'output').open('ab', buffering=0) as output:
    process = subprocess.Popen(argv, stdin=stream, stdout=output, stderr=output, start_new_session=True)
    (directory / 'pid').write_text(str(process.pid))
    (directory / 'exit').write_text(str(process.wait()))
"""
    process = subprocess.Popen(
        [sys.executable, "-I", "-c", runner, str(directory), json.dumps(argv)],
        cwd=WORKSPACE,
        env=safe_environment(),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    (directory / "runner").write_text(str(process.pid))
    return {"terminal_id": params["terminal_id"], "running": True}


def terminal_poll(params, policy):
    directory = _terminal_dir(params)
    output_file = directory / "output"
    after = max(0, int(params.get("after", 0)))
    maximum = int(policy.get("max_output_bytes", 32768))
    output = b""
    if output_file.is_file():
        with output_file.open("rb") as stream:
            stream.seek(after)
            output = stream.read(maximum)
    finished = directory / "exit"
    running = not finished.exists()
    if running and (directory / "runner").is_file():
        try:
            os.kill(int((directory / "runner").read_text()), 0)
        except ProcessLookupError as exc:
            raise HelperError("terminal runner disappeared before publishing its exit status") from exc
    return {
        "running": running,
        "exit_code": int(finished.read_text()) if finished.exists() else None,
        "output": output.decode("utf-8", errors="replace"),
        "cursor": after + len(output),
        "drained": not output_file.exists() or after + len(output) >= output_file.stat().st_size,
    }


def terminal_send(params, policy):
    directory = _terminal_dir(params)
    text = require_string(params, "text")
    if len(text.encode()) > 16384:
        raise HelperError("terminal input exceeds limit")
    fd = os.open(directory / "input", os.O_WRONLY | os.O_NONBLOCK)
    try:
        os.write(fd, text.encode())
    finally:
        os.close(fd)
    return {"sent": len(text)}


def terminal_stop(params, policy):
    directory = _terminal_dir(params)
    for filename in ("pid", "runner"):
        path = directory / filename
        if path.is_file():
            try:
                os.killpg(int(path.read_text()), signal.SIGTERM)
            except ProcessLookupError:
                pass
    return {"stopped": True}


def lsp_request(params, policy):
    argv = params.get("argv")
    if not isinstance(argv, list) or not argv or not all(isinstance(x, str) for x in argv):
        raise HelperError("invalid language server argv")
    validate_shell_command(argv, policy)
    method = require_string(params, "method")
    allowed = {
        "textDocument/definition",
        "textDocument/references",
        "textDocument/documentSymbol",
        "workspace/symbol",
        "textDocument/hover",
        "textDocument/completion",
    }
    if method not in allowed:
        raise HelperError("LSP method is not read-only or is unsupported")
    process = subprocess.Popen(
        argv,
        cwd=WORKSPACE,
        env=safe_environment(),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    deadline = time.monotonic() + 25

    def send(payload):
        body = json.dumps(payload).encode()
        process.stdin.write(f"Content-Length: {len(body)}\r\n\r\n".encode() + body)
        process.stdin.flush()

    def receive(request_id):
        buffer = b""
        while time.monotonic() < deadline:
            ready, _, _ = select.select([process.stdout], [], [], max(0, deadline - time.monotonic()))
            if not ready:
                break
            chunk = os.read(process.stdout.fileno(), 4096)
            if not chunk:
                break
            buffer += chunk
            if len(buffer) > 1048576:
                raise HelperError("language server response exceeds limit")
            while b"\r\n\r\n" in buffer:
                header, body = buffer.split(b"\r\n\r\n", 1)
                match = re.search(rb"Content-Length:\s*(\d+)", header, re.I)
                if not match:
                    raise HelperError("invalid LSP response framing")
                length = int(match.group(1))
                if len(body) < length:
                    break
                value = json.loads(body[:length])
                buffer = body[length:]
                if value.get("id") == request_id:
                    return value
        raise HelperError("language server request timed out")

    try:
        send(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"processId": None, "rootUri": WORKSPACE.as_uri(), "capabilities": {}},
            }
        )
        receive(1)
        send({"jsonrpc": "2.0", "method": "initialized", "params": {}})
        send({"jsonrpc": "2.0", "id": 2, "method": method, "params": params.get("params", {})})
        return {"response": receive(2)}
    finally:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)


def security_probe(params: dict[str, Any], policy: dict[str, Any]) -> dict[str, Any]:
    root_write_allowed = False
    probe_path = Path("/lightworker-root-write-probe")
    try:
        probe_path.write_text("unsafe", encoding="utf-8")
        root_write_allowed = True
        probe_path.unlink(missing_ok=True)
    except OSError:
        pass

    network_available = False
    try:
        with socket.create_connection(("pypi.org", 443), timeout=2):
            network_available = True
    except OSError:
        pass
    return {
        "uid": os.getuid(),
        "root_write_allowed": root_write_allowed,
        "docker_socket_exists": Path("/var/run/docker.sock").exists(),
        "network_available": network_available,
    }


def list_files(params: dict[str, Any], policy: dict[str, Any]) -> dict[str, Any]:
    relative = str(params.get("path") or ".")
    limit = min(int(params.get("limit") or 500), 2000)
    root = safe_path(relative, must_exist=True)
    if not root.is_dir():
        raise HelperError("list_files path must be a directory")
    values: list[str] = []
    sensitive = [str(item) for item in policy.get("sensitive_read_patterns") or []]
    for base, directories, files in os.walk(root, followlinks=False):
        directories[:] = sorted(
            item
            for item in directories
            if item != ".git"
            and not is_protected((Path(base) / item).relative_to(WORKSPACE).as_posix(), sensitive)
        )
        for name in sorted(files):
            path = Path(base) / name
            relative_path = path.relative_to(WORKSPACE).as_posix()
            if is_protected(relative_path, sensitive):
                continue
            values.append(relative_path)
            if len(values) >= limit:
                return {"files": values, "truncated": True}
    return {"files": values, "truncated": False}


def read_file(params: dict[str, Any], policy: dict[str, Any]) -> dict[str, Any]:
    relative = require_string(params, "path")
    sensitive = [str(item) for item in policy.get("sensitive_read_patterns") or []]
    if is_protected(relative, sensitive):
        raise HelperError(f"sensitive file cannot be read: {relative}")
    path = safe_path(relative, must_exist=True)
    if not path.is_file():
        raise HelperError("read_file path must be a regular file")
    maximum = int(policy.get("max_read_bytes") or 524_288)
    size = path.stat().st_size
    if size > maximum:
        raise HelperError(f"file is too large ({size} bytes; limit {maximum})")
    payload = path.read_bytes()
    if b"\0" in payload:
        raise HelperError("binary files cannot be read")
    text = payload.decode("utf-8", errors="replace")
    start_line = max(int(params.get("start_line") or 1), 1)
    end_line = int(params.get("end_line") or 0)
    lines = text.splitlines()
    selected = lines[start_line - 1 : end_line if end_line > 0 else None]
    numbered = "\n".join(f"{index}: {line}" for index, line in enumerate(selected, start=start_line))
    return {"path": relative, "content": cap(numbered, policy), "size": size, "line_count": len(lines)}


def search_text(params: dict[str, Any], policy: dict[str, Any]) -> dict[str, Any]:
    pattern = require_string(params, "pattern")
    relative = str(params.get("path") or ".")
    safe_path(relative, must_exist=True)
    sensitive = [str(item) for item in policy.get("sensitive_read_patterns") or []]
    if is_protected(relative, sensitive):
        raise HelperError(f"sensitive path cannot be searched: {relative}")
    command = ["rg", "--line-number", "--no-heading", "--color", "never", "--glob", "!.git/**"]
    for excluded in sensitive:
        command.extend(["--glob", f"!{excluded}"])
    if bool(params.get("fixed_strings", True)):
        command.append("--fixed-strings")
    if not bool(params.get("case_sensitive", False)):
        command.append("--ignore-case")
    command.extend(["--", pattern, relative])
    result = subprocess.run(
        command,
        cwd=WORKSPACE,
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )
    if result.returncode not in {0, 1}:
        raise HelperError(result.stderr.strip() or "ripgrep failed")
    return {"matches": cap(result.stdout, policy), "found": result.returncode == 0}


def apply_patch(params: dict[str, Any], policy: dict[str, Any]) -> dict[str, Any]:
    patch = require_string(params, "patch")
    # Model-generated unified diffs frequently omit the final line ending.
    # Git treats an otherwise valid patch without it as corrupt, so normalize
    # this harmless transport detail before size and safety validation.
    if not patch.endswith("\n"):
        patch += "\n"
    encoded = patch.encode("utf-8")
    maximum = int(policy.get("max_patch_bytes") or 1_048_576)
    if len(encoded) > maximum:
        raise HelperError(f"patch exceeds {maximum} byte limit")
    paths, deletes = extract_patch_paths(patch)
    if deletes and not bool(params.get("allow_delete", False)):
        raise HelperError("file deletion is blocked in non-interactive mode")
    if not paths:
        raise HelperError("patch does not contain any file paths")
    max_files = int(policy.get("max_changed_files") or 50)
    aggregate_paths = current_changed_paths() | paths
    if len(aggregate_paths) > max_files:
        raise HelperError(f"patch would leave {len(aggregate_paths)} changed files; limit is {max_files}")
    protected = [str(item) for item in policy.get("protected_patterns") or []]
    for relative in paths:
        validate_relative_path(relative)
        if is_protected(relative, protected):
            raise HelperError(f"protected path cannot be modified: {relative}")
        candidate = WORKSPACE / relative
        if candidate.exists():
            safe_path(relative, must_exist=True)
        else:
            safe_path(str(PurePosixPath(relative).parent), must_exist=True)

    checked = run_git_apply(patch, check=True)
    if checked.returncode:
        raise HelperError(checked.stderr.strip() or "git apply --check failed")
    applied = run_git_apply(patch, check=False)
    if applied.returncode:
        raise HelperError(applied.stderr.strip() or "git apply failed")
    return {"changed_files": sorted(paths), "status": git_status({}, policy)["status"]}


def git_status(params: dict[str, Any], policy: dict[str, Any]) -> dict[str, Any]:
    result = run_command_raw(["git", "-c", "safe.directory=/workspace", "status", "--short"], timeout=30)
    if result.returncode:
        raise HelperError(result.stderr.strip() or "git status failed")
    return {"status": cap(result.stdout, policy)}


def current_changed_paths() -> set[str]:
    result = run_command_raw(
        ["git", "-c", "safe.directory=/workspace", "status", "--porcelain=v1", "-z"],
        timeout=30,
    )
    if result.returncode:
        raise HelperError(result.stderr.strip() or "git status failed")
    paths: set[str] = set()
    for entry in result.stdout.split("\0"):
        if len(entry) >= 4 and entry[2] == " ":
            paths.add(entry[3:])
    return paths


def git_diff(params: dict[str, Any], policy: dict[str, Any]) -> dict[str, Any]:
    _mark_untracked_for_diff(policy)
    result = run_command_raw(
        ["git", "-c", "safe.directory=/workspace", "diff", "--binary", "--no-ext-diff", "--"],
        timeout=60,
    )
    if result.returncode:
        raise HelperError(result.stderr.strip() or "git diff failed")
    return {"diff": cap(result.stdout, policy, allow_full=bool(params.get("full", False)))}


def _mark_untracked_for_diff(policy: dict[str, Any]) -> None:
    """Use intent-to-add so Git renders safe untracked files in a normal diff."""
    listed = run_command_raw(
        ["git", "-c", "safe.directory=/workspace", "ls-files", "--others", "--exclude-standard", "-z"],
        timeout=30,
    )
    if listed.returncode:
        raise HelperError(listed.stderr.strip() or "failed to list untracked files")
    paths = [path for path in listed.stdout.split("\0") if path]
    if not paths:
        return
    maximum = int(policy.get("max_changed_files") or 50)
    if len(current_changed_paths()) > maximum:
        raise HelperError(f"workspace has more than {maximum} changed files")
    protected = [str(item) for item in policy.get("protected_patterns") or []]
    for relative in paths:
        validate_relative_path(relative)
        if is_protected(relative, protected):
            raise HelperError(f"protected path cannot be included in diff: {relative}")
        safe_path(relative, must_exist=True)
    added = run_command_raw(
        ["git", "-c", "safe.directory=/workspace", "add", "--intent-to-add", "--", *paths],
        timeout=30,
    )
    if added.returncode:
        raise HelperError(added.stderr.strip() or "failed to prepare untracked files for diff")


def run_command(params: dict[str, Any], policy: dict[str, Any]) -> dict[str, Any]:
    raw = params.get("argv")
    if not isinstance(raw, list) or not raw or any(not isinstance(item, str) or not item for item in raw):
        raise HelperError("argv must be a non-empty list of strings")
    argv = list(raw)
    validate_command(argv)
    timeout = min(int(params.get("timeout") or 900), 3600)
    started = time.perf_counter()
    try:
        result = run_command_raw(argv, timeout=timeout)
        timed_out = False
    except subprocess.TimeoutExpired as exc:
        result = subprocess.CompletedProcess(argv, 124, exc.stdout or "", exc.stderr or "")
        timed_out = True
    duration_ms = round((time.perf_counter() - started) * 1000, 3)
    combined = (result.stdout or "") + (("\n" + result.stderr) if result.stderr else "")
    return {
        "argv": argv,
        "exit_code": result.returncode,
        "timed_out": timed_out,
        "duration_ms": duration_ms,
        "output": cap(combined, policy),
        "full_output": combined,
    }


def shell_exec(params: dict[str, Any], policy: dict[str, Any]) -> dict[str, Any]:
    """Execute an approved argv vector in the container without invoking a shell."""
    raw = params.get("argv")
    if not isinstance(raw, list) or not raw or any(not isinstance(item, str) or not item for item in raw):
        raise HelperError("argv must be a non-empty list of strings")
    argv = list(raw)
    validate_shell_command(argv, policy)
    timeout = min(int(params.get("timeout") or 300), 3600)
    started = time.perf_counter()
    try:
        result = run_command_raw(argv, timeout=timeout)
        timed_out = False
    except subprocess.TimeoutExpired as exc:
        result = subprocess.CompletedProcess(argv, 124, exc.stdout or "", exc.stderr or "")
        timed_out = True
    combined = (result.stdout or "") + (("\n" + result.stderr) if result.stderr else "")
    return {
        "argv": argv,
        "exit_code": result.returncode,
        "timed_out": timed_out,
        "duration_ms": round((time.perf_counter() - started) * 1000, 3),
        "output": cap(combined, policy),
        "full_output": combined,
    }


def run_skill_script(params: dict[str, Any], policy: dict[str, Any]) -> dict[str, Any]:
    name = require_string(params, "name")
    content = require_string(params, "content")
    if len(content.encode("utf-8")) > 262_144:
        raise HelperError("skill script exceeds 262144 byte limit")
    if Path(name).name != name or Path(name).suffix.lower() not in {".py", ".sh"}:
        raise HelperError("skill script name must be a safe .py or .sh basename")
    raw_args = params.get("args") or []
    if not isinstance(raw_args, list) or len(raw_args) > 64:
        raise HelperError("skill script args must be a list with at most 64 items")
    args = [str(item) for item in raw_args]
    if any("\0" in item or len(item) > 4096 for item in args):
        raise HelperError("skill script contains an invalid argument")
    started = time.perf_counter()
    with tempfile.TemporaryDirectory(prefix="lightworker-skill-") as directory:
        path = Path(directory) / name
        path.write_text(content, encoding="utf-8")
        path.chmod(0o700)
        argv = ([sys.executable, str(path)] if path.suffix == ".py" else ["/bin/sh", str(path)]) + args
        try:
            result = run_command_raw(argv, timeout=min(int(params.get("timeout") or 300), 600))
            timed_out = False
        except subprocess.TimeoutExpired as exc:
            result = subprocess.CompletedProcess(argv, 124, exc.stdout or "", exc.stderr or "")
            timed_out = True
    combined = (result.stdout or "") + (("\n" + result.stderr) if result.stderr else "")
    return {
        "exit_code": result.returncode,
        "timed_out": timed_out,
        "duration_ms": round((time.perf_counter() - started) * 1000, 3),
        "output": cap(combined, policy),
        "full_output": combined,
    }


def pip_install(params: dict[str, Any], policy: dict[str, Any]) -> dict[str, Any]:
    requirements = validate_requirements(params, policy)
    timeout = min(int(params.get("timeout") or 900), 1800)
    index_url = str(policy.get("pip_index_url") or "https://pypi.org/simple")
    argv = [
        sys.executable,
        "-m",
        "pip",
        "install",
        "--user",
        "--no-input",
        "--disable-pip-version-check",
        "--index-url",
        index_url,
        *requirements,
    ]
    started = time.perf_counter()
    try:
        result = run_command_raw(argv, timeout=timeout)
        timed_out = False
    except subprocess.TimeoutExpired as exc:
        result = subprocess.CompletedProcess(argv, 124, exc.stdout or "", exc.stderr or "")
        timed_out = True
    freeze = run_command_raw([sys.executable, "-m", "pip", "freeze", "--user"], timeout=60)
    combined = (result.stdout or "") + (("\n" + result.stderr) if result.stderr else "")
    return {
        "requirements": requirements,
        "exit_code": result.returncode,
        "timed_out": timed_out,
        "duration_ms": round((time.perf_counter() - started) * 1000, 3),
        "output": cap(combined, policy),
        "full_output": combined,
        "frozen": sorted(line for line in freeze.stdout.splitlines() if line.strip()),
    }


def pip_check_requirements(params: dict[str, Any], policy: dict[str, Any]) -> dict[str, Any]:
    """Check whether requirements are already available without using an index or network."""
    requirements = validate_requirements(params, policy)
    argv = [
        sys.executable,
        "-m",
        "pip",
        "install",
        "--user",
        "--dry-run",
        "--no-index",
        "--no-input",
        "--disable-pip-version-check",
        *requirements,
    ]
    started = time.perf_counter()
    result = run_command_raw(argv, timeout=120)
    combined = (result.stdout or "") + (("\n" + result.stderr) if result.stderr else "")
    return {
        "requirements": requirements,
        "satisfied": result.returncode == 0,
        "exit_code": result.returncode,
        "timed_out": False,
        "duration_ms": round((time.perf_counter() - started) * 1000, 3),
        "output": cap(combined, policy),
        "full_output": combined,
    }


def validate_requirements(params: dict[str, Any], policy: dict[str, Any]) -> list[str]:
    raw = params.get("requirements")
    maximum = int(policy.get("max_pip_requirements") or 10)
    if not isinstance(raw, list) or not raw or len(raw) > maximum:
        raise HelperError(f"requirements must contain 1 to {maximum} entries")
    requirements = [str(item).strip() for item in raw]
    for requirement in requirements:
        if not REQUIREMENT_RE.fullmatch(requirement):
            raise HelperError(f"unsupported requirement syntax: {requirement}")
    return requirements


def cleanup_processes(params: dict[str, Any], policy: dict[str, Any]) -> dict[str, Any]:
    current = os.getpid()
    parent = os.getppid()
    killed: list[int] = []
    proc = Path("/proc")
    if not proc.exists():
        return {"killed": killed}
    for child in proc.iterdir():
        if not child.name.isdigit():
            continue
        pid = int(child.name)
        if pid in {1, current, parent}:
            continue
        try:
            command = (child / "cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", errors="replace")
            if command and "sleep infinity" not in command:
                os.kill(pid, signal.SIGTERM)
                killed.append(pid)
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue
    return {"killed": killed}


def validate_command(argv: list[str]) -> None:
    allowed = False
    if argv[0] == "pytest":
        allowed = True
    elif argv[:3] == ["python", "-m", "pytest"] or argv[:3] == ["python3", "-m", "pytest"]:
        allowed = True
    elif argv[:2] == ["ruff", "check"]:
        allowed = True
    elif argv[0] == "mypy":
        allowed = True
    elif argv[:3] == ["python", "-m", "build"] or argv[:3] == ["python3", "-m", "build"]:
        allowed = True
    if not allowed:
        raise HelperError(f"command is not allowlisted: {shlex.join(argv)}")
    forbidden_tokens = {"--config-file", "--config", "--command", "-c"}
    if any(token in forbidden_tokens for token in argv[1:]):
        raise HelperError("command contains a blocked option")


def validate_shell_command(argv: list[str], policy: dict[str, Any]) -> None:
    maximum = int(policy.get("max_shell_argv_items") or 128)
    if len(argv) > maximum or any("\0" in item or len(item) > 4096 for item in argv):
        raise HelperError("shell argv exceeds configured bounds")
    program = Path(argv[0]).name
    allowed = {str(item) for item in policy.get("shell_allowed_programs") or []}
    if program not in allowed:
        raise HelperError(f"program is not allowed in the Docker shell: {program}")
    if program in {"sh", "bash", "zsh", "fish", "sudo", "su", "docker", "podman"}:
        raise HelperError("interactive shells and host-control programs are blocked")
    if program == "git":
        if len(argv) < 2 or argv[1] not in {
            "status",
            "diff",
            "log",
            "show",
            "branch",
            "rev-parse",
            "ls-files",
            "grep",
            "blame",
            "describe",
            "check-ignore",
        }:
            raise HelperError("destructive or remote Git commands are blocked")
    if program in {"python", "python3"}:
        if any(item in {"-c", "-"} for item in argv[1:]):
            raise HelperError("inline Python and stdin programs are blocked; use a workspace script")
        if len(argv) >= 4 and argv[1:3] == ["-m", "pip"] and argv[3] in {"install", "uninstall"}:
            raise HelperError("use the audited pip_install tool for dependency changes")
    if program in {"pip", "pip3"} and len(argv) >= 2 and argv[1] in {"install", "uninstall"}:
        raise HelperError("use the audited pip_install tool for dependency changes")


def safe_path(relative: str, *, must_exist: bool) -> Path:
    validate_relative_path(relative)
    candidate = WORKSPACE / relative
    try:
        resolved = candidate.resolve(strict=must_exist)
    except FileNotFoundError as exc:
        raise HelperError(f"path does not exist: {relative}") from exc
    try:
        resolved.relative_to(WORKSPACE.resolve())
    except ValueError as exc:
        raise HelperError(f"path escapes workspace: {relative}") from exc
    return resolved


def validate_relative_path(value: str) -> None:
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or ".git" in path.parts:
        raise HelperError(f"unsafe workspace path: {value}")


def extract_patch_paths(patch: str) -> tuple[set[str], bool]:
    paths: set[str] = set()
    deletes = False
    for line in patch.splitlines():
        if line.startswith("diff --git "):
            try:
                parts = shlex.split(line)
            except ValueError as exc:
                raise HelperError("invalid diff header") from exc
            if len(parts) < 4 or not parts[2].startswith("a/") or not parts[3].startswith("b/"):
                raise HelperError("unsupported diff header")
            paths.add(parts[2][2:])
            paths.add(parts[3][2:])
        elif line == "+++ /dev/null" or line.startswith(("deleted file mode ", "rename from ", "rename to ")):
            deletes = True
        elif line.startswith(("new file mode ", "new mode ")):
            mode = line.rsplit(" ", 1)[-1]
            if mode not in {"100644", "100755"}:
                raise HelperError(f"special file mode is blocked: {mode}")
    return paths, deletes


def is_protected(relative: str, patterns: list[str]) -> bool:
    path = PurePosixPath(relative).as_posix()
    for pattern in patterns:
        if fnmatch.fnmatchcase(path, pattern) or fnmatch.fnmatchcase(path, pattern.removesuffix("/**")):
            return True
    return False


def run_git_apply(patch: str, *, check: bool) -> subprocess.CompletedProcess[str]:
    command = ["git", "-c", "safe.directory=/workspace", "apply", "--binary", "--whitespace=nowarn"]
    if check:
        command.append("--check")
    command.append("-")
    return subprocess.run(
        command,
        cwd=WORKSPACE,
        input=patch,
        text=True,
        capture_output=True,
        check=False,
        timeout=60,
    )


def run_command_raw(argv: list[str], *, timeout: int) -> subprocess.CompletedProcess[str]:
    process = subprocess.Popen(
        argv,
        cwd=WORKSPACE,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
        env=safe_environment(),
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        stdout, stderr = process.communicate()
        exc.stdout = stdout
        exc.stderr = stderr
        raise
    return subprocess.CompletedProcess(argv, process.returncode, stdout, stderr)


def safe_environment() -> dict[str, str]:
    names = {"HOME", "LANG", "LC_ALL"}
    values = {key: value for key, value in os.environ.items() if key in names}
    values.update(
        {
            "PATH": os.environ.get("PATH", "/deps/bin:/usr/local/bin:/usr/bin:/bin"),
            "PYTHONUSERBASE": os.environ.get("PYTHONUSERBASE", "/deps"),
            "TMPDIR": "/tmp",
            "PIP_CONFIG_FILE": os.devnull,
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
            "PIP_NO_INPUT": "1",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )
    sandbox_home = Path(values.get("HOME", "/home/worker"))
    sandbox_home.mkdir(parents=True, exist_ok=True)
    return values


def require_string(params: dict[str, Any], name: str) -> str:
    value = params.get(name)
    if not isinstance(value, str) or not value:
        raise HelperError(f"{name} must be a non-empty string")
    return value


def cap(value: str, policy: dict[str, Any], *, allow_full: bool = False) -> str:
    if allow_full:
        return value
    maximum = int(policy.get("max_output_bytes") or 32_768)
    encoded = value.encode("utf-8")
    if len(encoded) <= maximum:
        return value
    prefix = encoded[:maximum].decode("utf-8", errors="ignore")
    return f"{prefix}\n...[truncated {len(encoded) - maximum} bytes]"


ACTIONS = {
    "ptc_eval": ptc_eval,
    "terminal_start": terminal_start,
    "terminal_poll": terminal_poll,
    "terminal_send": terminal_send,
    "terminal_stop": terminal_stop,
    "lsp_request": lsp_request,
    "health": health,
    "security_probe": security_probe,
    "list_files": list_files,
    "read_file": read_file,
    "search_text": search_text,
    "apply_patch": apply_patch,
    "git_status": git_status,
    "git_diff": git_diff,
    "run_command": run_command,
    "shell_exec": shell_exec,
    "run_skill_script": run_skill_script,
    "pip_check_requirements": pip_check_requirements,
    "pip_install": pip_install,
    "cleanup_processes": cleanup_processes,
}


if __name__ == "__main__":
    raise SystemExit(main())
