"""Common immutable catalogues and release comparisons."""

import ast
import difflib
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile


FORMAT = "waypure-options-1"
VERSION_LABEL = r'[A-Za-z0-9][A-Za-z0-9._!+~-]*'


def option(name, description="", default=None, kind="string", choices=None, **extra):
    return {"name": name, "description": description, "default": default,
            "type": kind, "choices": choices or [], "choices_enforced": False, "deprecated": False,
            "category": "project", "rules": {}, **extra}


def catalogue(backend, project, version, options, sources=None, **extra):
    if not version or not re.fullmatch(VERSION_LABEL, str(version)):
        raise ValueError("cannot determine a release version; pass --version explicitly")
    return {"format": FORMAT, "backend": backend, "project": project,
            "version": str(version), "options": dict(sorted(options.items())),
            "sources": dict(sorted((sources or {}).items())), "notes": [], **extra}


def digest(text):
    return hashlib.sha256(text.encode()).hexdigest()


def dynamic(value):
    return value is None or any(character in str(value) for character in "$`")


def truth(value):
    if dynamic(value):
        return None
    text = str(value).upper()
    if text in {"OFF", "NO", "FALSE", "N", "0", "", "DISABLED", "IGNORE", "NOTFOUND"} or text.endswith("-NOTFOUND"):
        return False
    if text in {"ON", "YES", "TRUE", "Y", "1", "ENABLED"}:
        return True
    return None


def same(left, right, record=None):
    if record and record["type"] in {"bool", "feature"}:
        a, b = truth(left), truth(right)
        if a is not None and b is not None:
            return a == b
    if record and record["type"] == "array":
        return array_items(left) == array_items(right)
    return str(left) == str(right)


def array_items(value):
    """Meson string-array arguments accept comma lists or Python list literals."""
    if isinstance(value, str):
        if value.startswith("["):
            try:
                value = ast.literal_eval(value)
            except (SyntaxError, ValueError) as error:
                raise ValueError(f"malformed array value: {value}") from error
        else:
            value = [part.strip() for part in value.split(",")] if value else []
    if not isinstance(value, list) or not all(isinstance(part, str) for part in value):
        raise ValueError(f"expected a string array: {value}")
    return value


def value_error(record, value):
    values = array_items(value) if record["type"] == "array" else [str(value)]
    if record["choices"] and record.get("choices_enforced", False):
        if any(part not in [str(choice) for choice in record["choices"]] for part in values):
            return f"allowed values are {record['choices']}"
    if record["type"] == "bool" and truth(value) is None:
        return "expected a boolean"
    return None


def atomic_text(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            stream.write(text)
            stream.close()
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


def write_snapshot(data, path):
    encoded = json.dumps(data, indent=2, sort_keys=True) + "\n"
    if path.exists():
        if json.loads(path.read_text()) == data:
            return False
        raise ValueError(f"{path}: differing baseline already exists; choose a new filename")
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            stream.write(encoded)
            stream.close()
            os.link(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
    return True


def record_catalogue(data, directory):
    """Retain immutable evidence when a collector or source tree changes."""
    if not re.fullmatch(VERSION_LABEL, str(data["version"])):
        raise ValueError("invalid release version: " + str(data["version"]))
    path = directory / (data["version"] + ".catalog.json.log")
    if path.exists() and json.loads(path.read_text()) != data:
        identity = digest(json.dumps(data, sort_keys=True))[:16]
        path = directory / (data["version"] + "." + identity + ".catalog.json.log")
    created = write_snapshot(data, path)
    atomic_text(directory / (data["version"] + ".current.json.log"),
                json.dumps({"snapshot": path.name}) + "\n")
    return path, created


def recorded_catalogue(directory, version):
    receipt = directory / (version + ".current.json.log")
    if not receipt.is_file():
        return None
    pointer = json.loads(receipt.read_text())
    name = pointer.get("snapshot") if isinstance(pointer, dict) else None
    if not isinstance(name, str) or not re.fullmatch(re.escape(version) + r"(?:\.[0-9a-f]{16})?\.catalog\.json\.log", name):
        raise ValueError(f"invalid catalogue receipt: {receipt}")
    path = directory / name
    if load(path)["version"] != version:
        raise ValueError(f"catalogue receipt version mismatch: {receipt}")
    return path


def load(path):
    data = json.loads(path.read_text())
    if isinstance(data, dict) and data.get("format") == 1:
        from .adapters.legacy import normalize
        data = normalize(data)
    if not isinstance(data, dict) or data.get("format") != FORMAT:
        raise ValueError(f"{path}: invalid option catalogue")
    if data.get("backend") not in {"qt6", "ffmpeg", "cmake", "meson", "autoconf", "cargo", "python"}:
        raise ValueError(f"{path}: unsupported backend")
    if not isinstance(data.get("options"), dict) or not isinstance(data.get("version"), str) or not re.fullmatch(VERSION_LABEL, data["version"]):
        raise ValueError(f"{path}: incomplete catalogue")
    if not isinstance(data.get('project'), str) or not data['project']:
        raise ValueError(f'{path}: missing project identity')
    if data['backend'] in {'cargo', 'python'}:
        validate_language_catalogue(data, path)
    for name, record in data["options"].items():
        if not re.fullmatch(r"[A-Za-z0-9_.:+-]+", name) or not isinstance(record, dict):
            raise ValueError(f"{path}: invalid option record")
        if not {"type", "default", "choices", "description", "rules", "category", "deprecated"} <= record.keys():
            raise ValueError(f"{path}: incomplete option {name}")
    return data


def validate_language_catalogue(data, path):
    """Check typed inventory shapes before comparisons consume cached evidence."""
    def strings(value):
        return isinstance(value, list) and all(isinstance(item, str) for item in value)

    def table(value):
        return isinstance(value, dict) and all(isinstance(key, str) for key in value)

    def require(condition):
        if not condition:
            raise ValueError(f'{path}: invalid {data["backend"]} catalogue payload')

    require(table(data.get('sources', {})) and all(isinstance(value, str) for value in data.get('sources', {}).values()))
    if data['backend'] == 'cargo':
        cargo = data.get('cargo', {})
        require(table(cargo))
        require(table(cargo.get('workspace', {})))
        require(isinstance(cargo.get('packages', []), list))
        manifests = set()
        for item in cargo.get('packages', []):
            require(table(item) and isinstance(item.get('manifest'), str))
            require(item['manifest'] not in manifests)
            manifests.add(item['manifest'])
            require(table(item.get('features', {})) and all(strings(value) for value in item.get('features', {}).values()))
            require(isinstance(item.get('dependencies', []), list) and all(table(value) for value in item.get('dependencies', [])))
        for key in ('manifests', 'build_scripts', 'lockfiles'):
            require(strings(cargo.get(key, [])))
    else:
        for key in ('build_requirements', 'runtime_requirements', 'dynamic_metadata', 'uncertain_metadata', 'backend_path'):
            if data.get(key) is not None:
                require(strings(data[key]))
        require(table(data.get('optional_dependencies', {})) and
                all(strings(value) for value in data.get('optional_dependencies', {}).values()))
        groups = data.get('dependency_groups', {})
        require(table(groups))
        for entries in groups.values():
            require(isinstance(entries, list))
            require(all(isinstance(item, str) or
                        table(item) and set(item) == {'include-group'} and
                        isinstance(item['include-group'], str) and bool(item['include-group']) for item in entries))
        require(table(data.get('metadata_coverage', {})))


def visible(record, include_all=False):
    return include_all or (record.get("category", "project") == "project"
                           and record.get("availability") != "unadvertised")


def display(value):
    if value is None:
        return "unknown"
    if isinstance(value, (dict, list)):
        return json.dumps(value, sort_keys=True, ensure_ascii=False)
    return str(value)


def compare(old, new, include_all=False):
    if (old["backend"], old["project"]) != (new["backend"], new["project"]):
        raise ValueError("compare releases of the same project/backend")
    lines = [f"{new['project']}: {old['version']} -> {new['version']}"]
    partial_autoconf = new["backend"] == "autoconf" and not (old.get("help_complete") and new.get("help_complete"))
    if partial_autoconf and old.get("help_complete") != new.get("help_complete"):
        lines.append("NOTE Generated configure help coverage differs; additions/removals of help-only options are not comparable.")
    count = 0
    if new['backend'] in {'cargo', 'python'}:
        if new['backend'] == 'cargo':
            for key in ('workspace', 'manifest', 'manifests', 'build_scripts', 'lockfiles'):
                before, after = old.get('cargo', {}).get(key), new.get('cargo', {}).get(key)
                if before != after:
                    lines.append(f'~ cargo {key}: {display(before)} -> {display(after)}')
                    count += 1
            for name in sorted(old.get('sources', {}).keys() | new.get('sources', {}).keys()):
                if old.get('sources', {}).get(name) != new.get('sources', {}).get(name):
                    lines.append(f'~ source input {name}: changed; review source evidence and resolver results')
                    count += 1
            keys = ('name', 'features', 'dependencies', 'build', 'links')
            left = {item['manifest']: item for item in old.get('cargo', {}).get('packages', [])}
            right = {item['manifest']: item for item in new.get('cargo', {}).get('packages', [])}
            for manifest in sorted(left.keys() | right.keys()):
                for key in keys:
                    before, after = left.get(manifest, {}).get(key), right.get(manifest, {}).get(key)
                    if before != after:
                        lines.append(f'~ {manifest} {key}: {display(before)} -> {display(after)}')
                        count += 1
        else:
            for key in ('build_requirements', 'runtime_requirements', 'optional_dependencies', 'dependency_groups',
                        'dynamic_metadata', 'uncertain_metadata', 'requires_python', 'build_backend', 'backend_path', 'metadata_coverage'):
                if old.get(key) != new.get(key):
                    lines.append(f'~ {key}: {display(old.get(key))} -> {display(new.get(key))}')
                    count += 1
    for name in sorted(old["options"].keys() | new["options"].keys()):
        before, after = old["options"].get(name), new["options"].get(name)
        if not any(visible(record, include_all) for record in (before, after) if record):
            continue
        if partial_autoconf and (before is None or after is None):
            record, other = (after, old) if before is None else (before, new)
            source_file = record.get('source', {}).get('file')
            if not other.get('help_complete') and (not source_file or source_file not in other.get('sources', {})):
                continue
        if before is None:
            lines.append("+ " + name)
            count += 1
        elif after is None:
            lines.append("- " + name + " (removed or no longer declared; no rename assumed)")
            count += 1
        else:
            changes = [(key, before.get(key), after.get(key)) for key in
                       ("type", "default", "choices", "description", "deprecated", "deprecated_values", "rules")
                       if before.get(key) != after.get(key)]
            if changes:
                lines.append("~ " + name)
                for key, left, right in changes:
                    if key == 'rules' and new['backend'] == 'autoconf' and any(
                            field in rules for rules in (left, right) for field in ('autoconf_program', 'autoconf_rule_hash')):
                        lines.append(f"  rules: Autoconf source dependencies changed; waypure-options explain {new['version']} {name}")
                    else:
                        lines.append(f"  {key}: {display(left)} -> {display(right)}")
                count += 1
    if new["backend"] == "ffmpeg":
        before = old.get("adapter_data", {}).get("components", {})
        after = new.get("adapter_data", {}).get("components", {})
        for kind in sorted(before.keys() | after.keys()):
            added, removed = set(after.get(kind, [])) - set(before.get(kind, [])), set(before.get(kind, [])) - set(after.get(kind, []))
            if added or removed:
                count += 1
                lines.extend("+ " + kind + ": " + name for name in sorted(added))
                lines.extend("- " + kind + ": " + name for name in sorted(removed))
    if not count:
        lines.append("No relevant option/default/rule changes.")
    return "\n".join(lines) + "\n"


def text_diff(before, after, before_name, after_name):
    return "".join(difflib.unified_diff(before.splitlines(keepends=True), after.splitlines(keepends=True),
                                        fromfile=before_name, tofile=after_name))
