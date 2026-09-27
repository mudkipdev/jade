from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import tempfile
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from pathlib import Path

from .recipe import AudioClip, Clip

type Command = tuple[int, list[str]]
type SectionDefinition = tuple[str, list[Command]]

VIDEO_COMMANDS = frozenset(("add", "speed", "volume", "mute", "compress", "trim"))
AUDIO_COMMANDS = frozenset(("add", "speed", "volume", "mute", "trim"))

UNITS = {
    "b": 1,
    "kb": 1_000,
    "kib": 1_024,
    "mb": 1_000_000,
    "mib": 1_048_576,
    "gb": 1_000_000_000,
    "gib": 1_073_741_824,
}


def parse_time(value: str) -> Decimal:
    units = re.fullmatch(r"(?:(\d+)h)?(?:(\d+)m)?(?:(\d+(?:\.\d+)?)s)?", value, re.IGNORECASE)

    if units is not None and any(part is not None for part in units.groups()):
        hours, minutes, seconds = units.groups()
        return Decimal(hours or 0) * 3600 + Decimal(minutes or 0) * 60 + Decimal(seconds or 0)

    parts = value.split(":")

    if not 1 <= len(parts) <= 3:
        raise ValueError(f"Invalid time: {value}")

    try:
        numbers = [Decimal(part) for part in parts]
    except InvalidOperation as error:
        raise ValueError(f"Invalid time: {value}") from error

    if any(not number.is_finite() or number < 0 for number in numbers):
        raise ValueError(f"Invalid time: {value}")

    if len(numbers) > 1 and any(number >= 60 for number in numbers[1:]):
        raise ValueError(f"Invalid time: {value}")

    return sum(number * 60 ** (len(numbers) - index - 1) for index, number in enumerate(numbers))


def parse_size(value: str) -> int:
    match = re.fullmatch(r"(\d+(?:\.\d+)?)(b|kb|kib|mb|mib|gb|gib)", value, re.IGNORECASE)

    if match is None:
        raise ValueError(f"Invalid size {value!r}; use a value such as 20mb")

    size = int(Decimal(match.group(1)) * UNITS[match.group(2).lower()])

    if size <= 0:
        raise ValueError("Compression size must be positive")

    return size


def parse_factor(value: str, command: str) -> Decimal:
    match = re.fullmatch(r"((?:\d+(?:\.\d*)?|\.\d+))(x|%)", value, re.IGNORECASE)
    if match is None:
        raise ValueError(f"Invalid {command} {value!r}; use a value such as 2x or 50%")

    factor = Decimal(match.group(1))

    if match.group(2) == "%":
        factor /= 100

    if factor < 0 or (command == "speed" and factor == 0):
        raise ValueError(f"{command} must be {'positive' if command == 'speed' else 'nonnegative'}")

    return factor


def probe_clip(path: Path) -> Clip:
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration:stream=codec_type,width,height,avg_frame_rate,r_frame_rate",
            "-of",
            "json",
            str(path),
        ],
        text=True,
        capture_output=True,
        check=False,
    )

    if result.returncode:
        raise ValueError(f"Cannot read {path}: {result.stderr.strip()}")

    data = json.loads(result.stdout)
    video = next(
        (stream for stream in data.get("streams", []) if stream.get("codec_type") == "video"),
        None,
    )

    if video is None:
        raise ValueError(f"No video stream in {path}")

    try:
        duration = Decimal(data["format"]["duration"])
        width, height = int(video["width"]), int(video["height"])
    except (KeyError, InvalidOperation, TypeError) as error:
        raise ValueError(f"Missing video metadata in {path}") from error

    if duration <= 0 or width <= 0 or height <= 0:
        raise ValueError(f"Invalid video metadata in {path}")

    fps = None

    for field in ("avg_frame_rate", "r_frame_rate"):
        try:
            candidate = Fraction(video[field])
        except KeyError, TypeError, ValueError, ZeroDivisionError:
            continue

        if candidate > 0:
            fps = candidate
            break

    if fps is None:
        raise ValueError(f"Cannot determine frame rate for {path}")

    return Clip(
        path,
        duration,
        width,
        height,
        fps,
        any(stream.get("codec_type") == "audio" for stream in data.get("streams", [])),
    )


def probe_audio(path: Path) -> AudioClip:
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration:stream=codec_type,duration",
            "-of",
            "json",
            str(path),
        ],
        text=True,
        capture_output=True,
        check=False,
    )

    if result.returncode:
        raise ValueError(f"Cannot read {path}: {result.stderr.strip()}")

    data = json.loads(result.stdout)
    audio = next(
        (stream for stream in data.get("streams", []) if stream.get("codec_type") == "audio"),
        None,
    )

    if audio is None:
        raise ValueError(f"No audio stream in {path}")

    try:
        duration = Decimal(data.get("format", {}).get("duration") or audio["duration"])
    except (KeyError, InvalidOperation, TypeError) as error:
        raise ValueError(f"Missing audio duration in {path}") from error

    if not duration.is_finite() or duration <= 0:
        raise ValueError(f"Invalid audio duration in {path}")

    return AudioClip(path, duration)


def parse_recipe(recipe: str) -> tuple[dict[str, SectionDefinition], list[Command]]:
    sections: dict[str, SectionDefinition] = {}
    commands: list[Command] = []
    current_section = None

    for line_number, line in enumerate(recipe.splitlines(), 1):
        try:
            words = shlex.split(line, comments=True)
        except ValueError as error:
            raise ValueError(f"Line {line_number}: {error}") from error

        if not words:
            continue

        indented = line[0].isspace()

        if (
            not indented
            and words[-1].endswith(":")
            and (len(words) == 1 or (len(words) == 2 and words[0] == "audio"))
        ):
            kind = "audio" if len(words) == 2 else "video"
            name = words[-1][:-1]

            if not name or name in sections:
                raise ValueError(f"Line {line_number}: invalid or duplicate section name {name!r}")

            sections[name] = (kind, [])
            current_section = name
        elif indented:
            if current_section is None:
                raise ValueError(f"Line {line_number}: indented command outside a section")

            kind, section_commands = sections[current_section]

            if words[0] not in (AUDIO_COMMANDS if kind == "audio" else VIDEO_COMMANDS):
                raise ValueError(f"Line {line_number}: unknown section command {words[0]!r}")

            section_commands.append((line_number, words))
        else:
            current_section = None
            commands.append((line_number, words))

    return sections, commands


def initial_recipe(paths: list[Path]) -> str:
    lines = [f"add {shlex.quote(str(path))}" for path in paths]
    return "\n".join(lines) + "\n" if lines else ""


def edit_recipe(paths: list[Path]) -> str:
    editor_command = os.environ.get("VISUAL") or os.environ.get("EDITOR") or "vi"
    command = shlex.split(editor_command)

    if not command:
        raise ValueError("Editor command is empty")

    descriptor, filename = tempfile.mkstemp(prefix="jade-")
    os.close(descriptor)
    recipe_path = Path(filename)

    try:
        recipe_path.write_text(initial_recipe(paths), encoding="utf-8")
        result = subprocess.run([*command, str(recipe_path)], check=False)

        if result.returncode:
            raise ValueError(f"Editor exited with status {result.returncode}")

        return recipe_path.read_text(encoding="utf-8")
    finally:
        recipe_path.unlink(missing_ok=True)
