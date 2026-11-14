"""Annotated, copyable Bash arrays."""

from .analysis import Model, redundant
from .core import truth, visible
from .pkgbuild import flag_for


def oneline(value):
    return " ".join(str(value).split())


def render(data, array, prune=False, include_all=False, observed=None, entries_only=False):
    remaining, removed = redundant(data, array.active, array.defaults, array.uncertain) if prune else (array.active, {})
    model = Model(data, remaining, defaults=array.defaults, uncertain=array.uncertain)
    lines = []
    if not entries_only:
        lines.extend([f"# {data['project']} {data['version']}: proposed options; review before copying.",
                      "# SELECTED = explicit; REDUNDANT = explicit OFF omitted; IMPLIED ON/OFF = follows another choice.",
                      "# DEFAULT OFF/ON = source default; AUTO = automatic; UNKNOWN = unresolved. Comments do not set flags.",
                      "# OBSERVED values describe one build; they never justify pruning.", array.name + "=("])
    order = list(array.order) if array.order else [("setup", index) for index in range(len(array.setup))]
    recorded = {key for kind, key in order if kind == "option"}
    order.extend(("option", name) for name in array.active if name not in recorded)
    order.extend(("option", name) for name in data["options"] if name not in array.active)
    for kind, name in order:
        if kind == "setup":
            lines.append("  " + array.setup[name])
            continue
        record = data["options"].get(name)
        if name not in array.active and record and not visible(record, include_all):
            continue
        entry = array.active.get(name) or array.inactive.get(name)
        state = model.state(name)
        groups, reason, comment = [], "", ""
        active = name in remaining
        if entry:
            raw = entry["raw"]
            lines.extend("  " + line for line in entry.get("leading", []))
            groups = entry.get("groups", [])
            comment = entry.get("comment", "").removeprefix("#").strip()
            if comment in groups:
                comment = ""
            if name in removed:
                status = "IMPLIED OFF" if entry.get("previous") == "unset" else "REDUNDANT"
                reason = "; ".join(removed[name].reasons) or "source default is OFF"
            elif "previous" in entry:
                status = "POLICY CHANGE"
                reason = "previously " + entry["previous"]
            else:
                status = "SELECTED" if active else "UNSET"
                if not active:
                    value = state.value
                    if record and record["type"] in {"bool", "feature"} and truth(value) is not None:
                        value = "ON" if truth(value) else "OFF"
                    elif record and record["type"] == "string" and value is not None:
                        value = repr(value) + (" (empty string)" if value == "" else "")
                    reason = "effective value " + (str(value) if value is not None else "UNKNOWN")
                    if state.reasons:
                        reason += ": " + "; ".join(state.reasons)
            if active and state.value is None and state.reasons:
                reason += "; effective value UNKNOWN: " + "; ".join(state.reasons)
            elif active and record and entry["value"] == "auto" and state.value != "auto" and state.reasons:
                reason += "; effective value " + str(state.value) + ": " + "; ".join(state.reasons)
        else:
            if state.value is None:
                status, raw = "UNKNOWN", "# " + name + "=<configure-time value>"
                reason = "; ".join(state.reasons) or "unresolved source rule or detection; no ON/OFF prediction"
            else:
                raw = flag_for(name, state.value, data["backend"])
                boolean = truth(state.value) if record and record["type"] in {"bool", "feature"} else None
                if boolean is False:
                    status = "IMPLIED OFF" if state.reasons and state.origin != "platform" else "DEFAULT OFF"
                elif boolean is True:
                    status = "IMPLIED ON" if state.reasons and state.origin != "platform" else "DEFAULT ON"
                else:
                    status = "AUTO" if state.value in {"auto", "undefined", "autodetect"} else "DEFAULT"
                reason = "; ".join(state.reasons) or (record["description"] if record else "")
                if record and record["type"] == "string" and state.value == "":
                    reason = "empty string" + ("; " + reason if reason else "")
        tag = " ".join(groups + ["[" + status + "]"])
        if record and record.get("deprecated"):
            reason += "; DEPRECATED: " + str(record["deprecated"])
        if record and record.get("availability") == "unadvertised":
            reason += "; UNVERIFIED: unadvertised configure input"
        if observed and name in observed:
            reason += "; OBSERVED=" + observed[name]
        if comment:
            reason += " | NOTE: " + comment
        prefix = "" if active else "# "
        if raw.startswith("# "):
            raw = raw[2:]
        lines.append("  " + prefix + raw + " # " + oneline(tag + (" " + reason if reason else "")))
    if not entries_only:
        lines.append(")")
    return "\n".join(lines) + "\n"
