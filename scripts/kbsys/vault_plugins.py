"""Install researcher-approved Obsidian community plugins into a vault (D3.49).

Only plugins listed in config/obsidian_plugins.yaml, at the pinned version, with matching
SHA-256 hashes. Release files are cached in runtime/obsidian_plugins/<id>/<version>/;
`download` fetches them from the plugin's GitHub release and refuses any hash mismatch.
Installing never replaces a researcher's existing plugin settings (data.json) and only
adds the plugin id to community-plugins.json.
"""
import hashlib
import json
import sys
import urllib.request
from pathlib import Path

import yaml

from . import config

CONFIG = config.CONFIG_DIR / "obsidian_plugins.yaml"


def cache_dir():
    return config.ROOT / "runtime" / "obsidian_plugins"


def approved():
    return (yaml.safe_load(CONFIG.read_text(encoding="utf-8")) or {}).get("plugins") or {}


def _sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _release_dir(plugin_id, spec):
    return cache_dir() / plugin_id / spec["version"]


def verify(plugin_id, spec):
    """Return list of problems with the cached release files (empty = ok)."""
    folder = _release_dir(plugin_id, spec)
    problems = []
    for name, digest in spec["files"].items():
        path = folder / name
        if not path.is_file():
            problems.append(f"{plugin_id}: missing {name}")
        elif _sha(path) != digest:
            problems.append(f"{plugin_id}: SHA-256 mismatch for {name}")
    return problems


def download(plugin_id, spec):
    folder = _release_dir(plugin_id, spec)
    folder.mkdir(parents=True, exist_ok=True)
    for name, digest in spec["files"].items():
        url = f"https://github.com/{spec['repo']}/releases/download/{spec['version']}/{name}"
        data = urllib.request.urlopen(url, timeout=60).read()
        if hashlib.sha256(data).hexdigest() != digest:
            raise ValueError(f"{plugin_id} {name}: downloaded file does not match the approved SHA-256; not saved")
        (folder / name).write_bytes(data)


def _shape_queries(shapes):
    return {shape: {"combinationLogic": "OR",
                    "rules": [{"source": "property", "property": "type", "value": t, "logic": "is"} for t in types]}
            for shape, types in (shapes or {}).items()}


def plugin_settings(spec):
    settings = dict(spec.get("default_settings") or {})
    if "shapes" in settings:
        settings["shapeQueries"] = _shape_queries(settings.pop("shapes"))
    return settings


def _read_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else default
    except ValueError:
        return None


def install(vault_dir, plugins=None, reset_settings=False):
    """Copy verified files into <vault>/.obsidian/plugins/<id>/ and enable them. Returns a report."""
    vault_dir = Path(vault_dir)
    specs = approved()
    report = {"vault": str(vault_dir), "installed": [], "settings_written": [], "settings_kept": [],
              "settings_reset": [], "problems": []}
    for plugin_id in plugins or specs:
        if plugin_id not in specs:
            report["problems"].append(f"{plugin_id}: not approved in {CONFIG.name}")
            continue
        spec = specs[plugin_id]
        problems = verify(plugin_id, spec)
        if problems:
            report["problems"] += problems + [f"{plugin_id}: run `python -m kbsys.vault_plugins download` first"]
            continue
        target = vault_dir / ".obsidian" / "plugins" / plugin_id
        target.mkdir(parents=True, exist_ok=True)
        for name in spec["files"]:
            data = (_release_dir(plugin_id, spec) / name).read_bytes()
            if not (target / name).is_file() or (target / name).read_bytes() != data:
                (target / name).write_bytes(data)
        report["installed"].append(f"{plugin_id} {spec['version']}")
        data_json = target / "data.json"
        had_settings = data_json.exists()
        if had_settings and not reset_settings:
            report["settings_kept"].append(plugin_id)
        else:
            data_json.write_text(json.dumps(plugin_settings(spec), ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")
            (report["settings_reset"] if had_settings and reset_settings else report["settings_written"]).append(plugin_id)
    enabled_path = vault_dir / ".obsidian" / "community-plugins.json"
    enabled = _read_json(enabled_path, [])
    if enabled is None or not isinstance(enabled, list):
        report["problems"].append("community-plugins.json is not a JSON list; left unchanged — enable plugins in Obsidian")
    else:
        new = enabled + [p.split()[0] for p in report["installed"] if p.split()[0] not in enabled]
        if new != enabled:
            enabled_path.write_text(json.dumps(new, indent=2), encoding="utf-8", newline="\n")
    if report["installed"]:
        config.log(f"OBSIDIAN PLUGINS installed in {vault_dir.name}: {', '.join(report['installed'])}")
    return report


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    command = argv[0] if argv else "status"
    specs = approved()
    if command == "download":
        for plugin_id, spec in specs.items():
            if verify(plugin_id, spec):
                download(plugin_id, spec)
            print(f"{plugin_id} {spec['version']}: ok")
    elif command == "install":
        # Every sub-vault (a folder with its own .obsidian) plus the mother vault `vaults/` itself.
        reset_settings = "--reset-settings" in argv[1:]
        requested = [arg for arg in argv[1:] if arg != "--reset-settings"]
        folders = [config.vault_dir(v) for v in requested] or sorted(
            [p for p in config.VAULTS_DIR.iterdir() if (p / ".obsidian").is_dir()]
            + ([config.VAULTS_DIR] if (config.VAULTS_DIR / ".obsidian").is_dir() else []))
        for folder in folders:
            print(json.dumps(install(folder, reset_settings=reset_settings), ensure_ascii=False, indent=2))
    else:
        for plugin_id, spec in specs.items():
            print(f"{plugin_id} {spec['version']}: " + ("; ".join(verify(plugin_id, spec)) or "cached and verified"))


if __name__ == "__main__":
    for _stream in (sys.stdout, sys.stderr):
        if hasattr(_stream, "reconfigure"):
            _stream.reconfigure(encoding="utf-8", errors="replace")
    main()
