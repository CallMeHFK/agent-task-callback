"""Build the QwenPaw plugin bundle that the GitHub release ships as an asset.

The archive carries exactly the files the installer copies into
``~/.qwenpaw/plugins/agent-task-callback/``, under one top-level directory
holding ``plugin.json`` -- the only shape QwenPaw's own installers accept (the
in-app one searches the archive root then a single top-level directory, the CLI
one rejects an archive with more than one).
"""

from __future__ import annotations

import argparse
import json
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"

# What the running plugin actually needs. tests/ and packaging/ stay out: they
# are development scaffolding, and a second top-level directory in the archive
# would make QwenPaw's CLI installer reject it.
INCLUDE = ("plugin.json", "backend", "README.md")

# A fixed stamp keeps the asset byte-identical across rebuilds of one commit, so
# a re-upload can be diffed against what is already attached to the release.
STAMP = (1980, 1, 1, 0, 0, 0)
SKIP_DIRS = frozenset({"__pycache__"})


class BundleError(RuntimeError):
    """The bundle cannot be shipped as-is."""


def bundle_files(plugin_id: str) -> list[tuple[Path, str]]:
    """Return (source path, archive name) pairs in archive order."""
    out: list[tuple[Path, str]] = []
    for name in INCLUDE:
        path = ROOT / name
        if path.is_file():
            out.append((path, f"{plugin_id}/{name}"))
            continue
        if not path.is_dir():
            raise BundleError(f"{name} is listed in INCLUDE but missing")
        for child in sorted(path.rglob("*")):
            if child.is_symlink():
                # extractall() does not restore symlinks: the member lands as a
                # plain file whose bytes are the link target, and the plugin
                # still claims to have installed.
                raise BundleError(f"refusing to ship symlink: {child}")
            relative = child.relative_to(ROOT)
            if child.is_file() and not SKIP_DIRS.intersection(relative.parts):
                out.append((child, f"{plugin_id}/{relative.as_posix()}"))
    if not out:
        raise BundleError(f"nothing to bundle under {ROOT}")
    return out


def build(out_dir: Path = DIST) -> Path:
    """Write the plugin ZIP and return its path."""
    manifest = json.loads((ROOT / "plugin.json").read_text(encoding="utf-8"))
    plugin_id = manifest.get("id")
    version = manifest.get("version")
    if not plugin_id or not version:
        raise BundleError("plugin.json needs both id and version")

    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / f"{plugin_id}-qwenpaw-plugin-{version}.zip"
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        for path, arcname in bundle_files(plugin_id):
            info = zipfile.ZipInfo(arcname, date_time=STAMP)
            info.external_attr = 0o644 << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, path.read_bytes())
    return target


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=DIST, help="directory to write the zip into")
    args = parser.parse_args()
    try:
        print(build(args.out))
    except BundleError as exc:
        print(f"build failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
