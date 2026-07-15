"""Plane A guard: code/file-content reads happen ONLY under capture_code_from.

This is an allowlist, not a blocklist — anything not explicitly listed is
unreadable by construction, so company code can't be captured by
misconfiguration. Matching follows the firewall's rules: component-wise,
case-insensitive, and on RESOLVED paths, so a symlink or junction placed
inside an allowlisted tree that points outside it does NOT widen the
allowlist. Anything unresolvable fails closed.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from .filter import _path_parts

log = logging.getLogger("exo.allowlist")


class AllowlistViolation(Exception):
    pass


def device_code_paths(cfg) -> list[str]:
    """THIS device's Plane A allowlist. `capture_code_from` is either the
    legacy flat list (applies to this machine) or a dict keyed by
    device_name — absolute paths from one machine are meaningless or
    dangerous on another. A device with no entry gets NOTHING (fail closed)."""
    raw = cfg.get("capture_code_from")
    if isinstance(raw, dict):
        return [str(p) for p in raw.get(cfg.device_name) or []]
    return [str(p) for p in raw or []]


def resolved_roots(cfg) -> list[tuple[str, ...]]:
    roots = []
    for raw in device_code_paths(cfg):
        p = Path(os.path.expanduser(str(raw)))
        try:
            resolved = p.resolve(strict=True)
        except OSError:
            log.warning("capture_code_from entry missing, skipping: %s", raw)
            continue
        if not resolved.is_dir():
            log.warning("capture_code_from entry is not a directory, skipping: %s", raw)
            continue
        roots.append(_path_parts(resolved))
    return roots


def is_allowed(path: str | Path, roots: list[tuple[str, ...]]) -> bool:
    """True only if the RESOLVED path sits under a resolved allowlist root.
    Any resolution failure or malformed path -> False (fail closed)."""
    if not roots:
        return False
    try:
        raw = str(path)
        if "\x00" in raw:
            return False
        p = Path(os.path.expanduser(raw))
        if not p.is_absolute():
            return False
        parts = _path_parts(p.resolve(strict=False))
    except Exception:
        return False
    return any(parts[: len(root)] == root for root in roots)


def assert_allowed(path: str | Path, roots: list[tuple[str, ...]]) -> Path:
    if not is_allowed(path, roots):
        raise AllowlistViolation(f"refusing to read outside capture_code_from: {path}")
    return Path(os.path.expanduser(str(path))).resolve(strict=False)


def read_text_allowed(path: str | Path, cfg, max_bytes: int = 200_000) -> str:
    """THE one function through which Plane A file contents may be read."""
    p = assert_allowed(path, resolved_roots(cfg))
    return p.read_bytes()[:max_bytes].decode("utf-8", "replace")
