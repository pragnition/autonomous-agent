"""Config bundles for sandbox runners (spec v2 §5): the system prompt, the core plugin's skills, the effortless mod
(picks each prompt's reasoning effort) and, for project sandboxes, the pack's plugin. Deterministic, so the version (a
hash of the contents) changes only when they do."""

from __future__ import annotations

import gzip
import hashlib
import io
import tarfile
from pathlib import Path

from agent.core.payloads import DEFAULT_SYSTEM

CORE_PLUGIN = Path(__file__).parent.parent / "plugins" / "core"
EFFORTLESS_PLUGIN = Path(__file__).parent.parent / "plugins" / "effortless"
PACK_PROFILES = ("code", "debug")


def _files(config_dir: Path, profile: str, project: str | None) -> list[tuple[str, Path]]:
    custom = config_dir / "agent" / "system.md"
    files = [("system.md", custom if custom.is_file() else DEFAULT_SYSTEM)]
    for name, root in (("core", CORE_PLUGIN), ("effortless", EFFORTLESS_PLUGIN)):
        files += [(f"plugins/{name}/{p.relative_to(root)}", p) for p in sorted(root.rglob("*")) if p.is_file()]
    pack_plugin = config_dir / "packs" / (project or "") / "plugin"
    if profile in PACK_PROFILES and project and pack_plugin.is_dir():
        files += [(f"plugins/pack/{p.relative_to(pack_plugin)}", p) for p in sorted(pack_plugin.rglob("*"))
                  if p.is_file()]
    return files


def build_bundle(config_dir: Path, profile: str, project: str | None) -> tuple[str, bytes]:
    digest, raw = hashlib.sha256(), io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for name, path in _files(Path(config_dir), profile, project):
            data = path.read_bytes()
            digest.update(name.encode() + b"\0" + data + b"\0")
            info = tarfile.TarInfo(name)
            info.size, info.mtime, info.mode = len(data), 0, 0o644
            tar.addfile(info, io.BytesIO(data))
    return digest.hexdigest()[:16], gzip.compress(raw.getvalue(), mtime=0)
