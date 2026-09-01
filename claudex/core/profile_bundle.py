"""Pack and unpack a claudex profile as a portable tar.gz bundle.

The bundle includes all config files needed to reconstruct the profile on
another machine, but intentionally excludes session history (projects/).

Files included:
  - profile.toml
  - settings.json
  - mcp_servers.json
  - CLAUDE.md
  - commands/  (full tree)
  - skills/    (full tree)
  - .credentials.json  (OAuth / API key tokens — encrypted in the outer bundle)
  - .claude.json       (Claude Code state with oauthAccount)

Files excluded:
  - projects/          (session JSONL history — can be large, not needed for profile portability)
  - Any __pycache__ or *.pyc artefacts
"""

from __future__ import annotations

import io
import os
import tarfile
from pathlib import Path
from typing import Set

# Hard cap on the decompressed size of an imported bundle (defends against
# zip/tar bombs). Profile config is small; 200 MB is far above any real bundle.
_MAX_TOTAL_BYTES = 200 * 1024 * 1024

# Files that must never be world/group-readable once extracted.
_SECRET_NAMES: Set[str] = {".credentials.json", "credentials.json", ".claude.json"}

# Top-level names to include (files) and directories to include recursively
_INCLUDE_FILES: Set[str] = {
    "profile.toml",
    "settings.json",
    "mcp_servers.json",
    "CLAUDE.md",
    ".credentials.json",
    ".claude.json",
}

_INCLUDE_DIRS: Set[str] = {
    "commands",
    "skills",
}

# Names to skip when recursing into included directories
_SKIP_NAMES: Set[str] = {"__pycache__", ".DS_Store"}


def export_bundle(profile_config_dir: Path) -> bytes:
    """Create an in-memory tar.gz bundle of the profile config (no session history).

    Args:
        profile_config_dir: The ``CLAUDE_CONFIG_DIR`` of the profile
                            (e.g. ``~/.claudex/profiles/work``).

    Returns:
        Raw bytes of a gzip-compressed tar archive.
    """
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name in _INCLUDE_FILES:
            path = profile_config_dir / name
            if path.exists() and path.is_file():
                tar.add(path, arcname=name)

        for dirname in _INCLUDE_DIRS:
            dir_path = profile_config_dir / dirname
            if dir_path.exists() and dir_path.is_dir():
                _add_dir(tar, dir_path, arcname=dirname)

    return buf.getvalue()


def import_bundle(bundle_bytes: bytes, target_config_dir: Path) -> None:
    """Extract a bundle into *target_config_dir*, creating it if needed.

    Existing files are overwritten. Only paths that pass a safety check
    (no absolute paths, no ``..`` components) are extracted.

    Args:
        bundle_bytes: Raw bytes as returned by :func:`export_bundle` (or
                      the decrypted payload from a share).
        target_config_dir: Destination directory (profile's config dir).
    """
    target_config_dir.mkdir(parents=True, exist_ok=True)
    buf = io.BytesIO(bundle_bytes)
    with tarfile.open(fileobj=buf, mode="r:gz") as tar:
        safe_extract(tar, target_config_dir)


def safe_extract(tar: tarfile.TarFile, target_dir: Path) -> None:
    """Extract *tar* into *target_dir*, rejecting anything unsafe.

    Bundles are untrusted (they may be pulled from a sharing server), so this
    guards against the CVE-2007-4559 tar path-traversal class:

      * absolute paths and ``..`` components are skipped;
      * symlinks, hardlinks, devices, FIFOs are skipped (only regular files and
        directories are extracted) — a symlink member could otherwise redirect a
        later member outside the target tree;
      * the resolved destination is verified to stay within *target_dir*;
      * the total decompressed size is capped to defuse decompression bombs;
      * extracted credential files are locked down to mode 0600.
    """
    target_dir = target_dir.resolve()
    total = 0
    for member in tar.getmembers():
        member_path = Path(member.name)
        if member_path.is_absolute() or ".." in member_path.parts:
            continue
        # Only regular files and directories — never links/devices.
        if not (member.isreg() or member.isdir()):
            continue
        dest = (target_dir / member.name).resolve()
        if dest != target_dir and target_dir not in dest.parents:
            continue
        if member.isreg():
            total += member.size
            if total > _MAX_TOTAL_BYTES:
                raise ValueError("Bundle exceeds maximum allowed size (possible tar bomb)")
        tar.extract(member, path=target_dir)
        if member.isreg() and Path(member.name).name in _SECRET_NAMES:
            try:
                os.chmod(dest, 0o600)
            except OSError:
                pass


def _add_dir(tar: tarfile.TarFile, dir_path: Path, arcname: str) -> None:
    """Recursively add a directory to the tar, skipping unwanted entries."""
    tar.add(
        dir_path,
        arcname=arcname,
        filter=lambda info: None if Path(info.name).name in _SKIP_NAMES else info,
    )
