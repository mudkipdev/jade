from __future__ import annotations

import json
import shutil
from pathlib import Path

import click

from .commands import RecipeBuilder
from .parsing import edit_recipe, parse_recipe
from .rendering import Renderer


@click.command(
    help="Open a recipe, then stitch clips with FFmpeg.",
    add_help_option=False,
    options_metavar="",
)
@click.argument("clips", nargs=-1, type=click.Path(path_type=Path))
def main(clips: tuple[Path, ...]) -> None:
    for executable in ("ffmpeg", "ffprobe"):
        if shutil.which(executable) is None:
            raise click.ClickException(f"{executable} is required and was not found in PATH")

    try:
        paths = [path.expanduser().resolve(strict=True) for path in clips]
        recipe_text = edit_recipe(paths)
        sections, commands = parse_recipe(recipe_text)
        recipe = RecipeBuilder(sections, Path.cwd()).build(commands)
        Renderer(recipe).render()
    except (OSError, ValueError, json.JSONDecodeError) as error:
        raise click.ClickException(str(error)) from error

    click.echo(f"Saved {recipe.output_path}")


if __name__ == "__main__":
    main()
