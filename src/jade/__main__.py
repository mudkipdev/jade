from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from pathlib import Path

import click


@dataclass(frozen=True)
class Clip:
    path: Path
    duration: Decimal
    width: int
    height: int
    fps: Fraction
    has_audio: bool


@dataclass(frozen=True)
class Edit:
    clip: Clip
    start: Decimal | None = None
    end: Decimal | None = None

    @property
    def duration(self) -> Decimal:
        return (
            self.clip.duration if self.start is None or self.end is None else self.end - self.start
        )


@dataclass(frozen=True)
class Section:
    items: list[Edit | Section]
    speed: Decimal = Decimal(1)
    volume: Decimal = Decimal(1)
    target_bytes: int | None = None

    @property
    def duration(self) -> Decimal:
        return sum((item.duration for item in self.items), Decimal(0)) / self.speed


@dataclass(frozen=True)
class Plan:
    root: Section
    save: Path


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
        raise ValueError(f"Cannot determine frame rate for {path}; specify --fps")
    return Clip(
        path,
        duration,
        width,
        height,
        fps,
        any(stream.get("codec_type") == "audio" for stream in data.get("streams", [])),
    )


def parse_time(value: str) -> Decimal:
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
    match = re.fullmatch(r"(\d+(?:\.\d+)?)(b|kb|kib|mb|mib|gb|gib|tb|tib)", value, re.IGNORECASE)
    if match is None:
        raise ValueError(f"Invalid size {value!r}; use a value such as 20mb")
    units = {
        "b": 1,
        "kb": 1_000,
        "kib": 1_024,
        "mb": 1_000_000,
        "gb": 1_000_000_000,
        "mib": 1_048_576,
        "gib": 1_073_741_824,
        "tb": 1_000_000_000_000,
        "tib": 1_099_511_627_776,
    }
    size = int(Decimal(match.group(1)) * units[match.group(2).lower()])
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


def parse_todo(todo: str, base_dir: Path, default_save: Path | None) -> Plan:
    clip_cache: dict[Path, Clip] = {}
    sections: dict[str, list[tuple[int, list[str]]]] = {}
    commands: list[tuple[int, list[str]]] = []
    save = default_save
    save_seen = False

    current_section = None
    for line_number, line in enumerate(todo.splitlines(), 1):
        try:
            words = shlex.split(line, comments=True)
        except ValueError as error:
            raise ValueError(f"Line {line_number}: {error}") from error
        if not words:
            continue
        indented = line[0].isspace()
        if not indented and len(words) == 1 and words[0].endswith(":"):
            name = words[0][:-1]
            if not name or name in sections:
                raise ValueError(f"Line {line_number}: invalid or duplicate section name {name!r}")
            sections[name] = []
            current_section = name
        elif indented:
            if current_section is None:
                raise ValueError(f"Line {line_number}: indented command outside a section")
            if words[0] not in ("add", "speed", "volume", "mute", "compress"):
                raise ValueError(f"Line {line_number}: unknown section command {words[0]!r}")
            sections[current_section].append((line_number, words))
        else:
            current_section = None
            commands.append((line_number, words))

    def clip_for(reference: str) -> Clip:
        path = Path(reference).expanduser()
        if not path.is_absolute():
            path = base_dir / path
        path = path.resolve(strict=True)
        if path not in clip_cache:
            clip_cache[path] = probe_clip(path)
        return clip_cache[path]

    def build(lines: list[tuple[int, list[str]]], stack: tuple[str, ...] = ()) -> Section:
        nonlocal save, save_seen
        items: list[Edit | Section] = []
        speed = Decimal(1)
        volume = Decimal(1)
        target_bytes = None
        for line_number, words in lines:
            try:
                command, arguments = words[0], words[1:]
                if command == "save":
                    if stack:
                        raise ValueError("save is only allowed outside sections")
                    if len(arguments) != 1:
                        raise ValueError("expected 'save PATH'")
                    if save_seen:
                        raise ValueError("only one save command is allowed")
                    save_seen = True
                    save = Path(arguments[0]).expanduser()
                    if not save.is_absolute():
                        save = base_dir / save
                    save = save.absolute()
                    if save.suffix.lower() != ".mp4":
                        raise ValueError("save filename must end in .mp4")
                elif command == "add":
                    if len(arguments) == 1 and arguments[0] in sections:
                        name = arguments[0]
                        if name in stack:
                            raise ValueError(
                                f"recursive section reference: {' -> '.join((*stack, name))}"
                            )
                        child = build(sections[name], (*stack, name))
                        if not child.items:
                            raise ValueError(f"section {name!r} contains no clips")
                        items.append(child)
                    elif len(arguments) in (1, 3):
                        reference, *times = arguments
                        if reference in sections:
                            raise ValueError("a section cannot be trimmed; trim clips inside it")
                        items.append(make_edit(reference, times))
                    else:
                        raise ValueError("expected 'add PATH [START END]' or 'add SECTION'")
                elif command in ("speed", "volume", "mute"):
                    if (command == "mute" and arguments) or (
                        command != "mute" and len(arguments) != 1
                    ):
                        raise ValueError(
                            f"expected '{command}{' VALUE' if command != 'mute' else ''}'"
                        )
                    factor = (
                        Decimal(0) if command == "mute" else parse_factor(arguments[0], command)
                    )
                    if command == "speed":
                        speed = factor
                    else:
                        volume = factor
                elif command == "compress":
                    if len(arguments) != 1:
                        raise ValueError("expected 'compress SIZE', such as 'compress 20mb'")
                    if target_bytes is not None:
                        raise ValueError("only one compress command is allowed per section")
                    target_bytes = parse_size(arguments[0])
                else:
                    raise ValueError(f"unknown command {command!r}")
            except (OSError, ValueError) as error:
                if re.match(r"^Line \d+: ", str(error)):
                    raise
                raise ValueError(f"Line {line_number}: {error}") from error
        return Section(items, speed, volume, target_bytes)

    def make_edit(reference: str, times: list[str]) -> Edit:
        clip = clip_for(reference)
        if times:
            start, end = parse_time(times[0]), parse_time(times[1])
            if start >= end or end > clip.duration:
                raise ValueError(f"trim must satisfy 0 <= START < END <= {clip.duration} seconds")
            return Edit(clip, start, end)
        return Edit(clip)

    root = build(commands)
    if not root.items:
        raise ValueError("The edit list contains no clips to export")
    if save is None:
        raise ValueError("Specify --save PATH or add 'save PATH' in the editor")
    return Plan(root, save)


def initial_todo(paths: list[Path]) -> str:
    lines = [f"add {shlex.quote(str(path))}" for path in paths]
    return "\n".join(lines) + "\n" if lines else ""


def edit_todo(paths: list[Path], editor: str | None) -> str:
    editor_command = editor or os.environ.get("VISUAL") or os.environ.get("EDITOR") or "vi"
    command = shlex.split(editor_command)
    if not command:
        raise ValueError("Editor command is empty")
    descriptor, filename = tempfile.mkstemp(prefix="jade-")
    os.close(descriptor)
    todo_path = Path(filename)
    try:
        todo_path.write_text(initial_todo(paths), encoding="utf-8")
        result = subprocess.run([*command, str(todo_path)], check=False)
        if result.returncode:
            raise ValueError(f"Editor exited with status {result.returncode}")
        return todo_path.read_text(encoding="utf-8")
    finally:
        todo_path.unlink(missing_ok=True)


def run_ffmpeg(arguments: list[str]) -> None:
    result = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-n", *arguments],
        check=False,
    )
    if result.returncode:
        raise ValueError(f"FFmpeg exited with status {result.returncode}")


def audio_speed_filter(speed: Decimal) -> list[str]:
    filters = []
    remaining = speed
    while remaining > 2:
        filters.append("atempo=2")
        remaining /= 2
    while remaining < Decimal("0.5"):
        filters.append("atempo=0.5")
        remaining *= 2
    filters.append(f"atempo={remaining}")
    return filters


def iter_edits(section: Section):
    for item in section.items:
        if isinstance(item, Edit):
            yield item
        else:
            yield from iter_edits(item)


def render(plan: Plan, output: Path, fps: int | None) -> None:
    if not output.parent.is_dir():
        raise ValueError(f"Output directory does not exist: {output.parent}")
    edits = list(iter_edits(plan.root))
    first = edits[0].clip
    output_fps = str(fps or first.fps)
    width = first.width + first.width % 2
    height = first.height + first.height % 2
    use_audio = any(edit.clip.has_audio for edit in edits)
    video_filter = (
        f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={output_fps},format=yuv420p"
    )
    with tempfile.TemporaryDirectory(prefix="jade-", dir=output.parent) as directory:
        work = Path(directory)
        rendered_count = 0
        section_count = 0

        def render_clip(edit: Edit, segment: Path) -> None:
            nonlocal rendered_count
            clip = edit.clip
            command = []
            if edit.start is not None and edit.end is not None:
                command += ["-ss", str(edit.start), "-t", str(edit.end - edit.start)]
            command += ["-i", str(clip.path)]
            if use_audio and not clip.has_audio:
                command += ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo"]
            command += [
                "-map",
                "0:v:0",
                "-vf",
                video_filter,
                "-c:v",
                "libx264",
                "-preset",
                "medium",
                "-crf",
                "20",
                "-pix_fmt",
                "yuv420p",
                "-r",
                output_fps,
            ]
            if use_audio:
                command += [
                    "-map",
                    "0:a:0" if clip.has_audio else "1:a:0",
                    "-af",
                    "aresample=48000,apad",
                    "-c:a",
                    "aac",
                    "-b:a",
                    "192k",
                    "-ac",
                    "2",
                    "-ar",
                    "48000",
                    "-shortest",
                ]
            else:
                command += ["-an"]
            command.append(str(segment))
            rendered_count += 1
            print(
                f"Rendering {rendered_count}/{len(edits)}: {clip.path.name}",
                file=sys.stderr,
            )
            run_ffmpeg(command)

        def render_section(section: Section) -> Path:
            nonlocal section_count
            section_count += 1
            section_work = work / f"section_{section_count:04d}"
            section_work.mkdir()
            entries = []
            for index, item in enumerate(section.items):
                if isinstance(item, Edit):
                    segment = section_work / f"segment_{index:04d}.mp4"
                    render_clip(item, segment)
                else:
                    segment = render_section(item)
                relative = os.path.relpath(segment, section_work)
                entries.append(f"file '{relative}'")
            concat_file = section_work / "concat.txt"
            concat_file.write_text("\n".join(entries) + "\n", encoding="utf-8")
            stitched = section_work / "stitched.mp4"
            run_ffmpeg(
                [
                    "-f",
                    "concat",
                    "-safe",
                    "0",
                    "-i",
                    str(concat_file),
                    "-c",
                    "copy",
                    str(stitched),
                ]
            )
            if section.speed != 1 or (use_audio and section.volume != 1):
                adjusted = section_work / "adjusted.mp4"
                command = ["-i", str(stitched), "-map", "0:v:0"]
                if section.speed != 1:
                    command += [
                        "-vf",
                        f"setpts=(PTS-STARTPTS)/{section.speed},fps={output_fps},format=yuv420p",
                        "-c:v",
                        "libx264",
                        "-preset",
                        "medium",
                        "-crf",
                        "20",
                        "-r",
                        output_fps,
                    ]
                else:
                    command += ["-c:v", "copy"]
                if use_audio:
                    audio_filters = ["aresample=48000"]
                    if section.speed != 1:
                        audio_filters.extend(audio_speed_filter(section.speed))
                    audio_filters += [f"volume={section.volume}", "apad"]
                    command += [
                        "-map",
                        "0:a:0",
                        "-af",
                        ",".join(audio_filters),
                        "-c:a",
                        "aac",
                        "-b:a",
                        "192k",
                        "-ac",
                        "2",
                        "-ar",
                        "48000",
                        "-shortest",
                    ]
                else:
                    command += ["-an"]
                run_ffmpeg([*command, str(adjusted)])
                stitched = adjusted
            if section.target_bytes is not None:
                return compress(
                    stitched, section.duration, section.target_bytes, use_audio, section_work
                )
            return stitched

        stitched = render_section(plan.root)
        result = work / "result.mp4"
        run_ffmpeg(["-i", str(stitched), "-c", "copy", "-movflags", "+faststart", str(result)])
        os.replace(result, output)


def compress(
    stitched: Path,
    duration: Decimal,
    target_bytes: int,
    use_audio: bool,
    work: Path,
) -> Path:
    total_bitrate = int(Decimal(target_bytes) * 8 * Decimal("0.95") / duration)
    audio_bitrate = min(128_000, max(16_000, total_bitrate // 5)) if use_audio else 0
    video_bitrate = total_bitrate - audio_bitrate
    if video_bitrate < 5_000:
        raise ValueError("Compression target is too small for this video")
    print(f"Compressing to at most {target_bytes:,} bytes", file=sys.stderr)
    for attempt in range(1, 9):
        passlog = work / f"encode-pass-{attempt}"
        result = work / f"result-{attempt}.mp4"
        common = [
            "-i",
            str(stitched),
            "-map",
            "0:v:0",
            "-c:v",
            "libx264",
            "-b:v",
            str(video_bitrate),
        ]
        run_ffmpeg(
            [
                *common,
                "-pass",
                "1",
                "-passlogfile",
                str(passlog),
                "-an",
                "-f",
                "null",
                os.devnull,
            ]
        )
        second = [*common, "-pass", "2", "-passlogfile", str(passlog)]
        if use_audio:
            second += ["-map", "0:a:0", "-c:a", "aac", "-b:a", str(audio_bitrate)]
        else:
            second += ["-an"]
        run_ffmpeg([*second, "-movflags", "+faststart", str(result)])
        actual_bytes = result.stat().st_size
        if actual_bytes <= target_bytes:
            return result
        print(
            f"Attempt {attempt}: {actual_bytes:,} bytes; retrying with lower bitrates",
            file=sys.stderr,
        )
        scale = min(Decimal("0.85"), Decimal(target_bytes) / actual_bytes * Decimal("0.9"))
        next_video = max(5_000, int(video_bitrate * scale))
        next_audio = max(16_000, int(audio_bitrate * scale)) if use_audio else 0
        if (next_video, next_audio) == (video_bitrate, audio_bitrate):
            break
        video_bitrate, audio_bitrate = next_video, next_audio
    raise ValueError(f"Could not compress the video below {target_bytes:,} bytes")


@click.command(help="Open an edit list, then stitch clips with FFmpeg.")
@click.argument("clips", nargs=-1, type=click.Path(path_type=Path))
@click.option(
    "-s",
    "--save",
    type=click.Path(path_type=Path),
    help="Save path. Alternatively, add 'save PATH' in the editor.",
)
@click.option("--editor", help="Editor command. Defaults to VISUAL, EDITOR, then vi.")
@click.option("--fps", type=click.IntRange(min=1), help="Override the first clip's frame rate.")
def main(
    clips: tuple[Path, ...],
    save: Path | None,
    editor: str | None,
    fps: int | None,
) -> None:
    if save is not None and save.suffix.lower() != ".mp4":
        raise click.BadParameter("filename must end in .mp4", param_hint="--save")
    for executable in ("ffmpeg", "ffprobe"):
        if shutil.which(executable) is None:
            raise click.ClickException(f"{executable} is required and was not found in PATH")
    try:
        paths = [path.expanduser().resolve(strict=True) for path in clips]
        save_path = save.expanduser().absolute() if save is not None else None
        plan = parse_todo(edit_todo(paths, editor), Path.cwd(), save_path)
        render(plan, plan.save, fps)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        raise click.ClickException(str(error)) from error
    click.echo(f"Saved {plan.save}")


if __name__ == "__main__":
    main()
