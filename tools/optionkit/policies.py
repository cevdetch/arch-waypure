"""Explicit package-specific flag groups."""

from copy import deepcopy
import re
import shlex
import tomllib

from .core import dynamic, same, value_error
from .pkgbuild import parse_flag


def load_config(root):
    path = root / "build-options.toml"
    data = tomllib.loads(path.read_text()) if path.is_file() else {}
    if not isinstance(data.get("package", {}), dict) or not isinstance(data.get("profiles", {}), dict):
        raise ValueError(f"{path}: package/profiles must be TOML tables")
    for key in ("source", "source_version", "backend", "array", "arch"):
        value = data.get("package", {}).get(key)
        if value is not None and (not isinstance(value, str) or not value):
            raise ValueError(f"{path}: package.{key} must be a nonempty string")
    for name, profile in data.get("profiles", {}).items():
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", name) or not isinstance(profile, dict):
            raise ValueError(f"{path}: use uppercase profile names such as WAYLAND")
        if not isinstance(profile.get("flags"), list) or not all(isinstance(flag, str) for flag in profile["flags"]):
            raise ValueError(f"{path}: {name}.flags must be a list of literal flags")
    return data


def selected_profiles(config, requested=None):
    names = requested if requested is not None else config.get("package", {}).get("profiles", [])
    if not isinstance(names, list) or not all(isinstance(name, str) for name in names):
        raise ValueError("package.profiles must be a list of profile names")
    return names


def intentions(data, config, names):
    result = {}
    for group in names:
        if group not in config.get("profiles", {}):
            raise ValueError(f"unknown profile {group}")
        profile = config["profiles"][group]
        for raw in profile["flags"]:
            # A TOML string is one native argument; Bash array rows are shell text.
            raw = shlex.quote(raw)
            name, value = parse_flag(raw, data["backend"])
            if dynamic(value) or "\n" in raw or "\r" in raw:
                raise ValueError(f"{group}: profiles require literal values: {raw}")
            record = data["options"].get(name)
            error = value_error(record, value) if record else None
            if error:
                raise ValueError(f"{group}: {name}={value}: {error}")
            if name in result and not same(result[name]["value"], value, data["options"].get(name)):
                raise ValueError(f"profiles conflict for {name}: {', '.join(result[name]['groups'])} versus {group}")
            item = result.setdefault(name, {"value": value, "raw": raw, "groups": [],
                                            "comment": "", "leading": [], "reason": profile.get("description", "")})
            item["groups"].append(group)
    return result


def project(array, desired, data):
    result = deepcopy(array)
    for name, choice in desired.items():
        old = result.active.get(name)
        item = deepcopy(choice)
        annotated = old or result.inactive.get(name)
        if annotated:
            item["comment"] = annotated["comment"]
            item["leading"] = annotated["leading"]
        if old:
            if same(old["value"], choice["value"], data["options"].get(name)):
                item["raw"] = old["raw"]
            else:
                item["previous"] = old["value"]
        else:
            item["previous"] = "unset"
        result.active[name] = item
    return result
