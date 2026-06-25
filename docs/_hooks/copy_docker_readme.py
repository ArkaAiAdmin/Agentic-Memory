"""mkdocs build hook — copy docker/README.md into docs/.

The nav in mkdocs.yml references `docker/README.md`, but mkdocs
expects docs under `docs/`. This hook symlinks (or copies) the
file so the nav link resolves.

Symlink preferred — keeps the source as the single source of
truth, so edits to docker/README.md don't need a second step.
"""

from pathlib import Path
import shutil

DEST = Path("docs/docker_README.md")
SOURCE = Path("docker/README.md")


def on_pre_build(config, **kwargs) -> None:
    if not SOURCE.exists():
        return
    if DEST.exists() or DEST.is_symlink():
        DEST.unlink()
    try:
        DEST.symlink_to(SOURCE.resolve())
    except OSError:
        shutil.copy(SOURCE, DEST)


def on_post_build(config, **kwargs) -> None:
    if DEST.is_symlink():
        DEST.unlink()
    elif DEST.exists():
        DEST.unlink()
