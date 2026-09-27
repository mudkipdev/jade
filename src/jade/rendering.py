from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path

from .recipe import Recipe, Section, TrimmedClip


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


def iter_trimmed_clips(section: Section) -> Iterator[TrimmedClip]:
    for item in section.clips:
        if isinstance(item, TrimmedClip):
            yield item
        else:
            yield from iter_trimmed_clips(item)


class Renderer:
    def __init__(self, recipe: Recipe) -> None:
        self.recipe = recipe
        trimmed_clips = list(iter_trimmed_clips(recipe.root_section))
        first = trimmed_clips[0].clip
        width = first.width + first.width % 2
        height = first.height + first.height % 2

        self.output_fps = str(first.fps)
        self.use_audio = any(trimmed_clip.clip.has_audio for trimmed_clip in trimmed_clips)
        self.video_filter = (
            f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,"
            f"fps={self.output_fps},format=yuv420p"
        )

        self.total_clips = len(trimmed_clips)
        self.rendered_count = 0
        self.section_count = 0

    def render(self) -> None:
        output = self.recipe.output_path

        if not output.parent.is_dir():
            raise ValueError(f"Output directory does not exist: {output.parent}")

        with tempfile.TemporaryDirectory(prefix="jade-", dir=output.parent) as directory:
            work = Path(directory)
            stitched = self.render_section(self.recipe.root_section, work)
            result = work / "result.mp4"
            run_ffmpeg(["-i", str(stitched), "-c", "copy", "-movflags", "+faststart", str(result)])
            os.replace(result, output)

    def render_clip(self, trimmed_clip: TrimmedClip, segment: Path) -> None:
        clip = trimmed_clip.clip
        command = []

        if trimmed_clip.start is not None and trimmed_clip.end is not None:
            command += [
                "-ss",
                str(trimmed_clip.start),
                "-t",
                str(trimmed_clip.end - trimmed_clip.start),
            ]

        command += ["-i", str(clip.path)]

        if self.use_audio and not clip.has_audio:
            command += ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo"]

        command += [
            "-map",
            "0:v:0",
            "-vf",
            self.video_filter,
            "-c:v",
            "libx264",
            "-preset",
            "medium",
            "-crf",
            "20",
            "-pix_fmt",
            "yuv420p",
            "-r",
            self.output_fps,
        ]

        if self.use_audio:
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
        self.rendered_count += 1

        print(
            f"Rendering {self.rendered_count}/{self.total_clips}: {clip.path.name}",
            file=sys.stderr,
        )

        run_ffmpeg(command)

    def render_section(self, section: Section, work: Path) -> Path:
        self.section_count += 1
        section_work = work / f"section_{self.section_count:04d}"
        section_work.mkdir()
        entries = []

        for index, item in enumerate(section.clips):
            if isinstance(item, TrimmedClip):
                segment = section_work / f"segment_{index:04d}.mp4"
                self.render_clip(item, segment)
            else:
                segment = self.render_section(item, work)
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

        if section.speed != 1 or (self.use_audio and section.volume != 1):
            stitched = self.adjust_section(section, stitched, section_work)

        if section.compression_target is not None:
            return compress(
                stitched, section.duration, section.compression_target, self.use_audio, section_work
            )

        return stitched

    def adjust_section(self, section: Section, stitched: Path, section_work: Path) -> Path:
        adjusted = section_work / "adjusted.mp4"
        command = ["-i", str(stitched), "-map", "0:v:0"]

        if section.speed != 1:
            command += [
                "-vf",
                f"setpts=(PTS-STARTPTS)/{section.speed},fps={self.output_fps},format=yuv420p",
                "-c:v",
                "libx264",
                "-preset",
                "medium",
                "-crf",
                "20",
                "-r",
                self.output_fps,
            ]
        else:
            command += ["-c:v", "copy"]

        if self.use_audio:
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
        return adjusted


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

    print(f"Compressing to {target_bytes:,} bytes", file=sys.stderr)

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
            f"Attempt {attempt} was {actual_bytes:,} bytes; retrying with lower bitrates",
            file=sys.stderr,
        )

        scale = min(Decimal("0.85"), Decimal(target_bytes) / actual_bytes * Decimal("0.9"))
        next_video = max(5_000, int(video_bitrate * scale))
        next_audio = max(16_000, int(audio_bitrate * scale)) if use_audio else 0

        if (next_video, next_audio) == (video_bitrate, audio_bitrate):
            break

        video_bitrate, audio_bitrate = next_video, next_audio

    raise ValueError(f"Could not compress the video below {target_bytes:,} bytes")
