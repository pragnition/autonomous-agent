"""Config bundles for the runner: the core plugin's skills, the effortless plugin (picks each prompt's reasoning
effort) and every pack's plugin — every session gets every pack's skills. Deterministic, so the version (a hash of
the contents) changes only when they do."""

from __future__ import annotations

import gzip
import hashlib
import io
import tarfile
from pathlib import Path

CORE_PLUGIN = Path(__file__).parent.parent / "plugins" / "core"
EFFORTLESS_PLUGIN = Path(__file__).parent.parent / "plugins" / "effortless"


def _files(config_dir: Path) -> list[tuple[str, Path]]:
    files = []
    for name, root in (("core", CORE_PLUGIN), ("effortless", EFFORTLESS_PLUGIN)):
        files += [(f"plugins/{name}/{p.relative_to(root)}", p) for p in sorted(root.rglob("*")) if p.is_file()]
    packs_dir = config_dir / "packs"
    for pack_plugin in sorted(packs_dir.glob("*/plugin")) if packs_dir.is_dir() else []:
        if pack_plugin.is_dir():
            name = pack_plugin.parent.name
            files += [(f"plugins/packs/{name}/{p.relative_to(pack_plugin)}", p)
                      for p in sorted(pack_plugin.rglob("*")) if p.is_file()]
    return files


def build_bundle(config_dir: Path) -> tuple[str, bytes]:
    digest, raw = hashlib.sha256(), io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for name, path in _files(Path(config_dir)):
            data = path.read_bytes()
            digest.update(name.encode() + b"\0" + data + b"\0")
            info = tarfile.TarInfo(name)
            info.size, info.mtime, info.mode = len(data), 0, 0o644
            tar.addfile(info, io.BytesIO(data))
    return digest.hexdigest()[:16], gzip.compress(raw.getvalue(), mtime=0)
