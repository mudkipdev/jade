from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from fractions import Fraction
from pathlib import Path


@dataclass(frozen=True)
class Clip:
    path: Path
    duration: Decimal
    width: int
    height: int
    fps: Fraction
    has_audio: bool


@dataclass(frozen=True)
class TrimmedClip:
    clip: Clip
    start: Decimal | None = None
    end: Decimal | None = None

    @property
    def duration(self) -> Decimal:
        return (
            self.clip.duration if self.start is None or self.end is None else self.end - self.start
        )


@dataclass(frozen=True)
class AudioClip:
    path: Path
    duration: Decimal


@dataclass(frozen=True)
class TrimmedAudio:
    clip: AudioClip
    start: Decimal | None = None
    end: Decimal | None = None

    @property
    def duration(self) -> Decimal:
        return (
            self.clip.duration if self.start is None or self.end is None else self.end - self.start
        )


@dataclass
class AudioSection:
    clips: list[TrimmedAudio | AudioSection]
    speed: Decimal = Decimal(1)
    volume: Decimal = Decimal(1)
    left_trim: Decimal = Decimal(0)
    right_trim: Decimal = Decimal(0)

    @property
    def untrimmed_duration(self) -> Decimal:
        return sum((item.duration for item in self.clips), Decimal(0)) / self.speed

    @property
    def duration(self) -> Decimal:
        return self.untrimmed_duration - self.left_trim - self.right_trim


@dataclass(frozen=True)
class AudioOverlay:
    section: AudioSection
    start: Decimal = Decimal(0)
    end: Decimal | None = None


@dataclass(frozen=True)
class TrimmedSection:
    section: Section
    start: Decimal
    end: Decimal

    @property
    def duration(self) -> Decimal:
        return self.end - self.start


@dataclass
class Section:
    clips: list[TrimmedClip | TrimmedSection | Section]
    overlays: list[AudioOverlay] = field(default_factory=list)
    speed: Decimal = Decimal(1)
    volume: Decimal = Decimal(1)
    compression_target: int | None = None
    left_trim: Decimal = Decimal(0)
    right_trim: Decimal = Decimal(0)

    @property
    def untrimmed_duration(self) -> Decimal:
        return sum((item.duration for item in self.clips), Decimal(0)) / self.speed

    @property
    def duration(self) -> Decimal:
        return self.untrimmed_duration - self.left_trim - self.right_trim


@dataclass(frozen=True)
class Recipe:
    root_section: Section
    output_path: Path
