"""Config path policy for Agent config_write (PRD §5.4E / §5.4G)."""

from __future__ import annotations

import os

# Default: cloud-managed namespace only (backward compatible).
WRITE_SCOPE_UDHUB = "udhub"
# Extended: any YAML under config/packages/.
WRITE_SCOPE_PACKAGES = "packages"
# Extended+: any YAML under config/ except denylist (secrets, .storage, …).
WRITE_SCOPE_CONFIG = "config"

_CONFIG_DENY_BASENAMES = frozenset({"secrets.yaml"})
_CONFIG_DENY_PREFIXES = (".storage/", "deps/", "www/")


def _normalize_relative_path(relative: str) -> str:
    """Normalize without stripping leading dots from `.storage/` etc."""
    rel = relative.replace("\\", "/")
    if rel.startswith("./"):
        rel = rel[2:]
    return rel.lstrip("/")


def is_denied_config_path(relative: str) -> bool:
    """Return True if `relative` must never be read/written under config_dir."""
    rel = _normalize_relative_path(relative)
    if not rel or rel == ".":
        return False
    if ".." in rel.split("/"):
        return True
    base = os.path.basename(rel).lower()
    if base in _CONFIG_DENY_BASENAMES:
        return True
    lower = rel.lower()
    for prefix in _CONFIG_DENY_PREFIXES:
        bare = prefix.rstrip("/")
        if lower == bare or lower.startswith(prefix) or f"/{prefix}" in lower:
            return True
    return False


def is_denied_config_yaml_path(relative: str) -> bool:
    return is_denied_config_path(relative)


def is_allowed_file_relative_path(
    relative: str, write_scope: str = WRITE_SCOPE_UDHUB, *, write: bool = False
) -> bool:
    """Path policy for raw file_* ops (PRD §5.4G · Agent 0.3.1+)."""
    rel = _normalize_relative_path(relative)
    if is_denied_config_path(rel):
        return False
    if not write:
        return True
    if write_scope == WRITE_SCOPE_CONFIG:
        return True
    if write_scope == WRITE_SCOPE_PACKAGES:
        return rel.startswith("packages/") and rel.count("/") >= 1
    return rel.startswith("packages/udhub/") or (
        rel.startswith("packages/udhub_") and rel.count("/") == 1
    )


def is_listable_config_relative_path(relative: str) -> bool:
    rel = _normalize_relative_path(relative) or "."
    if rel == ".":
        return True
    return not is_denied_config_path(rel)


def filter_visible_config_entries(dir_relative: str, names: list[str]) -> list[str]:
    base = _normalize_relative_path(dir_relative) or "."
    visible: list[str] = []
    for name in names:
        child = name if base in ("", ".") else f"{base}/{name}"
        if not is_denied_config_path(child):
            visible.append(name)
    return visible


def is_allowed_config_relative_path(
    relative: str, write_scope: str = WRITE_SCOPE_UDHUB
) -> bool:
    """Return True if `relative` is a safe YAML path under config_dir."""
    rel = _normalize_relative_path(relative)
    if not rel.endswith(".yaml") or is_denied_config_path(rel):
        return False
    if write_scope == WRITE_SCOPE_CONFIG:
        return True
    if write_scope == WRITE_SCOPE_PACKAGES:
        return rel.startswith("packages/") and rel.count("/") >= 1
    return rel.startswith("packages/udhub/") or (
        rel.startswith("packages/udhub_") and rel.count("/") == 1
    )


def resolve_config_target(config_dir: str, path: str) -> tuple[str | None, str | None]:
    """Resolve absolute path and relative path; None if outside config_dir."""
    candidate = os.path.realpath(os.path.join(config_dir, path))
    root = os.path.realpath(config_dir)
    if not candidate.startswith(root + os.sep) and candidate != root:
        return None, None
    relative = os.path.relpath(candidate, root)
    return candidate, relative
