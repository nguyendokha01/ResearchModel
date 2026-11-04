"""Domain profile: the domain knowledge that is not general English (species words, MeSH on/off).

`config/system.yaml` key `domain` (default biomedical) names `config/domain/<name>.yaml`. Library-wide, read once."""
import functools

from . import config


@functools.lru_cache(maxsize=None)
def profile():
    name = str((config._read_yaml("system.yaml") or {}).get("domain") or "biomedical")
    path = config.CONFIG_DIR / "domain" / f"{name}.yaml"
    if not path.is_file():
        raise FileNotFoundError(f"domain profile {name!r} (config/system.yaml `domain`) not found: {path}")
    data = config._read_yaml(f"domain/{name}.yaml")
    species = data.get("species") if isinstance(data, dict) else None
    # a bare string ("mouse: mice" -> m,i,c,e), an empty word (matches everywhere) or an unquoted yes/no would fill species wrongly
    if not (isinstance(species, dict) and isinstance(data.get("use_mesh"), bool) and all(
            isinstance(k, str) and isinstance(v, list) and v and all(isinstance(w, str) and w.strip() for w in v) for k, v in species.items())):
        raise ValueError(f"domain profile {path}: needs `use_mesh: true|false` and `species: {{key: [words]}}` (non-empty quoted words)")
    return {"name": name, "use_mesh": data["use_mesh"], "species": {k: list(v) for k, v in species.items()}}


profile()  # read at import: a later CONFIG_DIR patch (bench, tests) must not decide the library's profile (or crash on it)
