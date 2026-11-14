"""Catalogue declared CMake options; do not configure or evaluate project code."""

import re

from ..core import catalogue, digest, option
from ..deprecations import cmake_messages, evidence
from .. import qt6 as qt
from . import cmake_wrappers


def expand(value, variables):
    return re.sub(r"\$\{(\w+)\}", lambda match: variables.get(match[1], match[0]), value)


def capture(source):
    options, hashes, notes, assignments, all_hashes = {}, {}, [], {}, {}
    messages = []
    project, version = source.name, "unknown"
    files = dict(qt.source_files(source))
    wrappers = cmake_wrappers.discover(files.items(), qt)
    local_ctest = any(relative.rsplit("/", 1)[-1] == "CTest.cmake" for relative in files)
    for relative, text in files.items():
        all_hashes[relative] = digest(text)
        variables, guards, functions = {}, [], 0
        relevant = relative == "CMakeLists.txt"
        for command, args, line in qt.commands(text):
            values = [token.value for token in args]
            if command == "message" and values[:1] == ["DEPRECATION"]:
                messages.append((relative, text, " ".join(values[1:])))
            if command == "if":
                guards.append(values)
            elif command in {"else", "elseif"} and guards:
                guards[-1] = ["UNRESOLVED_BRANCH"]
            elif command == "endif" and guards:
                guards.pop()
            elif command in {"function", "macro"}:
                functions += 1
            elif command in {"endfunction", "endmacro"}:
                functions -= 1
            if not values:
                continue
            if command == "set" and len(values) == 2 and not guards and not functions:
                variables[values[0]] = expand(values[1], variables)
            if command == "project" and relative == "CMakeLists.txt":
                project = values[0]
                if "VERSION" in values:
                    version = expand(values[values.index("VERSION") + 1], variables)
                    if "$" in version:
                        version = "unknown"
            record = None
            scope = list(guards) + ([["UNRESOLVED_FUNCTION"]] if functions else [])
            wrapper = wrappers.get(command) if not functions else None
            expanded = cmake_wrappers.expand(wrapper, args) if wrapper else None
            if expanded:
                command, values, effects = expanded
                if effects["uncertain"]:
                    scope.append(["UNRESOLVED_WRAPPER_ARGUMENTS"])
                for target in effects["writes"]:
                    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", target):
                        assignments.setdefault(target, []).append({"values": ["<wrapper write>"], "guards": scope, "file": relative})
                for evidence_path in wrapper["evidence"]:
                    hashes[evidence_path] = all_hashes.get(evidence_path) or digest(files[evidence_path])
            if command == "set" and "CACHE" not in values and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", values[0]):
                assignments.setdefault(values[0], []).append({"values": values[1:], "guards": scope, "file": relative})
            if command in {"option", "cmake_dependent_option"} and len(values) >= 2:
                default = values[2] if len(values) > 2 else "OFF"
                if "$" in default:
                    default = None
                rules = {"guards": scope}
                if command == "cmake_dependent_option":
                    if len(values) != 5:
                        notes.append(f"Review dependent option at {relative}:{line}")
                        continue
                    rules.update(dependency=values[3], otherwise=values[4])
                record = option(values[0], values[1], default, "bool", rules=rules)
            elif command == "include" and values[:1] == ["CTest"] and not functions and not local_ctest:
                # CTest's public include contract declares BUILD_TESTING (ON).
                record = option("BUILD_TESTING", "Build the testing tree.", "ON", "bool",
                                rules={"guards": scope}, declaration_module="CTest")
            elif command == "set" and "CACHE" in values:
                index = values.index("CACHE")
                if len(values) > index + 2 and values[index + 1] in {"BOOL", "STRING", "PATH", "FILEPATH"}:
                    default = ";".join(values[1:index])
                    if "$" in default:
                        default = None
                    kind = "bool" if values[index + 1] == "BOOL" else "string"
                    category = "setup" if values[index + 1] in {"PATH", "FILEPATH"} else "project"
                    record = option(values[0], values[index + 2], default, kind, category=category,
                                    rules={"guards": scope, "force": "FORCE" in values[index + 3:]})
            elif command == "set_property" and values[:1] == ["CACHE"] and "STRINGS" in values:
                if len(values) > 1 and values[1] in options:
                    options[values[1]]["choices"] = values[values.index("STRINGS") + 1:]
            if record:
                if expanded:
                    record["rules"]["wrapper"] = {"file": wrapper["file"], "line": wrapper["line"]}
                name = record["name"]
                if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
                    notes.append(f"Generated option {name} at {relative}:{line}; manual review required")
                    continue
                relevant = True
                record["source"] = {"file": relative, "line": line}
                record["deprecated"] = evidence(record["description"])
                if name in options:
                    existing = options[name]
                    existing["default"] = None
                    existing["rules"].setdefault("other_declarations", []).append({
                        "default": record["default"], "rules": record["rules"]})
                else:
                    options[name] = record
        if relevant:
            hashes[relative] = digest(text)
    for name, records in assignments.items():
        if name in options:
            options[name]["rules"]["input_assignments"] = [{"values": item["values"], "guards": item["guards"]} for item in records]
            for item in records:
                hashes[item["file"]] = all_hashes[item["file"]]
    hashes.update(cmake_messages(messages, options))
    return catalogue("cmake", project, version, options, hashes, notes=notes)
