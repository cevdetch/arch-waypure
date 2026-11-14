"""Read literal packaging option arrays without evaluating shell code."""

from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path
import re
import shlex
import shutil

from .literals import Bindings, words


ARRAYS = {"meson": "_meson_options", "cmake": "_cmake_options",
          "qt6": "_cmake_options", "ffmpeg": "_make_options", "autoconf": "_configure_options"}
GENERATED = re.compile(r"^(?:[A-Z][A-Z0-9_]*\s+)*\[(?:SELECTED|POLICY CHANGE|REDUNDANT|IMPLIED OFF|IMPLIED ON|DEFAULT OFF|DEFAULT ON|DEFAULT|AUTO|UNKNOWN|UNSET)\](?:\s|$)")


def human_comment(comment):
    content = comment.removeprefix("#").strip()
    if GENERATED.match(content):
        _, separator, note = content.partition(" | NOTE: ")
        return "# " + note if separator else ""
    return comment


def comment_groups(comment):
    content = comment.removeprefix("#").strip()
    if GENERATED.match(content):
        return content.split("[", 1)[0].split()
    if re.fullmatch(r"[A-Z][A-Z0-9_]*(?:\s+[A-Z][A-Z0-9_]*)*", content):
        return content.split()
    return []


def split_comment(line):
    quote, escaped = None, False
    for index, character in enumerate(line):
        if escaped:
            escaped = False
        elif character == "\\" and quote != "'":
            escaped = True
        elif quote:
            if character == quote:
                quote = None
        elif character in "\"'":
            quote = character
        elif character == "#" and (index == 0 or line[index - 1].isspace()):
            return line[:index].rstrip(), line[index:]
    return line.rstrip(), ""


class LiteralRecipe:
    """One immutable text snapshot and its conservative literal bindings."""

    def __init__(self, text):
        self.text = text

    @classmethod
    def read(cls, path):
        return cls(path.read_text())

    @cached_property
    def bindings(self):
        return Bindings(self.text)

    def scalar(self, name):
        match = re.search(r"^" + re.escape(name) + r"=([^\n]*)", self.text, re.MULTILINE)
        if match:
            code, _ = split_comment(match[1])
            bindings = self.bindings
            if not bindings.unique(name) or match.start() >= bindings.scope_limit or code[:1].isspace():
                return None
            values = words(code, lambda reference: bindings.get(reference, match.start()))
            if values is not None and len(values) == 1:
                return values[0]
        return None


def scalar(path, name):
    return LiteralRecipe.read(path).scalar(name) if path.is_file() else None


def parse_flag(flag, backend):
    args = shlex.split(flag)
    if backend == "meson" and args and args[0].startswith("--auto-features"):
        if len(args) == 1 and args[0].startswith("--auto-features="):
            return "auto_features", args[0].split("=", 1)[1]
        if len(args) == 2 and args[0] == "--auto-features":
            return "auto_features", args[1]
        raise ValueError(f"expected --auto-features=VALUE: {flag}")
    if args[:1] == ["-D"]:
        args = ["-D" + args[1]] + args[2:] if len(args) > 1 else []
    if len(args) != 1:
        raise ValueError(f"expected one option assignment: {flag}")
    token = args[0]
    if backend in {"meson", "cmake", "qt6"}:
        match = re.fullmatch(r"-D([A-Za-z_][A-Za-z0-9_.:+-]*?)=(.*)", token, re.DOTALL)
        if not match:
            raise ValueError(f"expected -Dname=value: {flag}")
        name = match[1] if backend == "meson" else match[1].split(":", 1)[0]
        return name, match[2]
    match = re.fullmatch(r"--(enable|disable|with|without)-([^=]+)(?:=(.*))?", token)
    if match:
        prefix, name, value = match.groups()
        key = ("feature:" if prefix in {"enable", "disable"} else "with:") + name
        if backend == "ffmpeg" and value is not None:
            # Component selectors retain the operation as well as its argument.
            return key, prefix + "=" + value
        if prefix in {"disable", "without"}:
            value = "OFF" if value is None else value
        elif value is None or value == "yes":
            value = "ON"
        elif value == "no":
            value = "OFF"
        return key, value
    match = re.fullmatch(r"--([^=]+)(?:=(.*))?", token)
    if match:
        return "option:" + match[1], match[2] if match[2] is not None else "ON"
    raise ValueError(f"unsupported option: {flag}")


def flag_for(name, value, backend):
    if backend in {"meson", "cmake", "qt6"}:
        return shlex.quote(f"-D{name}={value}")
    prefix, _, key = name.partition(":")
    if prefix in {"feature", "with"}:
        positive, negative = ("enable", "disable") if prefix == "feature" else ("with", "without")
        if backend == "ffmpeg" and value.startswith((positive + "=", negative + "=")):
            action, argument = value.split("=", 1)
            return shlex.quote(f"--{action}-{key}={argument}")
        if value in {"ON", "OFF"}:
            return f"--{positive if value == 'ON' else negative}-{key}"
        return shlex.quote(f"--{positive}-{key}={value}")
    return shlex.quote(f"--{key}" + ("" if value == "ON" else "=" + value))


@dataclass
class Array:
    name: str
    active: dict = field(default_factory=dict)
    inactive: dict = field(default_factory=dict)
    setup: list = field(default_factory=list)
    defaults: dict = field(default_factory=dict)
    order: list = field(default_factory=list)
    uncertain: dict = field(default_factory=dict)
    errors: list = field(default_factory=list)

    def keep_setup(self, lines):
        for line in lines:
            self.order.append(("setup", len(self.setup)))
            self.setup.append(line)


def meson_wrapper_defaults(text, name):
    """Recognize a complete literal arch-meson wrapper without executing it."""
    lines = [split_comment(line)[0] for line in text.splitlines()]
    code = "\n".join(lines).replace("\\\n", " ")
    call = re.search(r"(?m)^\s*arch-meson\s+([^\n]+)", code)
    if not call or '${' + name + '[@]}' not in call[1]:
        return {}
    default = {"value": None, "reason": "arch-meson wrapper defaults could not be verified"}
    executable = shutil.which("arch-meson")
    if executable:
        try:
            script = Path(executable).read_text().replace("\\\n", " ")
            statements = [code.strip() for line in script.splitlines() if (code := split_comment(line)[0]).strip()]
            # Arch's source validation loop invokes a child process and does
            # not change positional arguments. Other shell code is unknown.
            arch_preamble = ['for arg in "$@" .; do',
                             'if [[ $arg == [^-]* && -d $arg && -f $arg/meson.build ]]; then',
                             'arch-meson-check-wraps "$arg"', 'break;', 'fi', 'done']
            prefix = statements[:-1]
            if prefix[-1:] == ["set -x"]:
                prefix = prefix[:-1]
            if prefix not in ([], arch_preamble) or not statements or not statements[-1].endswith('"$@"'):
                return {"auto_features": default}
            args = shlex.split(statements[-1], comments=True)
            if (len(args) < 5 or args[0] != "exec" or Path(args[1]).name != "meson"
                    or args[2] != "setup" or args[-1] != "$@"
                    or any(re.search(r"[$`;&|<>]", arg) for arg in args[:-1])):
                return {"auto_features": default}
            settings = []
            for index, token in enumerate(args[3:-1], 3):
                if token.startswith(("--auto-features=", "-Dauto_features=")):
                    settings.append((token.split("=", 1)[1], "long" if token.startswith("--") else "define"))
                elif token == "--auto-features":
                    settings.append((args[index + 1], "long"))
                elif token == "-D" and args[index + 1].startswith("auto_features="):
                    settings.append((args[index + 1].split("=", 1)[1], "define"))
            if settings and all(value in {"auto", "enabled", "disabled"} for value, _ in settings):
                value, syntax = settings[-1]
                default = {"value": value, "reason": f"arch-meson: auto_features={value}", "syntax": syntax}
                if len({syntax for _, syntax in settings}) > 1:
                    default["error"] = "Meson rejects both -Dauto_features and --auto-features in arch-meson"
        except (OSError, UnicodeError, ValueError):
            pass
    return {"auto_features": default}


def read_array(path, backend, name=None):
    automatic = name is None
    name = name or ARRAYS[backend]
    result = Array(name)
    if not path.is_file():
        return result
    text = path.read_text()
    if backend == "meson":
        result.defaults = meson_wrapper_defaults(text, name)
    match = re.search(r"^\s*(?:local\s+)?" + re.escape(name) + r"\s*=\((.*?)^\s*\)",
                      text, re.MULTILINE | re.DOTALL)
    if not match and automatic and backend == "autoconf" and name == "_configure_options":
        return read_array(path, backend, "_make_options")
    if not match and automatic:
        aliases = {"cmake": "_cmake_args", "qt6": "_cmake_args", "meson": "_meson_args"}
        alias = aliases.get(backend)
        if alias and re.search(r"\b" + alias + r"\s*=\(", text):
            return read_array(path, backend, alias)
    if not match:
        raise ValueError(f"{path}: expected a literal {name} array; specify --array for another name")
    leading = []
    for number, line in enumerate(match[1].splitlines(), 1):
        stripped = line.strip()
        if not stripped:
            leading = []
            continue
        active = not stripped.startswith("#")
        content = stripped if active else stripped[1:].lstrip()
        if not active and not content.startswith(("-D", "--")):
            leading.append(stripped)
            continue
        code, comment = split_comment(content)
        if not code:
            continue
        try:
            tokens = shlex.split(code)
            if backend == "autoconf" and len(tokens) == 1 and re.match(r"^[A-Za-z_]\w*=", tokens[0]):
                if active:
                    result.keep_setup(leading + [stripped])
                    variable = tokens[0].split("=", 1)[0]
                    for prefix, key in (("enable_", "feature:"), ("with_", "with:")):
                        if variable.startswith(prefix):
                            result.uncertain[key + variable.removeprefix(prefix)] = variable + " assignment may override the option; review configure argument order"
                leading = []
                continue
            meson_long = backend == "meson" and tokens[0].startswith("--auto-features")
            if backend in {"cmake", "qt6", "meson"} and not tokens[0].startswith("-D") and not meson_long:
                if active:
                    result.keep_setup(leading + [stripped])
                leading = []
                continue
            # One assignment per row preserves the original shell quoting.
            flags = [code]
            parsed = [(flag, *parse_flag(flag, backend)) for flag in flags]
        except (ValueError, IndexError) as error:
            if not active:
                leading = []
                continue
            raise ValueError(f"{path}: {name} row {number}: {error}") from error
        for index, (raw, key, value) in enumerate(parsed):
            target = result.active if active else result.inactive
            if active and key in target:
                raise ValueError(f"{path}: duplicate setting {key}")
            target[key] = {"value": value, "raw": raw,
                           "comment": human_comment(comment) if index == len(parsed) - 1 else "",
                           "leading": leading if index == 0 else [], "groups": comment_groups(comment)}
            if active:
                result.order.append(("option", key))
        leading = []
    if backend == "meson":
        context = result.defaults.get("auto_features", {})
        selected = result.active.get("auto_features")
        error = context.get("error")
        if selected and context.get("syntax"):
            syntax = "define" if shlex.split(selected["raw"])[0].startswith("-D") else "long"
            if syntax != context["syntax"]:
                error = "Meson rejects both -Dauto_features and --auto-features; match arch-meson's spelling"
        if error:
            result.errors.append(error)
            result.uncertain["auto_features"] = error
    return result
