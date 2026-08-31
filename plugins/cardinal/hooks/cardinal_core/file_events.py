"""Best-effort file attribution for Semantic DAG lifecycle-hook fallbacks.

Claude and compatible Codex runtimes expose successful tool calls through
``PostToolUse``. This module turns the stable parts of those payloads into the
``read`` and ``updated`` file metadata shown in a Semantic DAG node drawer.
Codex Desktop's primary path consumes its structured session events instead.

Direct file tools and apply-patch payloads are exact. Shell attribution is
deliberately conservative: it recognizes common read commands, redirects,
and simple file mutation commands without executing or expanding input.
"""
from __future__ import annotations

import os
import re
import shlex
from pathlib import Path


_READ_ACTIONS = {
    "read",
    "read_file",
    "read_text_file",
    "get_file",
    "view_file",
    "view_image",
}
_UPDATE_ACTIONS = {
    "write",
    "write_file",
    "write_text_file",
    "edit",
    "multi_edit",
    "multiedit",
    "notebook_edit",
    "create_file",
    "delete_file",
    "remove_file",
}
_MOVE_ACTIONS = {"copy", "copy_file", "move", "move_file", "rename", "rename_file"}
_SHELL_ACTIONS = {"bash", "shell", "powershell", "exec_command"}
_READ_COMMANDS = {
    "awk",
    "bat",
    "cat",
    "cmp",
    "cut",
    "diff",
    "grep",
    "head",
    "jq",
    "less",
    "more",
    "nl",
    "rg",
    "sed",
    "sort",
    "tail",
    "uniq",
    "wc",
}
_MUTATE_COMMANDS = {"chmod", "chown", "rm", "touch", "truncate", "unlink"}
_SEPARATORS = {"&&", "||", ";", "|"}
_PATH_KEYS = ("file_path", "path", "notebook_path")
_PATH_LIST_KEYS = ("file_paths", "paths")
_PATCH_PATH_RE = re.compile(
    r"^\*\*\* (?:Add|Update|Delete) File: (?P<path>.+?)\s*$|"
    r"^\*\*\* Move to: (?P<move>.+?)\s*$",
    re.MULTILINE,
)
_REDIRECT_RE = re.compile(
    r"(?<![<>])>{1,2}\s*(?:\"(?P<double>[^\"]+)\"|'(?P<single>[^']+)'|(?P<plain>[^\s;&|]+))"
)
_SENSITIVE_DIRS = {".aws", ".git", ".gnupg", ".kube", ".ssh"}
_SENSITIVE_NAMES = {
    ".env",
    "credentials",
    "credentials.json",
    "id_dsa",
    "id_ed25519",
    "id_rsa",
}
_SENSITIVE_SUFFIXES = {".key", ".p12", ".pfx", ".pem"}


def _leaf_action(tool_name: object) -> str:
    value = str(tool_name or "").strip().lower().replace("-", "_")
    if "__" in value:
        value = value.rsplit("__", 1)[-1]
    return value


def _is_sensitive(path: Path) -> bool:
    lowered_parts = {part.lower() for part in path.parts}
    name = path.name.lower()
    return (
        bool(lowered_parts & _SENSITIVE_DIRS)
        or name in _SENSITIVE_NAMES
        or name.startswith(".env.")
        or path.suffix.lower() in _SENSITIVE_SUFFIXES
    )


def _normalize_path(value: object, cwd: Path) -> str | None:
    if not isinstance(value, str):
        return None
    raw = value.strip().strip("\"'")
    if (
        not raw
        or raw == "-"
        or "\x00" in raw
        or "\n" in raw
        or "://" in raw
        or raw.startswith("-")
        or any(char in raw for char in "$*?[]{}")
    ):
        return None
    candidate = Path(os.path.expanduser(raw))
    if not candidate.is_absolute():
        candidate = cwd / candidate
    candidate = Path(os.path.abspath(str(candidate)))
    if _is_sensitive(candidate):
        return None
    return str(candidate)


def _path_values(tool_input: dict, cwd: Path) -> list[str]:
    values: list[object] = [tool_input.get(key) for key in _PATH_KEYS]
    for key in _PATH_LIST_KEYS:
        item = tool_input.get(key)
        if isinstance(item, list):
            values.extend(item)
    paths: list[str] = []
    for value in values:
        path = _normalize_path(value, cwd)
        if path and path not in paths:
            paths.append(path)
    return paths


def _existing_file_tokens(tokens: list[str], cwd: Path) -> list[str]:
    paths: list[str] = []
    for token in tokens:
        if token in _SEPARATORS or token.startswith("-"):
            continue
        path = _normalize_path(token, cwd)
        if path and Path(path).is_file() and path not in paths:
            paths.append(path)
    return paths


def _shell_events(command: str, cwd: Path) -> list[tuple[str, str]]:
    try:
        tokens = shlex.split(command, posix=os.name != "nt")
    except ValueError:
        tokens = command.replace("\n", " ").split()

    commands = {
        Path(token).name.lower()
        for index, token in enumerate(tokens)
        if token not in _SEPARATORS
        and not token.startswith("-")
        and (index == 0 or tokens[index - 1] in _SEPARATORS)
    }
    events: list[tuple[str, str]] = []

    def add(kind: str, value: object) -> None:
        path = _normalize_path(value, cwd)
        event = (kind, path) if path else None
        if event and event not in events:
            events.append(event)

    for match in _REDIRECT_RE.finditer(command):
        add("updated", match.group("double") or match.group("single") or match.group("plain"))

    for index, token in enumerate(tokens):
        command_name = Path(token).name.lower()
        if command_name == "tee":
            for candidate in tokens[index + 1 :]:
                if candidate in _SEPARATORS:
                    break
                if not candidate.startswith("-"):
                    add("updated", candidate)
        elif command_name in _MUTATE_COMMANDS:
            for candidate in tokens[index + 1 :]:
                if candidate in _SEPARATORS:
                    break
                if not candidate.startswith("-"):
                    add("updated", candidate)
        elif command_name in {"cp", "install", "mv"}:
            arguments = []
            for candidate in tokens[index + 1 :]:
                if candidate in _SEPARATORS:
                    break
                if not candidate.startswith("-"):
                    arguments.append(candidate)
            if len(arguments) >= 2:
                source_kind = "updated" if command_name == "mv" else "read"
                for source in arguments[:-1]:
                    add(source_kind, source)
                add("updated", arguments[-1])

    if commands & _READ_COMMANDS:
        for path in _existing_file_tokens(tokens, cwd):
            event = ("read", path)
            if event not in events and ("updated", path) not in events:
                events.append(event)

    if "sed" in commands and any(token == "-i" or token.startswith("-i") for token in tokens):
        for path in _existing_file_tokens(tokens, cwd):
            read_event = ("read", path)
            if read_event in events:
                events.remove(read_event)
            update_event = ("updated", path)
            if update_event not in events:
                events.append(update_event)

    return events


def _patch_events(command: str, cwd: Path) -> list[tuple[str, str]]:
    events: list[tuple[str, str]] = []
    for match in _PATCH_PATH_RE.finditer(command):
        path = _normalize_path(match.group("path") or match.group("move"), cwd)
        event = ("updated", path) if path else None
        if event and event not in events:
            events.append(event)
    return events


def file_events_from_hook(payload: object) -> list[tuple[str, str]]:
    """Return ordered ``(read|updated, absolute_path)`` events for a hook.

    File events are emitted only after successful tool use. Claude's
    ``FileChanged`` lifecycle event is also accepted when callers choose to
    register it, because it is already post-change and path-specific.
    """
    if not isinstance(payload, dict):
        return []
    event_name = str(payload.get("hook_event_name") or "")
    cwd = Path(os.path.expanduser(str(payload.get("cwd") or os.getcwd())))

    if event_name == "FileChanged":
        path = _normalize_path(payload.get("file_path"), cwd)
        return [("updated", path)] if path else []
    if event_name != "PostToolUse":
        return []

    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        tool_input = {}
    action = _leaf_action(payload.get("tool_name"))

    if action == "apply_patch":
        command = tool_input.get("command") or tool_input.get("patch") or ""
        return _patch_events(command, cwd) if isinstance(command, str) else []
    if action in _SHELL_ACTIONS:
        command = tool_input.get("command") or tool_input.get("cmd") or ""
        return _shell_events(command, cwd) if isinstance(command, str) else []
    if action in _MOVE_ACTIONS:
        events: list[tuple[str, str]] = []
        source = tool_input.get("source_path") or tool_input.get("source")
        destination = (
            tool_input.get("destination_path")
            or tool_input.get("destination")
            or tool_input.get("target_path")
        )
        source_path = _normalize_path(source, cwd)
        destination_path = _normalize_path(destination, cwd)
        if source_path:
            events.append(("updated" if action.startswith(("move", "rename")) else "read", source_path))
        if destination_path:
            events.append(("updated", destination_path))
        return events
    if action in _READ_ACTIONS or action.startswith("read_"):
        return [("read", path) for path in _path_values(tool_input, cwd)]
    if action in _UPDATE_ACTIONS or action.startswith(("write_", "edit_", "delete_")):
        return [("updated", path) for path in _path_values(tool_input, cwd)]
    return []
