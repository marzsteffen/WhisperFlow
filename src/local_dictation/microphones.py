from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class Microphone:
    node_name: str
    description: str
    object_id: int
    serial: int | None = None
    muted: bool | None = None


class MicrophoneError(RuntimeError):
    pass


def _is_muted(object_id: int, *, runner=subprocess.run) -> bool | None:
    try:
        result = runner(
            ["wpctl", "get-volume", str(object_id)],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return "[MUTED]" in result.stdout.upper()


def list_microphones(*, runner=subprocess.run) -> list[Microphone]:
    try:
        result = runner(
            ["pw-dump"], capture_output=True, text=True, timeout=5, check=False
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise MicrophoneError("PipeWire-Geräte konnten nicht gelesen werden") from exc
    if result.returncode != 0:
        raise MicrophoneError("pw-dump ist fehlgeschlagen")
    try:
        objects: list[dict[str, Any]] = json.loads(result.stdout)
    except (ValueError, TypeError) as exc:
        raise MicrophoneError("pw-dump lieferte ungültige Daten") from exc

    microphones: list[Microphone] = []
    for obj in objects:
        props = obj.get("info", {}).get("props", {})
        if props.get("media.class") not in {"Audio/Source", "Audio/Source/Virtual"}:
            continue
        name = props.get("node.name")
        if not isinstance(name, str) or not name:
            continue
        object_id = obj.get("id")
        if not isinstance(object_id, int):
            continue
        serial = props.get("object.serial")
        microphones.append(
            Microphone(
                node_name=name,
                description=str(props.get("node.description") or props.get("node.nick") or name),
                object_id=object_id,
                serial=int(serial) if isinstance(serial, (int, str)) and str(serial).isdigit() else None,
                muted=_is_muted(object_id, runner=runner),
            )
        )
    return sorted(microphones, key=lambda mic: mic.description.casefold())

