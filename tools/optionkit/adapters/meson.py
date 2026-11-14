"""Use Meson's source introspection API for option declarations."""

import ast
import io
import json
import os
import re
import subprocess
import tokenize

from ..core import catalogue, digest, option


def deprecations(text):
    """Read literal option metadata, respecting strings, comments and nesting."""
    try:
        tokens = [token for token in tokenize.generate_tokens(io.StringIO(text).readline)
                  if token.type not in {tokenize.COMMENT, tokenize.NL, tokenize.NEWLINE,
                                        tokenize.INDENT, tokenize.DEDENT}]
    except (tokenize.TokenError, IndentationError):
        return
    for index in range(len(tokens) - 3):
        if tokens[index].type != tokenize.NAME or tokens[index].string != "option" or tokens[index + 1].string != "(":
            continue
        try:
            name = ast.literal_eval(tokens[index + 2].string)
        except (ValueError, SyntaxError):
            continue
        if not isinstance(name, str):
            continue
        depth, current = 1, index + 3
        while current < len(tokens) and depth:
            token = tokens[current]
            if depth == 1 and token.type == tokenize.NAME and token.string == "deprecated" and tokens[current + 1].string == ":":
                end, nested = current + 2, 0
                while end < len(tokens):
                    part = tokens[end]
                    if part.type == tokenize.OP:
                        if not nested and part.string in {",", ")"}:
                            break
                        if part.string in {"(", "[", "{"}:
                            nested += 1
                        elif part.string in {")", "]", "}"}:
                            nested -= 1
                    end += 1
                raw = "".join(part.string for part in tokens[current + 2:end])
                try:
                    value = {"true": True, "false": False}[raw] if raw in {"true", "false"} else ast.literal_eval(raw)
                except (ValueError, SyntaxError):
                    break
                yield name, value
                break
            if token.type == tokenize.OP:
                if token.string in {"(", "[", "{"}:
                    depth += 1
                elif token.string in {")", "]", "}"}:
                    depth -= 1
            current += 1


def capture(source):
    result = subprocess.run(["meson", "introspect", "--buildoptions", "--projectinfo", str(source / "meson.build")],
                            text=True, capture_output=True, timeout=60,
                            env={**os.environ, "LC_ALL": "C"})
    if result.returncode:
        raise ValueError("Meson source introspection failed:\n" + result.stdout + result.stderr)
    try:
        data = json.loads(result.stdout)
    except ValueError as error:
        raise ValueError("Meson did not return a JSON option catalogue") from error
    options, hashes = {}, {}
    for item in data["buildoptions"]:
        name = item["name"]
        value = item["value"]
        kind = item["type"]
        if isinstance(value, bool):
            value, kind = ("true" if value else "false"), "bool"
        elif isinstance(value, list):
            value = ",".join(str(part) for part in value)
        else:
            value = str(value)
        if item.get("choices") == ["enabled", "disabled", "auto"]:
            kind = "feature"
        options[name] = option(name, item["description"], value, kind, item.get("choices"),
                               category="project" if item["section"] == "user" else "setup",
                               deprecated=item.get("deprecated", False), choices_enforced=True)
    for directory, subdirs, files in os.walk(source):
        subdirs[:] = sorted(name for name in subdirs if name not in {".git", "build", "tests", "examples", "3rdparty"})
        for name in sorted(set(files) & {"meson.options", "meson_options.txt", "meson.build"}):
            path = source / os.path.relpath(directory, source) / name
            if path.is_symlink():
                continue
            text = path.read_text()
            hashes[path.relative_to(source).as_posix()] = digest(text)
            if name in {"meson.options", "meson_options.txt"}:
                relative = path.relative_to(source)
                parts = relative.parts[:-1]
                namespace = ""
                if parts:
                    # Meson subproject option names use the subproject directory.
                    indices = [index for index, part in enumerate(parts[:-1]) if part == "subprojects"]
                    if not indices or indices[-1] + 2 != len(parts):
                        continue
                    namespace = parts[indices[-1] + 1] + ":"
                for declared, notice in deprecations(text):
                    key = namespace + declared
                    if key not in options:
                        continue
                    if isinstance(notice, (list, dict)):
                        options[key]["deprecated_values"] = notice
                    elif notice:
                        options[key]["deprecated"] = "Declared deprecated in " + relative.as_posix() + (": " + notice if isinstance(notice, str) else "")
    info = data["projectinfo"]
    project = info.get("descriptive_name", source.name)
    if project in {"undefined", "unknown"}:
        match = re.search(r"\bproject\(\s*['\"]([^'\"]+)['\"]", (source / "meson.build").read_text())
        project = match[1] if match else source.name
    return catalogue("meson", project, info.get("version", "unknown"), options, hashes,
                     notes=["Subproject option declarations may differ from a configured build; dependency consequences require source rules."])
