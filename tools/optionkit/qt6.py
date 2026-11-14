"""Capture and compare Qt 6 module options without configuring a build.

  cd qt6-base
  qt6-options                              # snapshot release trees under src/
  qt6-options diff 6.11.1 6.12.0
  qt6-options render 6.12.0 --pkgbuild PKGBUILD
  qt6-options check 6.12.0 --build src/build
  qt6-options explain 6.12.0 eglfs_x11 --build src/build

Use snapshot SOURCE -o FILE for an extracted module or release tarball.
Snapshots live in options/<version>.json.log and retain the source rules.
diff/render hide CPU probes and features confined to other platforms; --all
shows them. Toolchain paths, dependency detections and cache internals are not
release options. A build cache supplies observed values for check/explain only.

PKGBUILDs are read as text, never executed. Inherited OFF results describe a
fresh configuration from the selected array, not stale FEATURE_* cache values.
Unknown expressions remain unknown. No command edits a PKGBUILD or build tree.
"""

from dataclasses import dataclass
import difflib
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import sys
import tarfile
import tempfile


FORMAT = 1
SKIP_DIRS = {".git", "3rdparty", "third_party", "tests", "examples", "doc", "build", "pkg"}
FLAGS = {"PUBLIC", "PRIVATE", "SYSTEM_LIBRARY", "NEGATE", "VCPKG_DEFAULT", "VCPKG_OPTIONAL"}
SINGLE = {"LABEL", "PURPOSE", "SECTION", "ALIAS_OF_FEATURE", "ALIAS_OF_CACHE", "VCPKG_DESCRIPTION"}
MULTI = {"CONDITION", "ENABLE", "DISABLE", "AUTODETECT", "EMIT_IF", "VCPKG_DEPENDENT_FEATURES"}
KEYWORDS = FLAGS | SINGLE | MULTI
CPU = re.compile(r"^(?:sse\d|ssse\d|avx|avx512|aesni$|f16c$|fma$|rdrnd$|rdseed$|"
                 r"bmi\d|popcnt$|sha_ni$|vaes$|vpclmul|arm_|neon|mips_|loongarch|lsx$|lasx$)")
BUILD_NAMES = {
    "BUILD_SHARED_LIBS", "BUILD_WITH_PCH", "BUILD_CMAKE_TESTING", "WARNINGS_ARE_ERRORS",
    "CMAKE_BUILD_TYPE", "CMAKE_INTERPROCEDURAL_OPTIMIZATION", "CMAKE_UNITY_BUILD",
    "CMAKE_UNITY_BUILD_BATCH_SIZE", "CMAKE_POSITION_INDEPENDENT_CODE",
    "QT_ALLOW_DOWNLOAD", "QT_GENERATE_SBOM", "QT_USE_CCACHE", "QT_UNITY_BUILD",
    "QT_UNITY_BUILD_BATCH_SIZE", "QT_INSTALL_EXAMPLES_SOURCES",
    "QT_INSTALL_EXAMPLES_SOURCES_BY_DEFAULT", "QT_INSTALL_CONFIG_INFO_FILES",
    "QT_QPA_DEFAULT_PLATFORM", "QT_QPA_PLATFORMS", "QT_QPA_PLATFORM",
    "QT_INTERNAL_AVOID_OVERRIDING_SYNCQT_CONFIG",
}
LINUX = {name: "OFF" for name in (
    "WIN32", "WINRT", "APPLE", "MACOS", "MACOS_IOS", "IOS", "UIKIT", "TVOS",
    "WATCHOS", "VISIONOS", "ANDROID", "WASM", "EMSCRIPTEN", "QNX", "INTEGRITY",
    "VXWORKS", "HAIKU", "FREEBSD", "OPENBSD", "NETBSD", "BSD", "RTEMS", "rtems", "SOLARIS",
    "OHOS", "HARMONYOS", "DARWIN", "MSVC", "MINGW", "CYGWIN",
)} | {"LINUX": "ON", "UNIX": "ON", "CMAKE_SYSTEM_NAME": "Linux"}


def normal(name):
    return re.sub(r"[^A-Za-z0-9_]", "_", name)


def meaningful(name):
    return name in BUILD_NAMES or name.startswith(("INPUT_", "QT_BUILD_", "QT_SBOM_", "BUILD_qt"))


@dataclass(frozen=True)
class Token:
    value: str
    line: int
    quoted: bool = False
    literal_dollars: bool = False


def tokens(text):
    """Read CMake arguments, including bracket comments and bracket strings."""
    result = []
    index, line = 0, 1
    while index < len(text):
        start, start_line = index, line
        char = text[index]
        if char.isspace():
            index += 1
            line += char == "\n"
            continue
        comment = char == "#"
        bracket = re.match(r"\[(=*)\[", text[index + int(comment):])
        if bracket:
            opening = bracket.group()
            begin = index + int(comment) + len(opening)
            close = "]" + bracket[1] + "]"
            end = text.find(close, begin)
            if end < 0:
                raise ValueError(f"unterminated CMake bracket at line {line}")
            index = end + len(close)
            line += text[start:index].count("\n")
            if not comment:
                result.append(Token(text[begin:end], start_line, True, True))
            continue
        if comment:
            end = text.find("\n", index)
            index = len(text) if end < 0 else end
            continue
        if char in "()":
            result.append(Token(char, line))
            index += 1
            continue
        quoted = char == '"'
        if quoted:
            index += 1
        value, literal_dollars = [], False
        closed = not quoted
        while index < len(text):
            char = text[index]
            if char == "\\" and index + 1 < len(text):
                following = text[index + 1]
                literal_dollars |= following == "$"
                if following != "\n":
                    value.append({"n": "\n", "t": "\t", "r": "\r"}.get(following, following))
                index += 2
                continue
            if quoted and char == '"':
                index += 1
                closed = True
                break
            if not quoted and (char.isspace() or char in '()#"'):
                break
            value.append(char)
            index += 1
        if not closed or index == start:
            raise ValueError(f"unsupported CMake token at line {start_line}")
        line += text[start:index].count("\n")
        result.append(Token("".join(value), start_line, quoted, literal_dollars))
    return result


def commands(text):
    stream = tokens(text)
    index = 0
    while index + 1 < len(stream):
        command, opening = stream[index:index + 2]
        if opening.value != "(" or opening.quoted:
            index += 1
            continue
        depth, end = 1, index + 2
        while end < len(stream) and depth:
            token = stream[end]
            if not token.quoted:
                depth += (token.value == "(") - (token.value == ")")
            end += 1
        if depth:
            raise ValueError(f"unclosed {command.value} at line {command.line}")
        yield command.value.lower(), stream[index + 2:end - 1], command.line
        index = end


def fields(arguments):
    result, current = {}, None
    for token in arguments:
        if not token.quoted and token.value in KEYWORDS:
            current = token.value
            result.setdefault(current, [])
        elif current:
            result[current].append(token.value)
    return result


def source_files(source):
    if source.is_dir():
        for directory, subdirs, names in os.walk(source):
            subdirs[:] = sorted(name for name in subdirs if name not in SKIP_DIRS)
            for name in sorted(names):
                if name == ".cmake.conf" or name == "CMakeLists.txt" or name.endswith(".cmake"):
                    path = Path(directory) / name
                    if path.is_symlink():
                        continue
                    yield path.relative_to(source).as_posix(), path.read_text()
    else:
        # Read selected text members; never extract archive paths or execute CMake.
        with tarfile.open(source, "r|*") as archive:
            for member in archive:
                parts = PurePosixPath(member.name).parts
                if not parts or member.name.startswith("/") or ".." in parts:
                    raise ValueError(f"unsafe archive member: {member.name}")
                relative = parts[1:]
                if not relative or any(part in SKIP_DIRS for part in relative[:-1]):
                    continue
                name = relative[-1]
                if not member.isfile() or not (name == ".cmake.conf" or name == "CMakeLists.txt" or name.endswith(".cmake")):
                    continue
                if member.size > 8 * 1024 * 1024:
                    raise ValueError(f"oversized CMake input: {member.name}")
                stream = archive.extractfile(member)
                if stream is None:
                    raise ValueError(f"unreadable archive member: {member.name}")
                yield "/".join(relative), stream.read().decode()


def capture(source):
    features, options, hashes, feature_inputs = {}, {}, {}, {}
    unresolved = []
    version, module = None, None
    for relative, text in source_files(source):
        if relative == ".cmake.conf":
            match = re.search(r'set\(QT_REPO_MODULE_VERSION\s+"?([0-9][\w.+-]*)', text)
            if match:
                version = match[1]
        if relative == "CMakeLists.txt":
            projects = re.findall(r"\bproject\(\s*(Qt[A-Za-z0-9_]+)", text, re.IGNORECASE)
            projects = [name for name in projects if not name.lower().endswith("dummy")]
            if projects:
                module = projects[0].lower()
        if not re.search(r"\b(?:qt_feature|qt_feature_alias|option|set)\s*\(", text, re.IGNORECASE):
            continue
        relevant = relative == "cmake/QtFeature.cmake"
        guards, functions = [], 0
        for command, args, line in commands(text):
            values = [token.value for token in args]
            if command == "if":
                guards.append(values)
            elif command in {"else", "elseif"}:
                if guards:
                    guards[-1] = ["UNRESOLVED_BRANCH"]
            elif command == "endif":
                if guards:
                    guards.pop()
            elif command in {"function", "macro"}:
                functions += 1
            elif command in {"endfunction", "endmacro"}:
                functions -= 1
            if not args:
                continue
            location = {"file": relative, "line": line}
            declaration_guards = list(guards) + ([["UNRESOLVED_FUNCTION"]] if functions else [])
            if command in {"set", "option"} and re.fullmatch(r"FEATURE_[A-Za-z0-9_]+", values[0]):
                feature_inputs.setdefault(values[0].removeprefix("FEATURE_"), []).append({
                    "values": values[1:], "guards": declaration_guards,
                    "force": command == "set" and ("CACHE" not in values or "FORCE" in values),
                    **location,
                })
                relevant = True
            if command in {"qt_feature", "qt_feature_alias"}:
                if functions:
                    continue
                name = values[0]
                if not re.fullmatch(r"[A-Za-z0-9_.+-]+", name):
                    unresolved.append({"name": name, **location})
                    relevant = True
                    continue
                key = normal(name)
                record = {"name": name, "fields": fields(args[1:]), "guards": list(guards), **location}
                record["alias"] = command == "qt_feature_alias"
                features.setdefault(key, []).append(record)
                relevant = True
            elif command == "option" and len(values) >= 2 and meaningful(values[0]):
                record = {"type": "BOOL", "default": values[2:] or ["OFF"], "description": values[1], "guards": declaration_guards, **location}
                save_option(options, unresolved, values[0], record)
                relevant = True
            elif command == "set" and "CACHE" in values and meaningful(values[0]):
                cache = values.index("CACHE")
                if len(values) > cache + 2 and values[cache + 1] in {"BOOL", "STRING"}:
                    record = {"type": values[cache + 1], "default": values[1:cache], "description": values[cache + 2], "guards": declaration_guards, **location}
                    save_option(options, unresolved, values[0], record)
                    relevant = True
            elif command == "set_property" and values[:1] == ["CACHE"] and "STRINGS" in values:
                if len(values) > 1 and values[1] in options:
                    options[values[1]]["values"] = values[values.index("STRINGS") + 1:]
        if relevant:
            hashes[relative] = hashlib.sha256(text.encode()).hexdigest()
    if not version or not module:
        raise ValueError(f"{source}: expected a Qt module with .cmake.conf and project(Qt...) metadata")
    for name, variants in features.items():
        if name in feature_inputs:
            for record in variants:
                record["inputs"] = feature_inputs[name]
    # Small modules can inherit all features; an empty own-feature list is valid.
    return {"format": FORMAT, "module": module, "version": version,
            "features": dict(sorted(features.items())), "options": dict(sorted(options.items())),
            "sources": dict(sorted(hashes.items())), "unresolved": sorted(unresolved, key=lambda item: (item["file"], item["line"]))}


def save_option(options, unresolved, name, record):
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
        unresolved.append({"name": name, "kind": "option", "file": record["file"], "line": record["line"]})
    elif name in options:
        options[name].setdefault("alternatives", []).append(record)
    else:
        options[name] = record


@dataclass(frozen=True)
class Proof:
    value: bool | None
    why: tuple[str, ...] = ()


UNKNOWN = Proof(None)


def combine(operator, left, right):
    decisive = False if operator == "AND" else True
    for proof in (left, right):
        if proof.value is decisive:
            return proof
    if left.value is None or right.value is None:
        return UNKNOWN
    return Proof(not decisive, tuple(dict.fromkeys(left.why + right.why)))


def boolean(value):
    upper = str(value).upper()
    if upper in {"", "0", "OFF", "NO", "FALSE", "N", "IGNORE", "NOTFOUND"} or upper.endswith("-NOTFOUND"):
        return False
    if upper in {"1", "ON", "YES", "TRUE", "Y"}:
        return True
    try:
        return float(value) != 0
    except ValueError:
        return True


class Expression:
    """Qt's evaluator short-circuits at each AND/OR; mixed chains associate right."""

    BINARY = {"STREQUAL", "EQUAL", "LESS", "LESS_EQUAL", "GREATER", "GREATER_EQUAL",
              "VERSION_EQUAL", "VERSION_LESS", "VERSION_LESS_EQUAL", "VERSION_GREATER", "VERSION_GREATER_EQUAL"}

    def __init__(self, expression, resolver, values, cmake=False):
        self.tokens, self.index = expression, 0
        self.resolver, self.values, self.cmake = resolver, values, cmake

    def evaluate(self):
        if not self.tokens:
            return Proof(True)
        try:
            result = self.chain()
            return result if self.index == len(self.tokens) else UNKNOWN
        except (IndexError, ValueError):
            return UNKNOWN

    def take(self):
        token = self.tokens[self.index]
        self.index += 1
        return token

    def chain(self):
        if self.cmake:
            left = self.cmake_and()
            while self.index < len(self.tokens) and self.tokens[self.index] == "OR":
                self.take()
                left = combine("OR", left, self.cmake_and())
            return left
        left = self.unary()
        while self.index < len(self.tokens) and self.tokens[self.index] in {"AND", "OR"}:
            operator = self.take()
            right = self.unary() if self.cmake else self.chain()
            left = combine(operator, left, right)
        return left

    def cmake_and(self):
        left = self.unary()
        while self.index < len(self.tokens) and self.tokens[self.index] == "AND":
            self.take()
            left = combine("AND", left, self.unary())
        return left

    def unary(self):
        token = self.take()
        if token == "NOT":
            result = self.unary()
            return Proof(None if result.value is None else not result.value, result.why)
        if token == "(":
            result = self.chain()
            if self.take() != ")":
                raise ValueError
            return result
        if token in {"TARGET", "COMMAND", "EXISTS", "DEFINED", "IS_DIRECTORY", "IS_SYMLINK"}:
            self.take()
            return UNKNOWN
        if self.index < len(self.tokens) and self.tokens[self.index] in self.BINARY:
            operator, rhs = self.take(), self.take()
            lhs_value = self.values(token)
            if lhs_value is None or "$" in rhs:
                return UNKNOWN
            rhs_value = rhs.strip("'")
            if operator == "STREQUAL":
                result = lhs_value == rhs_value
            else:
                if operator.startswith("VERSION_"):
                    lhs_value = tuple(int(part) for part in lhs_value.split("."))
                    rhs_value = tuple(int(part) for part in rhs_value.split("."))
                    length = max(len(lhs_value), len(rhs_value))
                    lhs_value += (0,) * (length - len(lhs_value))
                    rhs_value += (0,) * (length - len(rhs_value))
                    operator = operator.removeprefix("VERSION_")
                else:
                    lhs_value, rhs_value = float(lhs_value), float(rhs_value)
                result = {"EQUAL": lhs_value == rhs_value, "LESS": lhs_value < rhs_value,
                          "LESS_EQUAL": lhs_value <= rhs_value, "GREATER": lhs_value > rhs_value,
                          "GREATER_EQUAL": lhs_value >= rhs_value}[operator]
            return Proof(result, (f"{token}={self.values(token)!r}",))
        if self.index < len(self.tokens) and self.tokens[self.index] not in {"AND", "OR", ")"}:
            raise ValueError
        return self.resolver(token)


class Model:
    def __init__(self, data, selected=None, cache=None, ignore=None):
        self.data, self.selected, self.cache = data, selected or {}, cache or {}
        self.ignore = ignore
        self.active, self.memo = set(), {}
        self.alias_inputs = {}
        for alias in data["features"]:
            control = "FEATURE_" + alias
            if control not in self.selected or control == ignore:
                continue
            current, negate, uncertain, visited = alias, False, False, set()
            while current not in visited:
                visited.add(current)
                variants = data["features"].get(current, [])
                if len(variants) != 1:
                    break
                item = variants[0]
                parent = item["fields"].get("ALIAS_OF_FEATURE", [])
                if not item["alias"] or not parent:
                    break
                negate ^= "NEGATE" in item["fields"]
                uncertain |= bool(item["guards"])
                current = normal(parent[0])
                self.alias_inputs.setdefault(current, []).append((control, negate, uncertain))

    def value(self, name):
        if name in self.selected and name != self.ignore:
            value = self.selected[name]["value"]
            return None if "$" in value or "`" in value else value
        if name in LINUX:
            return LINUX[name]
        if name in self.cache:
            return self.cache[name]
        option = self.data["options"].get(name)
        if option and name.startswith("INPUT_") and not option.get("guards") and not option.get("alternatives"):
            default = ";".join(option["default"])
            if "$" not in default:
                return default
        return None

    def resolve(self, name):
        if name.startswith("QT_FEATURE_"):
            if name in self.cache:
                value = self.cache[name]
                return Proof(boolean(value), (f"cached {name}={value}",))
            feature = name.removeprefix("QT_FEATURE_")
            result = self.feature(feature)
            if result.value is not None and result.why:
                return Proof(result.value, (name + "=" + ("ON" if result.value else "OFF") + " <- " + "; ".join(result.why),))
            return result
        if name.upper() in {"ON", "OFF", "TRUE", "FALSE", "YES", "NO", "Y", "N", "0", "1"}:
            return Proof(boolean(name))
        value = self.value(name)
        return UNKNOWN if value is None else Proof(boolean(value), (f"{name}={value}",))

    def expression(self, values, default=True, cmake=False):
        if values is None:
            return Proof(default)
        return Expression(values, self.resolve, self.value, cmake).evaluate()

    def definition(self, feature):
        variants = self.data["features"].get(feature, [])
        usable = [item for item in variants if all(self.expression(guard, cmake=True).value is True for guard in item["guards"])]
        # Conditional declarations with unresolved branches need manual review.
        return usable[0] if len(usable) == 1 and len(variants) == 1 else None

    def feature(self, feature):
        if feature in self.memo:
            return self.memo[feature]
        if feature in self.active:
            return UNKNOWN
        if feature not in self.data["features"]:
            explicit = self.explicit(feature)
            if explicit is not None:
                return explicit
            value = self.cache.get("QT_FEATURE_" + feature)
            return UNKNOWN if value is None else Proof(boolean(value), (f"cached QT_FEATURE_{feature}={value}",))
        self.active.add(feature)
        definition = self.definition(feature)
        explicit = self.explicit(feature)
        assignments = [assignment for item in self.data["features"][feature] for assignment in item.get("inputs", [])
                       if not any(self.expression(guard, cmake=True).value is False for guard in assignment["guards"])]
        if assignments and (explicit is None or any(assignment["force"] for assignment in assignments)):
            result = Proof(None, (f"CMake assigns FEATURE_{feature}; review configure output",))
        elif definition is None:
            result = explicit if explicit and explicit.value is False else UNKNOWN
        elif definition["alias"]:
            fields = definition["fields"]
            if fields.get("ALIAS_OF_FEATURE"):
                result = self.feature(normal(fields["ALIAS_OF_FEATURE"][0]))
            elif fields.get("ALIAS_OF_CACHE"):
                result = self.resolve(fields["ALIAS_OF_CACHE"][0])
            else:
                result = UNKNOWN
            if "NEGATE" in fields:
                result = Proof(None if result.value is None else not result.value, result.why)
            if explicit is not None:
                result = explicit
        else:
            fields = definition["fields"]
            emit = self.expression(fields.get("EMIT_IF"))
            condition = self.expression(fields.get("CONDITION"))
            enable = self.expression(fields.get("ENABLE"), False)
            disable = self.expression(fields.get("DISABLE"), False)
            auto = self.expression(fields.get("AUTODETECT"))
            if "SYSTEM_LIBRARY" in fields:
                enable = combine("OR", self.resolve("QT_FEATURE_force_system_libs"), enable)
                disable = combine("OR", self.resolve("QT_FEATURE_force_bundled_libs"), disable)
            computed = self.choose(auto, condition, Proof(False, ("AUTODETECT OFF",)))
            computed = self.choose(enable, Proof(True), computed)
            computed = self.choose(disable, Proof(False, disable.why), computed)
            if explicit is not None:
                computed = explicit
            result = self.choose(emit, computed, Proof(False, emit.why))
        self.active.remove(feature)
        self.memo[feature] = result
        return result

    def explicit(self, feature):
        """Resolve input aliases conservatively; conflicting/dynamic inputs stay unknown."""
        controls = [("FEATURE_" + feature, False)]
        for control, negate, uncertain in self.alias_inputs.get(feature, []):
            if uncertain:
                return UNKNOWN
            controls.append((control, negate))
        proofs = []
        for control, negate in controls:
            if control not in self.selected or control == self.ignore:
                continue
            value = self.value(control)
            if value is None:
                return UNKNOWN
            proofs.append(Proof(boolean(value) != negate, (f"{control}={value}" + (" (negated alias)" if negate else ""),)))
        if not proofs:
            return None
        if len({proof.value for proof in proofs}) != 1:
            return UNKNOWN
        return Proof(proofs[0].value, tuple(reason for proof in proofs for reason in proof.why))

    @staticmethod
    def choose(test, yes, no):
        if test.value is None:
            return Proof(yes.value, tuple(dict.fromkeys(yes.why + no.why))) if yes.value is not None and yes.value == no.value else UNKNOWN
        branch = yes if test.value else no
        return Proof(branch.value, tuple(dict.fromkeys(test.why + branch.why)))


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
            return line[:index], line[index:].rstrip()
    return line, ""


class Selection(dict):
    def __init__(self):
        super().__init__()
        self.setup, self.inactive = [], {}


def selected_options(path):
    match = re.search(r"^\s*(?:local\s+)?_cmake_options\s*=\((.*?)^\s*\)", path.read_text(), re.MULTILINE | re.DOTALL)
    if not match:
        raise ValueError(f"cannot find a literal _cmake_options array in {path}")
    options = Selection()
    for line in match[1].splitlines():
        line = line.strip()
        active = not line.startswith("#")
        if not active:
            line = line[1:].lstrip()
            if not line.startswith("-D"):
                continue
        code, comment = split_comment(line)
        if not code.strip():
            continue
        try:
            args = shlex.split(code, comments=False)
        except ValueError:
            if not active:
                continue
            raise
        index, assignments, other = 0, [], []
        invalid = False
        while index < len(args):
            arg = args[index]
            if arg == "-D":
                index += 1
                if index == len(args):
                    if not active:
                        invalid = True
                        break
                    raise ValueError("-D without an assignment")
                arg = "-D" + args[index]
            if arg.startswith("-D"):
                name, separator, value = arg[2:].partition("=")
                name = name.split(":", 1)[0]
                if not separator or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
                    if not active:
                        invalid = True
                        break
                    raise ValueError(f"unsupported CMake assignment: {arg}")
                assignments.append((name, value))
            else:
                other.append(arg)
            index += 1
        if invalid or (not active and (len(assignments) != 1 or other)):
            continue
        if len(assignments) > 1 or (assignments and other):
            raise ValueError("put one -D assignment per line so its quoting and comments can be preserved")
        if assignments:
            name, value = assignments[0]
            target = options if active else options.inactive
            if active and name in options:
                raise ValueError(f"duplicate CMake setting: {name}")
            target[name] = {"value": value, "comment": comment, "raw": code.strip()}
        elif active:
            options.setup.append(code.strip() + (" " + comment if comment else ""))
    return options


class Cache(dict):
    def __init__(self):
        super().__init__()
        self.inherited = {}


def read_cache(build, data=None):
    path = build / "CMakeCache.txt" if build.is_dir() else build
    result = Cache()
    inherited = None
    for line in path.read_text().splitlines():
        origin = re.match(r"//Qt feature: (\w+) \(from target ([^)]+)\)", line)
        if origin:
            inherited = origin.groups()
        match = re.match(r"([^#/:][^:]*):[^=]+=(.*)$", line)
        if match:
            result[match[1]] = match[2]
            if inherited and match[1] == "QT_FEATURE_" + inherited[0]:
                result.inherited[inherited[0]] = inherited[1]
            inherited = None
    if data:
        project = result.get("CMAKE_PROJECT_NAME", "").lower()
        version = result.get("CMAKE_PROJECT_VERSION")
        if project and project != data["module"]:
            raise ValueError(f"{path}: build is for {project}, inventory is {data['module']}")
        if version and version != data["version"]:
            raise ValueError(f"{path}: build is {version}, inventory is {data['version']}")
        systems = list((path.parent / "CMakeFiles").glob("*/CMakeSystem.cmake"))
        for system in systems:
            match = re.search(r'set\(CMAKE_SYSTEM_NAME\s+"([^"]+)"\)', system.read_text())
            if match and match[1] != "Linux":
                raise ValueError(f"{path}: {match[1]} target; this tool models Linux packaging choices")
    return result


def load(path):
    data = json.loads(path.read_text())
    try:
        valid = (data["format"] == FORMAT
                 and re.fullmatch(r"qt[a-z0-9_]+", data["module"])
                 and re.fullmatch(r"[0-9][A-Za-z0-9.+_-]*", data["version"])
                 and isinstance(data["features"], dict) and isinstance(data["options"], dict))
        for name, variants in data["features"].items():
            valid = valid and bool(re.fullmatch(r"[A-Za-z0-9_]+", name)) and bool(variants)
            for item in variants:
                valid = valid and isinstance(item["fields"], dict) and isinstance(item["guards"], list)
                valid = valid and all(isinstance(value, str) for values in item["fields"].values() for value in values)
                valid = valid and all(isinstance(value, str) for guard in item["guards"] for value in guard)
        if not valid:
            raise ValueError
    except (KeyError, TypeError, AttributeError, ValueError):
        raise ValueError(f"invalid or unsupported Qt inventory: {path}") from None
    return data


def package_root(directory=None):
    directory = (directory or Path.cwd()).resolve()
    if (directory / ".cmake.conf").is_file():
        return directory.parent.parent if directory.parent.name == "src" else directory
    return directory.parent if directory.name == "src" else directory


def inventory_path(value):
    path = Path(value).expanduser()
    if path.exists() or "/" in value or value.endswith((".json", ".json.log")):
        return path
    return package_root() / "options" / (value + ".json.log")


def write_snapshot(data, output):
    if output.exists():
        if load(output) == data:
            print(f"{output}: unchanged")
            return
        raise ValueError(f"{output} already exists with different content; keep the baseline and choose another filename")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", dir=output.parent, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            json.dump(data, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.close()
            os.link(temporary, output)
        finally:
            temporary.unlink(missing_ok=True)
    print(f'{output}: {data["module"]} {data["version"]}, {len(data["features"])} features, {len(data["options"])} build/input controls')
    warn_unresolved(data)


def warn_unresolved(data):
    if data.get("unresolved"):
        print(f'Note: {len(data["unresolved"])} generated declaration(s) need manual/cache review; see snapshot unresolved records.', file=sys.stderr)


def scan(directory=None):
    root = package_root(directory)
    if (root / ".cmake.conf").is_file():
        sources = [root]
    else:
        sources = sorted(path for path in (root / "src").iterdir()
                         if path.is_dir() and (path / ".cmake.conf").is_file()) if (root / "src").is_dir() else []
    if not sources:
        raise ValueError(f"no extracted Qt module releases under {root / 'src'}; use snapshot SOURCE -o FILE for a tarball")
    for source in sources:
        data = capture(source)
        write_snapshot(data, root / "options" / (data["version"] + ".json.log"))


class PlatformModel(Model):
    """Find Linux-inapplicable conditions without assuming feature defaults or probes."""

    def value(self, name):
        return LINUX.get(name)

    def feature(self, feature):
        if feature in self.memo:
            return self.memo[feature]
        if feature in self.active:
            return UNKNOWN
        self.active.add(feature)
        variants = []
        for item in self.data["features"].get(feature, []):
            proof = Proof(True)
            for guard in item["guards"]:
                proof = combine("AND", proof, self.expression(guard, cmake=True))
            for field in ("CONDITION", "EMIT_IF"):
                proof = combine("AND", proof, self.expression(item["fields"].get(field)))
            variants.append(proof)
        result = Proof(False, tuple(reason for proof in variants for reason in proof.why)) if variants and all(proof.value is False for proof in variants) else UNKNOWN
        self.active.remove(feature)
        self.memo[feature] = result
        return result


def visible(data, name, include_all=False, selected=None):
    if include_all or "FEATURE_" + name in (selected or {}):
        return True
    if CPU.match(name):
        return False
    return PlatformModel(data).feature(name).value is not False


def signature(records):
    if isinstance(records, list):
        return [signature(record) for record in records]
    if isinstance(records, dict):
        return {key: signature(value) for key, value in records.items() if key not in {"file", "line"}}
    return records


def print_changes(previous, current):
    if isinstance(previous, list) and isinstance(current, list) and len(previous) == len(current) == 1:
        def feature_rules(record):
            result = dict(record["fields"])
            if record.get("inputs"):
                result["INPUT_ASSIGNMENTS"] = record["inputs"]
            if record["alias"]:
                result["ALIAS"] = True
            if record["guards"]:
                result["DECLARATION_GUARDS"] = " AND ".join("(" + " ".join(guard) + ")" for guard in record["guards"])
            return result
        previous, current = feature_rules(previous[0]), feature_rules(current[0])
    if isinstance(previous, dict) and isinstance(current, dict):
        def display(value):
            if value is None:
                return "(unset)"
            if isinstance(value, list) and all(isinstance(item, str) for item in value):
                return " ".join(value) if value else "(present)"
            return json.dumps(value, ensure_ascii=False)
        for key in sorted(previous.keys() | current.keys()):
            if previous.get(key) != current.get(key) or (key in previous) != (key in current):
                print(f"  {key}: {display(previous.get(key))} -> {display(current.get(key))}")
    else:
        for line in difflib.ndiff(json.dumps(previous, indent=2, sort_keys=True).splitlines(), json.dumps(current, indent=2, sort_keys=True).splitlines()):
            if line.startswith(("- ", "+ ")):
                print("  " + line)


def compare(old, new, include_all=False):
    if old["module"] != new["module"]:
        raise ValueError("compare releases of the same Qt module")
    print(f'{old["module"]}: {old["version"]} -> {new["version"]} (Linux packaging choices)')
    count = 0
    for kind in ("features", "options"):
        before, after = old[kind], new[kind]
        for name in sorted(before.keys() | after.keys()):
            if kind == "features" and not any(visible(data, name, include_all) for data in (old, new) if name in data[kind]):
                continue
            label = "FEATURE_" + name if kind == "features" else name
            if name not in before:
                print(f"+ {label}")
                count += 1
            elif name not in after:
                print(f"- {label}")
                count += 1
            else:
                previous, current = signature(before[name]), signature(after[name])
                if previous != current:
                    print(f"~ {label}")
                    print_changes(previous, current)
                    count += 1
    engine = "cmake/QtFeature.cmake"
    if old.get("sources", {}).get(engine) != new.get("sources", {}).get(engine):
        print("~ Qt's feature evaluator changed; review cmake/QtFeature.cmake before pruning options")
        count += 1
    if signature(old.get("unresolved")) != signature(new.get("unresolved")):
        print("~ Generated feature declarations changed; review snapshot unresolved records")
        count += 1
    if not count:
        print("No relevant option/default/dependency-rule changes.")
    warn_unresolved(new)


def proof_text(proof):
    return "; ".join(proof.why) or ("unresolved condition or configure-time detection" if proof.value is None else "upstream default")


def reduced_choices(data, selected):
    if any(name.startswith("QT_FEATURE_") for name in selected):
        raise ValueError("replace QT_FEATURE_* inputs with FEATURE_* before requesting removal suggestions")
    remaining, removed = dict(selected), {}
    for name, entry in selected.items():
        if not name.startswith("FEATURE_") or "$" in entry["value"] or "`" in entry["value"] or boolean(entry["value"]):
            continue
        candidate = {key: value for key, value in remaining.items() if key != name}
        model = Model(data, candidate)
        names = list(removed) + [name]
        # Recheck prior removals together; mutually dependent OFF flags cannot all disappear.
        proofs = {key: model.feature(key.removeprefix("FEATURE_")) for key in names}
        if all(proof.value is False for proof in proofs.values()):
            remaining = candidate
            removed = proofs
    return remaining, removed


def selected_context(args, data):
    path = args.pkgbuild or package_root() / "PKGBUILD"
    selected = selected_options(path) if path.is_file() else Selection()
    if args.pkgbuild and not path.is_file():
        raise ValueError(f"PKGBUILD not found: {path}")
    cache = read_cache(args.build, data) if args.build else {}
    return selected, cache


def render(data, selected, include_all=False, previous=None, entries_only=False, prune=False):
    if previous and previous["module"] != data["module"]:
        raise ValueError("--added-since must name the same Qt module")
    remaining, removed = reduced_choices(data, selected) if prune else (selected, {})
    if not entries_only:
        print(f'# {data["module"]} {data["version"]}: Linux packaging choices; review before applying.')
        print("_cmake_options=(")
    for line in selected.setup:
        print("  " + line)
    setup = [(name, entry) for name, entry in selected.items()
             if not name.startswith("FEATURE_") and name not in data["options"]]
    if setup:
        print("\n  # Existing CMake setup")
        for name, entry in setup:
            print("  " + entry["raw"] + (" " + entry["comment"] if entry["comment"] else ""))
    if data["options"]:
        print("\n  # Build and library choices")
        for name, option in data["options"].items():
            if previous and name in previous["options"] and name not in selected:
                continue
            entry = selected.get(name) or selected.inactive.get(name)
            if entry:
                prefix = "" if name in selected else "# "
                print("  " + prefix + entry["raw"] + (" " + entry["comment"] if entry["comment"] else ""))
            else:
                value = ";".join(option["default"])
                if "$" in value or option.get("alternatives"):
                    print(f"  # {name}: computed default; review {option['file']}:{option['line']}")
                else:
                    print("  # " + shlex.quote(f"-D{name}={value}") + " # " + " ".join(option["description"].split()))
    print("\n  # Features")
    model = Model(data, remaining)
    for name in sorted(data["features"], key=lambda key: (data["features"][key][0]["file"], data["features"][key][0]["line"])):
        control = "FEATURE_" + name
        if not visible(data, name, include_all, selected):
            continue
        if previous and name in previous["features"] and control not in selected:
            continue
        entry = selected.get(control) or selected.inactive.get(control)
        definition = data["features"][name][0]
        label = " ".join(" ".join(definition["fields"].get("LABEL", [name])).split())
        proof = model.feature(name)
        if entry:
            prefix = "" if control in remaining else "# "
            comment = entry["comment"]
            if control in removed:
                comment += ("; " if comment else "# ") + "implied OFF: " + proof_text(removed[control])
            print("  " + prefix + entry["raw"] + (" " + comment if comment else ""))
        elif proof.value is False:
            print(f"  # -D{control}=OFF # {label}; off: {proof_text(proof)}")
        else:
            condition = " ".join(definition["fields"].get("CONDITION", []))
            note = "; auto depends on " + condition if condition else "; upstream automatic choice"
            print(f"  # -D{control}=ON # {label}{note}")
    unknown = [(name, entry) for name, entry in selected.items()
               if name.startswith("FEATURE_") and name.removeprefix("FEATURE_") not in data["features"]]
    if unknown:
        print("\n  # Selected features absent from this source inventory: review before applying")
        for name, entry in unknown:
            print("  " + entry["raw"] + (" " + entry["comment"] if entry["comment"] else ""))
    if not entries_only:
        print(")")
    warn_unresolved(data)


def check(data, selected, cache):
    errors, warnings = [], []
    model = Model(data, selected)
    for name, entry in selected.items():
        if name.startswith("QT_FEATURE_"):
            errors.append(f"{name}: internal Qt state; use FEATURE_{name.removeprefix('QT_FEATURE_')}")
            continue
        if not name.startswith("FEATURE_"):
            continue
        feature = name.removeprefix("FEATURE_")
        if feature not in data["features"]:
            origin = getattr(cache, "inherited", {}).get(feature)
            if origin:
                warnings.append(f"{name}: inherited from {origin}; this module does not define its rules or rebuild that dependency")
                actual = cache.get("QT_FEATURE_" + feature)
                if actual is not None and boolean(actual) != boolean(entry["value"]):
                    warnings.append(f"{name}: requested {entry['value']}, inherited build value is {actual}")
            else:
                errors.append(f"{name}: feature absent from this source inventory; inherited features need a matching --build cache")
            continue
        if "$" in entry["value"] or "`" in entry["value"]:
            warnings.append(f"{name}: dynamic Bash value; review at configure time")
            continue
        if entry["value"].upper() not in {"ON", "OFF", "TRUE", "FALSE", "YES", "NO", "Y", "N"} and not entry["value"].isdigit():
            errors.append(f"{name}: Qt expects a boolean, not {entry['value']!r}")
            continue
        definition = model.definition(feature)
        if definition:
            emit = model.expression(definition["fields"].get("EMIT_IF"))
            condition = model.expression(definition["fields"].get("CONDITION"))
            if emit.value is False:
                warnings.append(f"{name}: Qt ignores this option in this configuration ({proof_text(emit)})")
            elif boolean(entry["value"]) and condition.value is False:
                errors.append(f"{name}=ON breaks its condition: {proof_text(condition)}")
        actual = cache.get("QT_FEATURE_" + feature)
        if actual is not None and boolean(actual) != boolean(entry["value"]):
            warnings.append(f"{name}: requested {entry['value']}, build cache reports {actual}")
    for warning in warnings:
        print("NOTE " + warning)
    for error in errors:
        print("ERROR " + error)
    if errors:
        print(f'\n{data["module"]} {data["version"]}: {len(selected)} selected CMake settings; {len(errors)} error(s), {len(warnings)} note(s).')
        print("Fix these inputs before evaluating redundant settings.")
        warn_unresolved(data)
        return True
    remaining, removed = reduced_choices(data, selected)
    if removed:
        print("\nOFF settings that can be omitted together in a fresh configuration:")
        for name, proof in removed.items():
            print(f"  {name}: {proof_text(proof)}")
    derived = Model(data, remaining)
    inherited = [(name, derived.feature(name)) for name in data["features"]
                 if "FEATURE_" + name not in selected and visible(data, name)]
    inherited = [(name, proof) for name, proof in inherited
                 if proof.value is False and any(re.search(r"(?<!\w)" + re.escape(key) + "=", reason)
                                                for reason in proof.why for key in remaining)]
    if inherited:
        print("\nOther OFF features implied by those choices:")
        for name, proof in inherited:
            print(f"  FEATURE_{name}: {proof_text(proof)}")
    print(f'\n{data["module"]} {data["version"]}: {len(selected)} selected CMake settings; {len(errors)} error(s), {len(warnings)} note(s).')
    print("Dependency proofs use source rules and selected settings; cache/probe failures do not justify removing guards.")
    warn_unresolved(data)
    return bool(errors)


def explain(data, feature, selected, cache):
    feature = normal(feature.removeprefix("QT_FEATURE_").removeprefix("FEATURE_"))
    if feature not in data["features"]:
        raise ValueError(f"unknown feature: {feature}")
    print(f'{data["module"]} {data["version"]}: FEATURE_{feature}')
    for item in data["features"][feature]:
        print(f'  Source: {item["file"]}:{item["line"]}')
        for key in ("LABEL", "CONDITION", "ENABLE", "DISABLE", "AUTODETECT", "EMIT_IF", "ALIAS_OF_FEATURE", "ALIAS_OF_CACHE"):
            if key in item["fields"]:
                print(f"  {key}: {' '.join(item['fields'][key])}")
        if item["guards"]:
            print("  Declaration guard: " + " AND ".join("(" + " ".join(guard) + ")" for guard in item["guards"]))
    for label, model in (("From selected settings", Model(data, selected)),
                         ("Without this explicit setting", Model(data, selected, ignore="FEATURE_" + feature))):
        proof = model.feature(feature)
        state = "unknown" if proof.value is None else "ON" if proof.value else "OFF"
        print(f"  {label}: {state}; {proof_text(proof)}")
    if cache:
        actual = cache.get("QT_FEATURE_" + feature, "not recorded")
        print(f"  Observed build cache: {actual}")
        model = Model(data, selected, cache)
        definition = model.definition(feature)
        if definition:
            for key in ("CONDITION", "ENABLE", "DISABLE", "AUTODETECT", "EMIT_IF"):
                if key in definition["fields"]:
                    proof = model.expression(definition["fields"][key])
                    state = "unknown" if proof.value is None else "true" if proof.value else "false"
                    print(f"  Observed {key}: {state}; {proof_text(proof)}")
