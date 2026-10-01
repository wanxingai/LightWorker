"""Read-only Python symbol intelligence plus an approved Docker LSP transport."""

from __future__ import annotations

import ast
import json

from .models import ApprovalPolicy, ToolCategory
from .tool_protocol import tool_info


class LanguageTools:
    def __init__(self, repo_tools, sandbox):
        self.repo_tools, self.sandbox = repo_tools, sandbox
        self.tools = [self.code_symbols]
        if getattr(sandbox, "supports_shell", False):
            self.tools.append(self.lsp_request)

    @tool_info(
        "code_symbols",
        "Read Python definitions, imports and syntax diagnostics without execution.",
        [{"name": "path", "type": "string", "required": True}],
        category=ToolCategory.WORKSPACE,
    )
    def code_symbols(self, path):
        payload = self.repo_tools.read_file(path=path)
        payload = json.loads(payload) if isinstance(payload, str) else payload
        if payload.get("ok") is False:
            return json.dumps(payload)
        try:
            tree = ast.parse(payload.get("content", ""), filename=path)
        except SyntaxError as exc:
            return json.dumps(
                {
                    "ok": True,
                    "symbols": [],
                    "diagnostics": [{"line": exc.lineno, "column": exc.offset, "message": exc.msg}],
                }
            )
        symbols = [
            {"name": node.name, "kind": type(node).__name__, "line": node.lineno, "end_line": node.end_lineno}
            for node in ast.walk(tree)
            if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
        ]
        return json.dumps({"ok": True, "symbols": symbols, "diagnostics": []})

    @tool_info(
        "lsp_request",
        "Send a JSON-RPC request to an allowlisted stdio language server in Docker. "
        "The server must already be installed. No host process is started.",
        [
            {"name": "argv", "type": "array", "required": True},
            {"name": "method", "type": "string", "required": True},
            {"name": "params", "type": "object", "required": True},
        ],
        category=ToolCategory.SHELL,
        is_read_only=False,
        is_write=True,
        sandbox_required=True,
        approval_policy=ApprovalPolicy.ALWAYS,
    )
    def lsp_request(self, argv, method, params):
        return json.dumps(
            self.sandbox.call("lsp_request", {"argv": argv, "method": method, "params": params}, timeout=30)
        )
