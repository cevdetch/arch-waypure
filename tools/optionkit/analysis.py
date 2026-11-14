"""Conservative feature consequences from source rules and requested flags."""

from dataclasses import dataclass
import fnmatch
import json
import re

from .core import array_items, dynamic, same, truth, value_error


@dataclass(frozen=True)
class State:
    value: str | None
    reasons: tuple = ()
    origin: str = "unknown"


class Model:
    def __init__(self, data, selected, ignore=None, defaults=None, uncertain=None):
        self.data, self.selected, self.ignore = data, selected, ignore
        self.defaults = defaults or {}
        self.uncertain = uncertain or {}
        self.active, self.memo = set(), {}
        self.autoconf_values = {}
        self.qt = None
        if data["backend"] == "qt6":
            from .qt6 import Model as QtModel
            self.qt = QtModel(data["adapter_data"], selected, ignore=ignore)

    def state(self, name):
        prefix, separator, suffix = name.partition(":")
        variable = prefix + separator + re.sub(r"[^A-Za-z0-9_]", "_", suffix) if separator else name
        if variable in self.uncertain:
            return State(None, (self.uncertain[variable],))
        if name in self.memo:
            return self.memo[name]
        if name in self.active:
            return State(None, ("cyclic or unresolved source dependency",))
        if self.qt and name.startswith("FEATURE_"):
            result = self.qt.feature(name.removeprefix("FEATURE_"))
            value = None if result.value is None else "ON" if result.value else "OFF"
            origin = "selected" if name in self.selected and name != self.ignore else "derived"
            return State(value, result.why, origin)
        record = self.data["options"].get(name)
        selected = self.selected.get(name) if name != self.ignore else None
        if record is None:
            return State(None)
        self.active.add(name)
        rules = record.get("rules", {})
        forced = None
        if self.data["backend"] == "cmake" and rules.get("dependency") and not rules.get("guards"):
            condition = self.condition(rules["dependency"])
            if condition.value is False:
                forced = State(rules["otherwise"], condition.why, "derived")
            elif condition.value is None:
                branch = selected["value"] if selected and not dynamic(selected["value"]) else record["default"]
                forced = State(branch, (), "derived") if branch is not None and same(branch, rules["otherwise"], record) else State(None, ("dependent condition is unresolved",))
        if rules.get("guards"):
            result = State(None, ("conditional declaration; review configure output",))
        elif rules.get("force") or rules.get("other_declarations") or rules.get("input_assignments"):
            result = State(None, ("CMake may override this value; review configure output",))
        elif forced is not None:
            result = forced
        elif self.data["backend"] == "autoconf" and rules.get("autoconf_variable"):
            from .autoconf_defaults import evaluate, slice_program
            variable = rules['autoconf_variable']
            if variable not in self.autoconf_values:
                selections = {key: item for key, item in self.selected.items() if key != self.ignore}
                program = rules.get('autoconf_program')
                if program is None:
                    program = slice_program(self.data.get('adapter_data', {}).get('default_program', []), {variable})[0]
                self.autoconf_values[variable] = evaluate(program, selections, self.uncertain).get(variable)
            value = self.autoconf_values[variable]
            normalized = {"yes": "ON", "no": "OFF"}.get(value.value, value.value) if value else None
            reasons = value.why if value else ("Autoconf did not establish " + rules["autoconf_variable"],)
            origin = "selected" if selected else "derived" if any(reason.startswith(("feature:", "with:")) for reason in reasons) else "platform" if reasons else "default"
            result = State(normalized, reasons, origin)
        elif selected:
            value = selected["value"]
            result = State(None if dynamic(value) else value, (f"{name}={value}",), "selected")
        elif name in self.defaults:
            value = self.defaults[name]
            result = State(value["value"], (value["reason"],), "context")
        elif self.data["backend"] == "ffmpeg" and rules.get("requires"):
            dependencies = [(key, self.state(key)) for key in rules["requires"]]
            disabled = next(((key, state) for key, state in dependencies if truth(state.value) is False), None)
            result = State("OFF", disabled[1].reasons or (disabled[0] + "=OFF",), "derived") if disabled else State(record["default"], (), "default")
        else:
            result = State(record["default"], (), "default")
        if (self.data["backend"] == "meson" and record["type"] == "feature"
                and name.rsplit(":", 1)[-1] != "auto_features" and result.value == "auto"):
            control = self.state("auto_features")
            if control.value != "auto":
                result = State(control.value, control.reasons, "auto_features")
        self.active.remove(name)
        self.memo[name] = result
        return result

    def condition(self, text):
        from . import qt6 as qt
        stream = qt.tokens(text.replace(";", " AND "))
        tokens = [token.value for token in stream]

        def value(name):
            if name in qt.LINUX:
                return qt.LINUX[name]
            return self.state(name).value

        def resolve(name):
            if name.upper() in {"ON", "OFF", "TRUE", "FALSE", "YES", "NO", "0", "1"}:
                return qt.Proof(bool(truth(name)))
            state = State(qt.LINUX[name]) if name in qt.LINUX else self.state(name)
            boolean = truth(state.value)
            reason = f"{name}={state.value}"
            if state.reasons and state.reasons != (reason,):
                reason += " <- " + "; ".join(state.reasons)
            return qt.Proof(boolean, (reason,)) if boolean is not None else qt.UNKNOWN

        # Ordinary CMake dereferences an unquoted RHS; Qt's feature evaluator
        # treats it as a literal. Resolve that distinction before reusing the
        # boolean parser, retaining unknown variable values as unknown.
        for index, token in enumerate(stream[:-1]):
            if token.value in qt.Expression.BINARY and not stream[index + 1].quoted:
                rhs = stream[index + 1].value
                resolved = value(rhs)
                if resolved is not None:
                    tokens[index + 1] = resolved
                elif not re.fullmatch(r"[0-9]+(?:\.[0-9]+)*", rhs) and truth(rhs) is None:
                    tokens[index + 1] = "$UNRESOLVED"

        return qt.Expression(tokens, resolve, value, cmake=True).evaluate()


def redundant(data, selected, defaults=None, uncertain=None):
    remaining, removed = dict(selected), {}
    if any(name.startswith("QT_FEATURE_") for name in selected):
        return remaining, removed
    for name, entry in selected.items():
        if name not in data["options"] or truth(entry["value"]) is not False:
            continue
        candidate = {key: value for key, value in remaining.items() if key != name}
        model = Model(data, candidate, defaults=defaults, uncertain=uncertain)
        proofs = {key: model.state(key) for key in list(removed) + [name]}
        def has_selected_cause(state):
            return state.origin != "auto_features" and any(re.search(r"(?<!\w)" + re.escape(key) + "=", reason)
                       for reason in state.reasons for key in candidate)

        if all(truth(state.value) is False and has_selected_cause(state) for state in proofs.values()):
            remaining, removed = candidate, proofs
    return remaining, removed


def setup_option(name, backend):
    if backend in {"qt6", "cmake"}:
        return name.startswith(("CMAKE_", "INSTALL_", "QT_INTERNAL_"))
    return name.startswith("option:") and name.removeprefix("option:") in {
        "prefix", "exec-prefix", "libdir", "libexecdir", "bindir", "sbindir", "includedir", "datadir", "datarootdir",
        "sysconfdir", "localstatedir", "runstatedir", "sharedstatedir", "mandir", "infodir", "host", "build", "target", "program-prefix"}


def check(data, array, desired, observed=None, strict=False):
    errors, notes = list(array.errors), list(data.get("notes", []))
    model = Model(data, array.active, defaults=array.defaults, uncertain=array.uncertain)
    for name, item in array.active.items():
        if name.startswith("QT_FEATURE_"):
            errors.append(f"{name}: internal Qt output; use FEATURE_{name.removeprefix('QT_FEATURE_')}")
            continue
        record = data["options"].get(name)
        if not record:
            if setup_option(name, data["backend"]):
                notes.append(f"{name}: setup setting; project availability not verified")
            elif data["backend"] == "autoconf" and not data.get("help_complete", False):
                notes.append(f"{name}: not found in literal declarations; generated configure help may define it")
            else:
                message = f"{name}: absent from this release catalogue; check removals/renames or inherited options"
                if data["backend"] == "autoconf" and ":" in name:
                    prefix, suffix = name.split(":", 1)
                    alternate = ("with:" if prefix == "feature" else "feature:") + suffix
                    if alternate in data["options"]:
                        message += f"; this release advertises {alternate}"
                errors.append(message)
            continue
        value = item["value"]
        if record.get("availability") == "unadvertised":
            notes.append(f"{name}: unadvertised configure input ({record['configure_variable']}); its effect requires source review")
        if record["deprecated"]:
            (errors if strict else notes).append(f"{name}: deprecated: {record['deprecated']}")
        if dynamic(value):
            notes.append(f"{name}: dynamic Bash value; configure-time review required")
            continue
        deprecated_values = record.get("deprecated_values", {})
        selected_values = array_items(value) if record["type"] == "array" else [str(value)]
        for selected_value in selected_values:
            if selected_value in deprecated_values:
                replacement = deprecated_values[selected_value] if isinstance(deprecated_values, dict) else None
                message = f"{name}: deprecated: value {selected_value}" + (f"; use {replacement}" if replacement else "")
                (errors if strict else notes).append(message)
        error = value_error(record, value)
        if error:
            errors.append(f"{name}={value}: {error}")
        predicted = model.state(name)
        if predicted.value is None and predicted.reasons:
            notes.append(f"{name}: requested {value}; effective value UNKNOWN: {'; '.join(predicted.reasons)}")
        if data["backend"] == "ffmpeg" and name.startswith("feature:") and value not in {"ON", "OFF"}:
            kind = name.removeprefix("feature:") + "s"
            components = data.get("adapter_data", {}).get("components", {})
            if kind in components:
                argument = value.split("=", 1)[1] if value.startswith(("enable=", "disable=")) else value
                for pattern in argument.split(","):
                    if not fnmatch.filter(components[kind], pattern):
                        errors.append(f"{name}={value}: no {kind} match {pattern!r}")
        if data["backend"] == "qt6" and name.startswith("FEATURE_"):
            definition = model.qt.definition(name.removeprefix("FEATURE_"))
            if definition:
                emit = model.qt.expression(definition["fields"].get("EMIT_IF"))
                condition = model.qt.expression(definition["fields"].get("CONDITION"))
                if emit.value is False:
                    notes.append(f"{name}: Qt ignores this option because EMIT_IF is false")
                elif truth(value) is True and condition.value is False:
                    errors.append(f"{name}=ON violates its source condition: {'; '.join(condition.why)}")
        if data["backend"] == "cmake" and record["rules"].get("dependency") and not record["rules"].get("guards"):
            condition = model.condition(record["rules"]["dependency"])
            if condition.value is False and not same(value, record["rules"]["otherwise"], record):
                errors.append(f"{name}: dependency is false; CMake forces {record['rules']['otherwise']}")
        if observed and name in observed and not same(value, observed[name], record):
            notes.append(f"{name}: requested {value}, observed build value {observed[name]}")
    for name, item in desired.items():
        groups = ", ".join(item["groups"])
        if name not in data["options"]:
            errors.append(f"{groups}: policy option {name} is absent from this release")
        elif name not in array.active:
            state = model.state(name)
            if state.value is not None and same(state.value, item["value"], data["options"].get(name)):
                notes.append(f"{groups}: {name}={item['value']} is satisfied by source rules/defaults without an explicit flag")
            else:
                errors.append(f"{groups}: {name}={item['value']} is requested by policy but unset in the recipe")
        elif not same(array.active[name]["value"], item["value"], data["options"].get(name)):
            errors.append(f"{groups}: {name} should be {item['value']}; recipe sets {array.active[name]['value']}")
    return errors, notes


def read_observed(path, data):
    native_version = data.get("source_version")
    expected_version = native_version if native_version not in {None, "unknown", "undefined"} else data["version"]
    if data["backend"] in {"qt6", "cmake"}:
        from . import qt6 as qt
        cache = qt.read_cache(path, data.get("adapter_data") if data["backend"] == "qt6" else None)
        version = cache.get("CMAKE_PROJECT_VERSION")
        if version and version != expected_version:
            raise ValueError(f"build cache is {version}; catalogue source version is {expected_version}")
        project = cache.get("CMAKE_PROJECT_NAME")
        if data["backend"] == "cmake" and project and project != data["project"]:
            raise ValueError(f"build cache project is {project}; catalogue is {data['project']}")
        return {key.removeprefix("QT_") if key.startswith("QT_FEATURE_") else key: value for key, value in cache.items()}
    if data["backend"] == "meson":
        info = path / "meson-info" if path.is_dir() else path.parent
        project = json.loads((info / "intro-projectinfo.json").read_text())
        if project["version"] != expected_version:
            raise ValueError("Meson build version does not match the release catalogue")
        if project.get("descriptive_name") and project["descriptive_name"] != data["project"]:
            raise ValueError("Meson build project does not match the release catalogue")
        items = json.loads((info / "intro-buildoptions.json").read_text())
        return {item["name"]: "true" if item["value"] is True else "false" if item["value"] is False else str(item["value"]) for item in items}
    raise ValueError(f"--build observation is not supported for {data['backend']}; source review remains available")
