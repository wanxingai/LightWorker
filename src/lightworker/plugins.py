"""Pinned declarative plugins. User trust is bound to every file's content hash."""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path


class PluginManager:
    def __init__(self, store, directories=(), public_keys=()):
        self.store = store
        self.directories = [Path(p).expanduser().resolve() for p in directories]
        self.public_keys = list(public_keys)
        self.run_id = "lightworker-plugins"

    @staticmethod
    def inspect(directory):
        directory = Path(directory).resolve()
        manifest = json.loads((directory / "plugin.json").read_text())
        if not isinstance(manifest, dict) or not manifest.get("name") or not manifest.get("version"):
            raise ValueError("plugin.json requires name and version")
        digest = hashlib.sha256()
        files = []
        for path in sorted(directory.rglob("*")):
            if path.is_symlink():
                raise ValueError("plugin symlinks are not allowed")
            if not path.is_file() or path.name == "signature.json":
                continue
            relative = path.relative_to(directory).as_posix()
            if path.stat().st_size > 2_097_152:
                raise ValueError("plugin file exceeds size limit")
            payload = path.read_bytes()
            digest.update(relative.encode() + b"\0" + hashlib.sha256(payload).digest())
            files.append(relative)
        if set(manifest) - {"name", "version", "description", "skills", "mcp", "workflows"}:
            raise ValueError("unsupported plugin manifest key")
        return {
            "name": manifest["name"],
            "version": manifest["version"],
            "path": str(directory),
            "digest": digest.hexdigest(),
            "files": files,
            "manifest": manifest,
        }

    def _lock(self):
        try:
            return self.store.read_json(self.run_id, "plugin-lock.json")
        except FileNotFoundError:
            return {"plugins": {}}

    def discover(self):
        values = []
        locks = self._lock()["plugins"]
        for root in self.directories:
            candidates = [root] if (root / "plugin.json").is_file() else sorted(root.glob("*/plugin.json"))
            for candidate in candidates:
                directory = candidate.parent if candidate.name == "plugin.json" else candidate
                try:
                    value = self.inspect(directory)
                    value["trusted"] = locks.get(value["path"], {}).get("digest") == value["digest"]
                    value["signature_valid"] = self._verify_signature(value)
                    if self.public_keys and not value["signature_valid"]:
                        value["trusted"] = False
                    values.append(value)
                except (ValueError, OSError) as exc:
                    values.append({"path": str(directory), "trusted": False, "error": str(exc)})
        return values

    def _verify_signature(self, value):
        path = Path(value["path"]) / "signature.json"
        if not path.is_file():
            return False
        try:
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

            signature = json.loads(path.read_text())
            key = signature["public_key"]
            if key not in self.public_keys:
                return False
            Ed25519PublicKey.from_public_bytes(base64.b64decode(key)).verify(
                base64.b64decode(signature["signature"]), value["digest"].encode()
            )
            return True
        except Exception:
            return False

    def trust(self, directory, expected_digest):
        value = self.inspect(directory)
        if not any(Path(value["path"]).is_relative_to(root) for root in self.directories):
            raise ValueError("plugin is outside the configured directories")
        if value["digest"] != expected_digest:
            raise ValueError("plugin changed since inspection")
        if self.public_keys and not self._verify_signature(value):
            raise ValueError("plugin signature is not trusted")
        with self.store.transaction(self.run_id):
            lock = self._lock()
            lock["plugins"][value["path"]] = {k: value[k] for k in ("name", "version", "digest")}
            self.store.write_json(self.run_id, "plugin-lock.json", lock)
            self.store.session(self.run_id).append("plugin.trusted", {"plugin": value})
        return value

    def apply(self, config):
        from .config import MCPServerConfig

        configured = config.model_copy(deep=True)
        for plugin in self.discover():
            if not plugin.get("trusted"):
                continue
            root = Path(plugin["path"])
            for relative in plugin["manifest"].get("skills", []):
                directory = (root / relative).resolve()
                if not directory.is_relative_to(root):
                    raise ValueError("plugin skill path escapes its directory")
                configured.skills.managed_directories.append(directory)
            for name, server in plugin["manifest"].get("mcp", {}).items():
                namespace = f"{plugin['name']}_{name}"
                if namespace in configured.mcp.servers:
                    raise ValueError("plugin MCP namespace conflict")
                configured.mcp.servers[namespace] = MCPServerConfig.model_validate(server)
            for name, steps in plugin["manifest"].get("workflows", {}).items():
                namespace = f"{plugin['name']}_{name}"
                if namespace in configured.runtime.workflow_presets:
                    raise ValueError("plugin workflow namespace conflict")
                configured.runtime.workflow_presets[namespace] = steps
        return configured
