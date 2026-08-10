"""Markdown Skill discovery with project/user/managed precedence and Docker-only scripts."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .config import SkillsConfig
from .models import ApprovalPolicy, ToolCategory
from .sandbox import SandboxBackend
from .tool_protocol import tool_info

FRONTMATTER = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


@dataclass(frozen=True)
class SkillDefinition:
    name: str
    description: str
    path: Path
    source: str
    version: str = ""
    allowed_tools: tuple[str, ...] = ()
    required_permissions: tuple[str, ...] = ()
    network: bool = False
    trusted: bool = False
    compatibility: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


class SkillRegistry:
    def __init__(self, *, workspace: Path, config: SkillsConfig):
        self.workspace = workspace.resolve()
        self.config = config
        self.skills: dict[str, SkillDefinition] = {}
        self.conflicts: dict[str, list[str]] = {}

    def discover(self) -> list[SkillDefinition]:
        self.skills.clear()
        self.conflicts.clear()
        directories = [
            ("project", self.workspace / ".lightworker" / "skills"),
            *(("user", path.expanduser()) for path in self.config.user_directories),
            *(("managed", path.expanduser()) for path in self.config.managed_directories),
            ("builtin", Path(__file__).resolve().parent / "builtin_skills"),
        ]
        for source, directory in directories:
            if not directory.is_dir():
                continue
            for skill_dir in sorted(path for path in directory.iterdir() if path.is_dir()):
                skill_file = skill_dir / "SKILL.md"
                if not skill_file.is_file():
                    continue
                try:
                    definition = self._load_metadata(skill_file, source)
                except (OSError, ValueError, yaml.YAMLError):
                    continue
                if definition.name in self.skills:
                    self.conflicts.setdefault(
                        definition.name, [str(self.skills[definition.name].path)]
                    ).append(str(definition.path))
                    continue
                self.skills[definition.name] = definition
        return list(self.skills.values())

    def activate(self, name: str) -> str:
        skill = self.get(name)
        raw = self._read_bounded(skill.path / "SKILL.md")
        return FRONTMATTER.sub("", raw, count=1).strip()

    def read_reference(self, name: str, relative: str) -> str:
        skill = self.get(name)
        path = self._safe_child(skill.path / "references", relative)
        if not path.is_file():
            raise ValueError("skill reference not found")
        return self._read_bounded(path)

    def script(self, name: str, relative: str) -> tuple[SkillDefinition, Path, str]:
        skill = self.get(name)
        path = self._safe_child(skill.path / "scripts", relative)
        if not path.is_file() or path.suffix.lower() not in {".py", ".sh"}:
            raise ValueError("skill script must be an existing .py or .sh file")
        return skill, path, self._read_bounded(path)

    def get(self, name: str) -> SkillDefinition:
        if not self.skills:
            self.discover()
        skill = self.skills.get(name)
        if skill is None:
            raise ValueError(f"skill not found: {name}")
        return skill

    def manifest(self) -> dict[str, Any]:
        if not self.skills:
            self.discover()
        return {
            "skills": [
                {
                    "name": item.name,
                    "description": item.description,
                    "source": item.source,
                    "version": item.version,
                    "allowed_tools": list(item.allowed_tools),
                    "required_permissions": list(item.required_permissions),
                    "network": item.network,
                    "trusted": item.trusted,
                    "compatibility": item.compatibility,
                }
                for item in self.skills.values()
            ],
            "conflicts": self.conflicts,
        }

    def _load_metadata(self, path: Path, source: str) -> SkillDefinition:
        raw = self._read_bounded(path)
        match = FRONTMATTER.match(raw)
        if not match:
            raise ValueError("SKILL.md is missing YAML frontmatter")
        payload = yaml.safe_load(match.group(1)) or {}
        if not isinstance(payload, dict):
            raise ValueError("skill frontmatter must be a mapping")
        name = str(payload.get("name") or "")
        description = str(payload.get("description") or "")
        if not SAFE_NAME.fullmatch(name) or not description:
            raise ValueError("skill name or description is invalid")
        return SkillDefinition(
            name=name,
            description=description,
            path=path.parent.resolve(),
            source=source,
            version=str(payload.get("version") or ""),
            allowed_tools=tuple(str(item) for item in payload.get("allowed_tools") or []),
            required_permissions=tuple(str(item) for item in payload.get("required_permissions") or []),
            network=bool(payload.get("network", False)),
            trusted=bool(payload.get("trusted", source == "managed")),
            compatibility=str(payload.get("compatibility") or ""),
            metadata=payload,
        )

    def _read_bounded(self, path: Path) -> str:
        raw = path.read_bytes()
        if len(raw) > self.config.max_skill_bytes:
            raise ValueError("skill file exceeds configured size limit")
        return raw.decode("utf-8", errors="replace")

    @staticmethod
    def _safe_child(root: Path, relative: str) -> Path:
        if not relative or Path(relative).is_absolute() or ".." in Path(relative).parts:
            raise ValueError("unsafe skill path")
        resolved_root = root.resolve()
        path = (root / relative).resolve()
        try:
            path.relative_to(resolved_root)
        except ValueError as exc:
            raise ValueError("skill path escapes its directory") from exc
        return path


class SkillTools:
    def __init__(self, registry: SkillRegistry, sandbox: SandboxBackend, config: SkillsConfig):
        self.registry = registry
        self.sandbox = sandbox
        self.config = config
        self.tools = [self.list_skills, self.activate_skill, self.skill_read_reference]
        if config.allow_scripts:
            self.tools.append(self.skill_run_script)

    @tool_info(
        "list_skills", "List available Markdown Skills and source conflicts.", [], category=ToolCategory.SKILL
    )
    def list_skills(self) -> str:
        return json.dumps(self.registry.manifest(), ensure_ascii=False)

    @tool_info(
        "activate_skill",
        "Load the full Markdown instructions for one discovered Skill.",
        [{"name": "name", "description": "Skill name", "type": "string", "required": True}],
        category=ToolCategory.SKILL,
    )
    def activate_skill(self, name: str) -> str:
        try:
            return self.registry.activate(name)
        except ValueError as exc:
            return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)

    @tool_info(
        "skill_read_reference",
        "Read one file below a Skill's references directory.",
        [
            {"name": "name", "description": "Skill name", "type": "string", "required": True},
            {"name": "path", "description": "Reference-relative path", "type": "string", "required": True},
        ],
        category=ToolCategory.SKILL,
    )
    def skill_read_reference(self, name: str, path: str) -> str:
        try:
            return self.registry.read_reference(name, path)
        except ValueError as exc:
            return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)

    @tool_info(
        "skill_run_script",
        "Run a reviewed Skill script inside the existing Docker task sandbox, never on the host.",
        [
            {"name": "name", "description": "Skill name", "type": "string", "required": True},
            {
                "name": "script",
                "description": "Script-relative .py or .sh path",
                "type": "string",
                "required": True,
            },
            {"name": "args", "description": "Argument vector", "type": "array", "required": False},
        ],
        category=ToolCategory.SKILL,
        is_read_only=False,
        is_write=True,
        external_side_effect=True,
        sandbox_required=True,
        approval_policy=ApprovalPolicy.ALWAYS,
        timeout_seconds=300,
    )
    def skill_run_script(self, name: str, script: str, args: list[str] | None = None) -> str:
        try:
            skill, path, content = self.registry.script(name, script)
            if skill.network:
                return json.dumps(
                    {"ok": False, "error": "networked Skill scripts are disabled in the task sandbox"},
                    ensure_ascii=False,
                )
            response = self.sandbox.call(
                "run_skill_script",
                {"name": path.name, "content": content, "args": list(args or [])},
                timeout=300,
            )
            return json.dumps(response, ensure_ascii=False)
        except ValueError as exc:
            return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)
