"""Catalogue literal Autoconf arguments and generated configure help."""

import json
import re
import subprocess
import tempfile

from ..core import catalogue, digest, option
from ..autoconf_defaults import PLATFORM, Reader, evaluate, option_variables, slice_program
from ..deprecations import evidence
from ..pkgbuild import parse_flag


def unquote(text):
    text = text.strip()
    while text.startswith("[") and text.endswith("]"):
        text = text[1:-1].strip()
    return text


def calls(text, names):
    text = re.sub(r"(?m)^\s*(?:dnl\b|#)[^\n]*", "", text)
    pattern = re.compile(r"\b(" + "|".join(names) + r")\s*\(")
    for match in pattern.finditer(text):
        index, start, depth, brackets = match.end(), match.end(), 1, 0
        values = []
        while index < len(text) and depth:
            character = text[index]
            if character == "[":
                brackets += 1
            elif character == "]" and brackets:
                brackets -= 1
            elif not brackets:
                if character == "(":
                    depth += 1
                elif character == ")":
                    depth -= 1
                if (character == "," and depth == 1) or depth == 0:
                    values.append(unquote(text[start:index]))
                    start = index + 1
            index += 1
        if depth == 0:
            yield match[1], values, text[:match.start()].count("\n") + 1


def capture(source):
    options, hashes, notes = {}, {}, []
    program = []
    project, version = source.name, "unknown"
    inputs = [path for path in (source / "configure.ac", source / "configure.in") if path.is_file()]
    m4 = source / "m4"
    if m4.is_dir():
        inputs.extend(sorted(m4.rglob("*.m4")))
    for path in inputs:
        if path.is_symlink():
            continue
        text = path.read_text()
        relative = path.relative_to(source).as_posix()
        hashes[relative] = digest(text)
        if path == inputs[0]:
            program = Reader(text, relative).read()
        for command, args, line in calls(text, ("AC_INIT", "AC_ARG_ENABLE", "AC_ARG_WITH")):
            if command == "AC_INIT" and len(args) >= 2:
                project, version = args[0], args[1]
                if not re.fullmatch(r"[0-9][\w.+~-]*", version):
                    version = "unknown"
                continue
            if len(args) < 2 or not re.fullmatch(r"[A-Za-z0-9_.+-]+", args[0]):
                notes.append(f"Generated configure option at {relative}:{line}; manual review required")
                continue
            name = ("feature:" if command == "AC_ARG_ENABLE" else "with:") + args[0]
            help_calls = list(calls(args[1], ("AS_HELP_STRING",)))
            description = help_calls[0][1][1] if help_calls and len(help_calls[0][1]) > 1 else args[1]
            default = None
            if len(args) > 3:
                variable = ("enable_" if command == "AC_ARG_ENABLE" else "with_") + re.sub(r"\W", "_", args[0])
                match = re.fullmatch(re.escape(variable) + r"\s*=\s*['\"]?([A-Za-z0-9_.+-]+)['\"]?\s*;?", args[3].strip())
                if match:
                    default = {"yes": "ON", "no": "OFF"}.get(match[1], match[1])
            options[name] = option(name, " ".join(description.split()), default, "feature",
                                   deprecated=evidence(description),
                                   source={"file": relative, "line": line})
    configure = source / "configure"
    if configure.is_file():
        configure_text = configure.read_text()
        hashes["configure"] = digest(configure_text)
        with tempfile.TemporaryDirectory(prefix="build-options-help-") as directory:
            result = subprocess.run(["sh", str(configure.resolve()), "--help"], cwd=directory,
                                    text=True, capture_output=True, timeout=60)
        if result.returncode:
            raise ValueError("configure --help failed:\n" + result.stdout + result.stderr)
        lines = result.stdout.splitlines()
        for index, line in enumerate(lines):
            match = re.match(r"\s+(--(?:enable|disable|with|without)-[A-Za-z0-9_.+-]+)(?:\[[^\]]*\]|=[^\s]+)?(?:\s+(.*))?$", line)
            if not match:
                continue
            name, _ = parse_flag(match[1], "autoconf")
            if name in {"feature:FEATURE", "with:PACKAGE"}:
                continue
            description = match[2] or ""
            if not description and index + 1 < len(lines) and re.match(r"\s{8,}\S", lines[index + 1]):
                description = lines[index + 1].strip()
            if name not in options:
                options[name] = option(name, description, None, "feature",
                                       deprecated=evidence(description))
        # Some projects consume undocumented enable_*/with_* shell inputs.
        # A reference is evidence for review, not proof of a supported switch.
        for prefix, variable in re.findall(r"\$(?:\{)?(enable|with)_([A-Za-z0-9_]+)\b", configure_text):
            name = ("feature:" if prefix == "enable" else "with:") + variable.replace("_", "-")
            if name not in options:
                options[name] = option(name, "Unadvertised configure input", None, "feature",
                                       availability="unadvertised", configure_variable=prefix + "_" + variable)
        notes.append("Generated configure --help was read; only literal Autoconf defaults are inferred.")
    else:
        notes.append("Source declarations only: M4-generated/included options may require generated configure help.")
    variables = option_variables(program)
    for variable, name in variables.items():
        record = options.get(name)
        if record and record.get("source", {}).get("file") == inputs[0].name:
            record["rules"]["autoconf_variable"] = variable
            proof = slice_program(program, {variable})[0]
            record["rules"]["autoconf_rule_hash"] = digest(json.dumps(proof, sort_keys=True, separators=(',', ':')))
            value = evaluate(proof).get(variable)
            record["default"] = {"yes": "ON", "no": "OFF"}.get(value.value, value.value) if value else None
    if variables:
        notes.append("Bounded Autoconf source defaults use Arch Linux/x86_64; explicit --host/--build overrides require target context.")
    return catalogue("autoconf", project, version, options, hashes, notes=notes,
                     help_complete=configure.is_file(),
                     adapter_data={"default_program": program, "platform": PLATFORM})
