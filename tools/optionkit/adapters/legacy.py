"""Normalize native Qt/FFmpeg snapshots without changing their baselines."""

import re

from ..core import catalogue, option
from ..deprecations import cmake_messages, evidence


def normalize(data):
    if "features" in data and "module" in data:
        from .. import qt6 as qt
        options = {}
        model = qt.Model(data)
        for name, records in data["features"].items():
            fields = records[0]["fields"]
            state = model.feature(name).value
            value = None if state is None else "ON" if state else "OFF"
            category = "project" if qt.visible(data, name) else "platform"
            options["FEATURE_" + name] = option(
                "FEATURE_" + name, " ".join(fields.get("LABEL", [name])), value, "bool",
                rules={"variants": qt.signature(records)}, category=category,
                deprecated=evidence(" ".join(fields.get("LABEL", []))),
                source={"file": records[0]["file"], "line": records[0]["line"]})
        for name, record in data["options"].items():
            default = ";".join(record["default"])
            if "$" in default or record.get("alternatives") or record.get("guards"):
                default = None
            options[name] = option(name, record["description"], default,
                                   "bool" if record["type"] == "BOOL" else "string",
                                   record.get("values"), rules=qt.signature(record), deprecated=evidence(record["description"]))
        inputs = {token for records in data["features"].values() for record in records
                  for values in record["fields"].values() for token in values
                  if re.fullmatch(r"INPUT_[A-Za-z0-9_]+", token)}
        for name in sorted(inputs - options.keys()):
            options[name] = option(name, "Qt configure input referenced by feature rules", None, "string")
        return catalogue("qt6", data["module"], data["version"], options, data.get("sources"),
                         adapter_data=data, notes=["Generated declarations require manual review"] if data.get("unresolved") else [])
    if "components" not in data or "configure_sha256" not in data:
        raise ValueError("unrecognized legacy catalogue")
    from .. import ffmpeg
    options = {}
    for name, forms in data["options"].items():
        first = forms[0]
        default = None
        match = re.search(r"\[(yes|no|auto|autodetect|enabled|disabled)\]", first["description"], re.I)
        if match:
            value = match[1].lower()
            default = {"yes": "ON", "enabled": "ON", "no": "OFF", "disabled": "OFF"}.get(value, "auto")
        category = "project" if name.startswith("feature:") else "setup"
        options[name] = option(name, " | ".join(form["description"] for form in forms), default,
                               "feature" if name.startswith("feature:") else "string",
                               deprecated=next((evidence(form["description"]) for form in forms if evidence(form["description"])), False),
                               category=category, rules={"forms": [list(pair) for pair in ffmpeg.signature(forms)]})
    return catalogue("ffmpeg", "FFmpeg", data["version"], options,
                     {"configure": data["configure_sha256"]}, adapter_data=data)


def capture(source, backend):
    if backend == "qt6":
        from .. import qt6 as collector
    else:
        from .. import ffmpeg as collector
    result = normalize(collector.capture(source))
    if backend == "qt6":
        messages = ((relative, text, " ".join(token.value for token in args[1:]))
                    for relative, text in collector.source_files(source)
                    for command, args, _ in collector.commands(text)
                    if command == "message" and args and args[0].value == "DEPRECATION")
        result["sources"].update(cmake_messages(messages, result["options"]))
    if backend == "ffmpeg":
        text = (source / "configure").read_text()
        for name, dependencies in re.findall(r'^([A-Za-z0-9_]+)_deps="([A-Za-z0-9_ ]*)"\s*$', text, re.MULTILINE):
            key = "feature:" + name.replace("_", "-")
            if key in result["options"]:
                result["options"][key]["rules"]["requires"] = ["feature:" + part.replace("_", "-") for part in dependencies.split()]
    return result
