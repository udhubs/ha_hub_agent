"""Probe / ensure HA packages include for UDHUB managed YAML (PRD §5.4E)."""

from __future__ import annotations

import logging
import os
import re
import shutil
from typing import Any

from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

_INCLUDE_LINE_RE = re.compile(
    r"!include_dir_(?:named|merge_named)\s+(\S+)",
    re.IGNORECASE,
)
_HOMEASSISTANT_BLOCK_RE = re.compile(r"(?m)^homeassistant:\s*(?:#.*)?$")


def _config_paths(hass: HomeAssistant) -> tuple[str, str, str]:
    config_dir = os.path.realpath(hass.config.config_dir)
    return (
        config_dir,
        os.path.join(config_dir, "configuration.yaml"),
        os.path.join(config_dir, "packages"),
    )


def _strip_yaml_comment(line: str) -> str:
    """Remove YAML comments; ignore # inside single-quoted tokens (best-effort)."""
    in_single = False
    out: list[str] = []
    i = 0
    while i < len(line):
        ch = line[i]
        if ch == "'" and not in_single:
            in_single = True
            out.append(ch)
        elif ch == "'" and in_single:
            in_single = False
            out.append(ch)
        elif ch == "#" and not in_single:
            break
        else:
            out.append(ch)
        i += 1
    return "".join(out).rstrip()


def _ha_block_packages_targets(text: str) -> list[str]:
    """packages includes nested under a homeassistant: mapping (not root-level)."""
    targets: list[str] = []
    in_ha = False
    ha_indent = -1
    for raw in text.splitlines():
        active = _strip_yaml_comment(raw)
        if not active.strip():
            continue
        indent = len(active) - len(active.lstrip(" "))
        stripped = active.strip()
        if re.match(r"^homeassistant:\s*$", stripped) or re.match(
            r"^homeassistant:\s+\S", stripped
        ):
            in_ha = True
            ha_indent = indent
            # inline homeassistant: { ... } rare; still scan same line
            m_inline = _INCLUDE_LINE_RE.search(stripped)
            if m_inline:
                t = m_inline.group(1).strip().strip("'\"")
                if t.rstrip("/") == "packages" or t.startswith("packages"):
                    targets.append(t.rstrip("/"))
            continue
        if not in_ha:
            continue
        if indent <= ha_indent:
            in_ha = False
            ha_indent = -1
            # This line may start another top-level key; don't consume as HA child
            if re.match(r"^homeassistant:\s*$", stripped):
                in_ha = True
                ha_indent = indent
            continue
        m = _INCLUDE_LINE_RE.search(stripped)
        if not m:
            continue
        target = m.group(1).strip().strip("'\"")
        if target.rstrip("/") == "packages" or target.startswith("packages"):
            targets.append(target.rstrip("/"))
    return targets


def _root_level_packages_targets(text: str) -> list[str]:
    """False-positive style: packages include at YAML root (HA ignores for packages)."""
    targets: list[str] = []
    for raw in text.splitlines():
        active = _strip_yaml_comment(raw)
        if not active.strip():
            continue
        indent = len(active) - len(active.lstrip(" "))
        if indent != 0:
            continue
        m = _INCLUDE_LINE_RE.search(active)
        if not m:
            continue
        target = m.group(1).strip().strip("'\"")
        if target.rstrip("/") == "packages" or target.startswith("packages"):
            targets.append(target.rstrip("/"))
    return targets


def _include_loads_packages_root(targets: list[str]) -> bool:
    """True if flat packages/udhub_*.yaml would be loaded."""
    return any(t == "packages" for t in targets)


def _config_snippet_for_packages(text: str, limit: int = 20) -> list[str]:
    """Non-secret lines mentioning packages / homeassistant / scene for diagnostics."""
    lines: list[str] = []
    for i, raw in enumerate(text.splitlines(), 1):
        low = raw.lower()
        if (
            "package" in low
            or re.match(r"^\s*homeassistant\s*:", raw)
            or re.match(r"^\s*scene\s*:", raw)
            or re.match(r"^\s*automation\s*:", raw)
            or "include" in low
        ):
            lines.append(f"{i}:{raw.rstrip()}"[:160])
            if len(lines) >= limit:
                break
    return lines


def _list_udhub_files(packages_dir: str) -> list[str]:
    if not os.path.isdir(packages_dir):
        return []
    out: list[str] = []
    try:
        for name in sorted(os.listdir(packages_dir)):
            if name.startswith("udhub_") and name.endswith(".yaml"):
                out.append(name)
            elif name == "udhub" and os.path.isdir(os.path.join(packages_dir, name)):
                out.append("udhub/")
    except OSError:
        pass
    return out


def _read_text(path: str) -> str | None:
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except OSError:
        return None


def _inject_packages_include(text: str) -> tuple[str, str]:
    """Return (new_text, action) where action is none|injected|appended|replaced."""
    if _include_loads_packages_root(_ha_block_packages_targets(text)):
        return text, "none"

    # If root-level false packages: exist, comment them out and inject under HA
    root_targets = _root_level_packages_targets(text)
    if root_targets:
        lines = text.splitlines(keepends=True)
        new_lines: list[str] = []
        for raw in lines:
            active = _strip_yaml_comment(raw)
            indent = len(active) - len(active.lstrip(" ")) if active.strip() else 0
            m = _INCLUDE_LINE_RE.search(active) if active.strip() else None
            if indent == 0 and m:
                target = m.group(1).strip().strip("'\"")
                if target.rstrip("/") == "packages" or target.startswith("packages"):
                    new_lines.append(f"# UDHUB disabled root packages include: {raw.lstrip()}")
                    continue
            new_lines.append(raw)
        text = "".join(new_lines)

    # Replace an existing HA-nested packages include that does not load packages/ root
    ha_targets = _ha_block_packages_targets(text)
    if ha_targets and not _include_loads_packages_root(ha_targets):
        lines = text.splitlines(keepends=True)
        changed = False
        new_lines: list[str] = []
        for raw in lines:
            active = _strip_yaml_comment(raw)
            m = _INCLUDE_LINE_RE.search(active)
            if m:
                target = m.group(1).strip().strip("'\"")
                if target.startswith("packages"):
                    key_m = re.match(r"^(\s*packages\s*:)", active, re.IGNORECASE)
                    if key_m:
                        new_lines.append(
                            f"{key_m.group(1)} !include_dir_named packages\n"
                        )
                        changed = True
                        continue
            new_lines.append(raw)
        if changed:
            return "".join(new_lines), "replaced"

    if _include_loads_packages_root(_ha_block_packages_targets(text)):
        return text, "none"

    m = _HOMEASSISTANT_BLOCK_RE.search(text)
    if m:
        injection = "\n  packages: !include_dir_named packages"
        return text[: m.end()] + injection + text[m.end() :], "injected"
    addition = (
        "\n\n# UDHUB — enable packages for managed scenes/automations\n"
        "homeassistant:\n"
        "  packages: !include_dir_named packages\n"
    )
    return text + addition, "appended"


def _udhub_scene_states(hass: HomeAssistant) -> list[str]:
    ids: list[str] = []
    for state in hass.states.async_all("scene"):
        if state.entity_id.startswith("scene.udhub_"):
            ids.append(state.entity_id)
    return sorted(ids)


async def probe_packages(hass: HomeAssistant) -> dict[str, Any]:
    """Snapshot: whether configuration.yaml loads config/packages/."""
    config_dir, cfg_path, packages_dir = _config_paths(hass)
    text = _read_text(cfg_path)
    ha_targets = _ha_block_packages_targets(text or "")
    root_targets = _root_level_packages_targets(text or "")
    include_ok = _include_loads_packages_root(ha_targets)
    dir_exists = os.path.isdir(packages_dir)
    files = _list_udhub_files(packages_dir)
    all_entries: list[str] = []
    if dir_exists:
        try:
            for name in sorted(os.listdir(packages_dir)):
                path = os.path.join(packages_dir, name)
                if os.path.isdir(path):
                    all_entries.append(f"{name}/")
                else:
                    try:
                        sz = os.path.getsize(path)
                    except OSError:
                        sz = -1
                    all_entries.append(f"{name}:{sz}")
        except OSError:
            pass
    sample_file = None
    sample_head = None
    if files:
        for name in files:
            if name.endswith(".yaml"):
                sample_file = name
                sample_head = (_read_text(os.path.join(packages_dir, name)) or "")[:240]
                break
    scene_ids = sorted(s.entity_id for s in hass.states.async_all("scene"))[:40]
    check_errors: list[str] = []
    try:
        from homeassistant.helpers.check_config import async_check_ha_config_file

        check = await async_check_ha_config_file(hass)
        # CheckConfigError / dict-like across versions
        err = getattr(check, "errors", None) or getattr(check, "error", None)
        if err:
            if isinstance(err, (list, tuple)):
                check_errors = [str(x)[:200] for x in err[:8]]
            else:
                check_errors = [str(err)[:200]]
        # Some versions expose .components failures via str(check)
        if not check_errors and check is not None and not bool(check):
            check_errors = [str(check)[:300]]
    except Exception as exc:
        check_errors = [f"check_config_failed:{exc}"]

    if include_ok and dir_exists:
        status = "ready"
    elif root_targets and not include_ok:
        status = "include_root_level"
    elif ha_targets and not include_ok:
        status = "include_not_root"
    elif not include_ok:
        status = "include_missing"
    else:
        status = "dir_missing"
    return {
        "status": status,
        "include_ok": include_ok,
        "include_targets": ha_targets,
        "include_targets_root": root_targets,
        "packages_dir_exists": dir_exists,
        "packages_dir": "packages",
        "packages_dir_abs": packages_dir,
        "all_package_entries": all_entries[:40],
        "udhub_files": files,
        "udhub_scene_entities": _udhub_scene_states(hass),
        "scene_entities": scene_ids,
        "check_errors": check_errors,
        "sample_file": sample_file,
        "sample_head": sample_head,
        "config_snippets": _config_snippet_for_packages(text or ""),
        "config_path": "configuration.yaml",
        "config_readable": text is not None,
        "config_dir": os.path.basename(config_dir) or config_dir,
    }


async def ensure_packages(hass: HomeAssistant) -> dict[str, Any]:
    """Create packages/ and add include to configuration.yaml if missing.

    After patching configuration.yaml, calls homeassistant.reload_core_config.
    First-time enable usually works without full HA restart.
    """
    probe = await probe_packages(hass)
    _config_dir, cfg_path, packages_dir = _config_paths(hass)
    actions: list[str] = []

    if not probe.get("packages_dir_exists"):
        try:
            os.makedirs(packages_dir, exist_ok=True)
            actions.append("mkdir_packages")
        except OSError as exc:
            return {
                **probe,
                "status": "failed",
                "error": f"mkdir_failed:{exc}",
                "actions": actions,
            }

    patched = False
    if not probe.get("include_ok"):
        text = _read_text(cfg_path)
        if text is None:
            return {
                **probe,
                "status": "failed",
                "error": "configuration_yaml_unreadable",
                "actions": actions,
            }
        new_text, action = _inject_packages_include(text)
        if action != "none":
            bak = f"{cfg_path}.bak.udhub"
            try:
                if not os.path.exists(bak):
                    shutil.copy2(cfg_path, bak)
                    actions.append("backup_configuration_yaml")
                tmp = f"{cfg_path}.tmp.udhub"
                with open(tmp, "w", encoding="utf-8") as f:
                    f.write(new_text)
                os.replace(tmp, cfg_path)
                actions.append(f"packages_include_{action}")
                patched = True
            except OSError as exc:
                return {
                    **probe,
                    "status": "failed",
                    "error": f"write_failed:{exc}",
                    "actions": actions,
                }

    reload_error = None
    if patched or probe.get("status") != "ready":
        try:
            await hass.services.async_call(
                "homeassistant", "reload_core_config", {}, blocking=True
            )
            actions.append("reload_core_config")
        except Exception as exc:
            _LOGGER.warning("UDHUB packages reload_core_config failed: %s", exc)
            reload_error = str(exc)
            actions.append("reload_core_config_failed")

    final = await probe_packages(hass)
    if final.get("include_ok") and final.get("packages_dir_exists"):
        final["status"] = "ready"
    elif reload_error:
        final["status"] = "needs_restart"
        final["error"] = reload_error
        final["hint"] = (
            "packages include written; reload_core_config failed — "
            "HA restart may be required once"
        )
    else:
        final["status"] = final.get("status") or "failed"
        if final.get("status") == "include_root_level":
            final["hint"] = (
                "packages include is at YAML root; HA requires it under "
                "homeassistant: packages: !include_dir_named packages"
            )
        elif final.get("status") == "include_not_root":
            final["hint"] = (
                "configuration.yaml packages include does not load packages/ root "
                f"(targets={final.get('include_targets')}); need "
                "!include_dir_named packages for flat udhub_*.yaml"
            )
    final["actions"] = actions
    final["patched"] = patched
    return final
