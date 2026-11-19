"""The agent's home folder and browsers live on the work volume, so a rebuilt container loses nothing it installed.

The image makes `/home/agent` a link to `<root>/home` and `/opt/ms-playwright` a link to `<root>/ms-playwright`
(see the Dockerfile), so `pip --user`, `uv tool`, `npm -g` (its prefix is `~/.local`), `bun add -g`, dotfile edits and
`playwright install` all land on the volume. `prepare` runs as the runner starts:

- a volume with no home yet gets one from `/etc/skel`, with its `.claude` link to `<root>/claude`; a home that exists
  is never touched beyond putting that link back;
- each browser the image ships (`/opt/ms-playwright-image`) that the volume doesn't have yet is copied in, so a newer
  image adds its browser without touching the ones the agent installed. A browser is copied under a temporary name
  and renamed when complete, so a copy that died halfway is redone, never taken for a whole browser.

A failure is logged and the runner starts anyway: a missing browser or dotfile is better than no agent.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

log = logging.getLogger(__name__)

_COPYING = ".copying-"


def prepare(root: Path, *, skel: Path = Path("/etc/skel"),
            browsers: Path = Path("/opt/ms-playwright-image")) -> None:
    try:
        _home(root / "home", skel, root / "claude")
    except OSError:
        log.exception("couldn't prepare the agent's home folder at %s", root / "home")
    try:
        _browsers(browsers, root / "ms-playwright")
    except OSError:
        log.exception("couldn't copy the image's browsers to %s", root / "ms-playwright")


def _home(home: Path, skel: Path, claude: Path) -> None:
    if not home.exists():
        if skel.is_dir():
            shutil.copytree(skel, home, symlinks=True)
        else:
            home.mkdir()
    link = home / ".claude"
    if not link.is_symlink() and not link.exists():
        link.symlink_to(claude)


def _browsers(source: Path, dest: Path) -> None:
    dest.mkdir(exist_ok=True)
    for leftover in dest.glob(_COPYING + "*"):
        shutil.rmtree(leftover, ignore_errors=True)
    if not source.is_dir():
        return
    for entry in sorted(source.iterdir()):
        target = dest / entry.name
        if entry.name == ".links" and entry.is_dir():
            target.mkdir(exist_ok=True)
            for link in entry.iterdir():
                if not (target / link.name).exists():
                    shutil.copy2(link, target / link.name)
        elif target.exists() or target.is_symlink():
            continue
        elif entry.is_dir():
            partial = dest / (_COPYING + entry.name)
            shutil.copytree(entry, partial, symlinks=True)
            partial.rename(target)
        else:
            shutil.copy2(entry, target)
