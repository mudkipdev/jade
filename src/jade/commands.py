from __future__ import annotations

import re
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path

from .parsing import Command, parse_factor, parse_size, parse_time, probe_clip
from .recipe import Clip, Recipe, Section, TrimmedClip


class RecipeBuilder:
    def __init__(
        self,
        sections: dict[str, list[Command]],
        base_directory: Path,
    ) -> None:
        self.sections = sections
        self.base_directory = base_directory
        self.output_path: Path | None = None
        self.save_seen = False
        self.clip_cache: dict[Path, Clip] = {}
        self.handlers: dict[str, Callable[[list[str], Section, tuple[str, ...]], None]] = {
            "save": self.save,
            "add": self.add,
            "speed": self.speed,
            "volume": self.volume,
            "mute": self.mute,
            "compress": self.compress,
            "trim": self.trim,
        }

    def build(self, commands: list[Command]) -> Recipe:
        root = self.build_section(commands)

        if not root.clips:
            raise ValueError("The recipe contains no clips to export")

        if self.output_path is None:
            raise ValueError("Add 'save PATH' to the recipe")

        return Recipe(root, self.output_path)

    def build_section(self, commands: list[Command], stack: tuple[str, ...] = ()) -> Section:
        section = Section([])
        trim_line = None

        for line_number, words in commands:
            try:
                name, arguments = words[0], words[1:]
                handler = self.handlers.get(name)

                if handler is None:
                    raise ValueError(f"unknown command {name!r}")

                handler(arguments, section, stack)

                if name == "trim":
                    trim_line = line_number
            except (OSError, ValueError) as error:
                if re.match(r"^Line \d+: ", str(error)):
                    raise

                raise ValueError(f"Line {line_number}: {error}") from error

        if section.clips and section.duration <= 0:
            raise ValueError(f"Line {trim_line}: trim removes the entire section")

        return section

    def clip_for(self, reference: str) -> Clip:
        path = Path(reference).expanduser()

        if not path.is_absolute():
            path = self.base_directory / path

        path = path.resolve(strict=True)

        if path not in self.clip_cache:
            self.clip_cache[path] = probe_clip(path)

        return self.clip_cache[path]

    def make_trimmed_clip(self, reference: str, times: list[str]) -> TrimmedClip:
        clip = self.clip_for(reference)

        if times:
            start, end = parse_time(times[0]), parse_time(times[1])

            if start >= end or end > clip.duration:
                raise ValueError(f"trim must satisfy 0 <= START < END <= {clip.duration} seconds")

            return TrimmedClip(clip, start, end)

        return TrimmedClip(clip)

    def save(self, arguments: list[str], _section: Section, stack: tuple[str, ...]) -> None:
        if stack:
            raise ValueError("save is only allowed outside sections")

        if len(arguments) != 1:
            raise ValueError("expected 'save PATH'")

        if self.save_seen:
            raise ValueError("only one save command is allowed")

        self.save_seen = True
        output_path = Path(arguments[0]).expanduser()

        if not output_path.is_absolute():
            output_path = self.base_directory / output_path

        self.output_path = output_path.absolute()

        if self.output_path.suffix.lower() != ".mp4":
            raise ValueError("save filename must end in .mp4")

    def add(self, arguments: list[str], section: Section, stack: tuple[str, ...]) -> None:
        if len(arguments) == 1 and arguments[0] in self.sections:
            name = arguments[0]

            if name in stack:
                raise ValueError(f"recursive section reference: {' -> '.join((*stack, name))}")

            child = self.build_section(self.sections[name], (*stack, name))

            if not child.clips:
                raise ValueError(f"section {name!r} contains no clips")

            section.clips.append(child)
        elif len(arguments) in (1, 3):
            reference, *times = arguments

            if reference in self.sections:
                raise ValueError("a section cannot be trimmed; trim clips inside it")

            section.clips.append(self.make_trimmed_clip(reference, times))
        else:
            raise ValueError("expected 'add PATH [START END]' or 'add SECTION'")

    def speed(self, arguments: list[str], section: Section, _stack: tuple[str, ...]) -> None:
        if len(arguments) != 1:
            raise ValueError("expected 'speed VALUE'")

        section.speed = parse_factor(arguments[0], "speed")

    def volume(self, arguments: list[str], section: Section, _stack: tuple[str, ...]) -> None:
        if len(arguments) != 1:
            raise ValueError("expected 'volume VALUE'")

        section.volume = parse_factor(arguments[0], "volume")

    def mute(self, arguments: list[str], section: Section, _stack: tuple[str, ...]) -> None:
        if arguments:
            raise ValueError("expected 'mute'")

        section.volume = Decimal(0)

    def compress(self, arguments: list[str], section: Section, _stack: tuple[str, ...]) -> None:
        if len(arguments) != 1:
            raise ValueError("expected 'compress SIZE', such as 'compress 20mb'")

        if section.compression_target is not None:
            raise ValueError("only one compress command is allowed per section")

        section.compression_target = parse_size(arguments[0])

    def trim(self, arguments: list[str], section: Section, _stack: tuple[str, ...]) -> None:
        if len(arguments) != 2 or arguments[0] not in ("left", "right"):
            raise ValueError("expected 'trim left|right DURATION', such as 'trim right 1m'")

        duration = parse_time(arguments[1])

        if duration <= 0:
            raise ValueError("trim duration must be positive")

        if arguments[0] == "left":
            section.left_trim += duration
        else:
            section.right_trim += duration
