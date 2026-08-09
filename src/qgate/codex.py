"""Codex Change Event interpretation and Gate Target Selection."""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO, cast

from qgate.targeting import select_gate_targets

type JsonValue = bool | int | float | str | list[JsonValue] | dict[str, JsonValue] | None


_PATCH_FILE_PATTERN = re.compile(
    r"^\*\*\*\s+(?:(?:Add|Delete|Update)\s+File|Move\s+to):\s*(.+?)\s*$",
    re.MULTILINE,
)


@dataclass(frozen=True)
class _ChangeEventInterpretation:
    candidate_paths: tuple[str, ...]
    reported_working_directory: str | None


def select_codex_gate_targets(stream: TextIO, workspace: Path) -> list[Path]:
    """Select safe Gate Targets reported by one Codex Change Event."""
    candidates = _interpret_change_event(stream)
    if candidates is None:
        return []
    return select_gate_targets(
        candidates.candidate_paths,
        workspace=workspace,
        reported_working_directory=candidates.reported_working_directory,
    )


def _interpret_change_event(stream: TextIO) -> _ChangeEventInterpretation | None:
    payload = _load_payload(stream)
    if payload is None:
        return None

    raw_working_directory = payload.get("cwd")
    reported_working_directory = (
        raw_working_directory if isinstance(raw_working_directory, str) else None
    )
    return _ChangeEventInterpretation(
        candidate_paths=tuple(_payload_paths(payload)),
        reported_working_directory=reported_working_directory,
    )


def _load_payload(stream: TextIO) -> dict[str, JsonValue] | None:
    try:
        raw_payload: object = json.load(stream)
        payload = _json_value(raw_payload)
    except (json.JSONDecodeError, TypeError):
        return None
    return payload if isinstance(payload, dict) else None


def _json_value(value: object) -> JsonValue:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, dict):
        raw_dict = cast(dict[object, object], value)
        converted: dict[str, JsonValue] = {}
        for key, child in raw_dict.items():
            if not isinstance(key, str):
                raise TypeError("JSON object keys must be strings")
            converted[key] = _json_value(child)
        return converted
    if isinstance(value, list):
        return [_json_value(child) for child in cast(list[object], value)]
    raise TypeError(f"Unsupported JSON value: {type(value).__name__}")


def _string_values(value: JsonValue) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for child in value:
            yield from _string_values(child)


def _payload_paths(payload: dict[str, JsonValue]) -> list[str]:
    tool_input = payload.get("tool_input") or payload.get("toolInput")
    if not isinstance(tool_input, dict):
        return []

    paths: list[str] = []
    for key in ("path", "file_path", "target_file", "paths"):
        value = tool_input.get(key)
        if value is not None:
            paths.extend(_string_values(value))

    tool_name = payload.get("tool_name") or payload.get("toolName")
    command = tool_input.get("command")
    if tool_name == "apply_patch" and isinstance(command, str):
        paths.extend(_PATCH_FILE_PATTERN.findall(command))
    return paths
