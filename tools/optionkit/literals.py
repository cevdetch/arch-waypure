"""A bounded reader for declarative Bash metadata; never evaluate shell code."""

from collections import Counter
import re


NAME = r"[A-Za-z_][A-Za-z0-9_]*"
REFERENCE = re.compile(r"\$(?:\{(" + NAME + r")\}|(" + NAME + r"))")
ASSIGNMENT = re.compile(r"(" + NAME + r")(\+?=)")
WRITES = re.compile(r"(?m)^[ \t]*(?:(?:local|export|readonly|declare|typeset)[ \t]+(?:-\w+[ \t]+)*)?("
                    + NAME + r")(?:\[[^\]\n]*\])?[ \t]*\+?=")
SPECIAL = {"_", "RANDOM", "SRANDOM", "SECONDS", "EPOCHSECONDS", "EPOCHREALTIME",
           "LINENO", "PPID", "UID", "EUID", "OPTIND", "DIRSTACK", "FUNCNAME",
           "GROUPS", "HISTCMD", "PIPESTATUS", "SHELLOPTS"}


def words(text, lookup=lambda name: None):
    """Read literal words and simple references, preserving Bash quote rules."""
    result, word = [], []
    quote, started, index = None, False, 0
    while index < len(text):
        character = text[index]
        if quote == "'":
            if character == "'":
                quote = None
            else:
                word.append(character)
        elif character == "\\":
            index += 1
            if index == len(text):
                return None
            following = text[index]
            if quote == '"' and following not in '\\$`"\n':
                word.append("\\")
            if following != "\n":
                word.append(following)
                started = True
        elif character == '"':
            quote = None if quote else '"'
            started = True
        elif character == "'" and quote is None:
            quote, started = "'", True
        elif character == "$":
            match = REFERENCE.match(text, index)
            if not match:
                return None
            value = lookup(match[1] or match[2])
            if value is None or quote is None and (not value or re.search(r"\s|[*?\[\]]", value)):
                return None
            word.append(value)
            started = True
            index = match.end() - 1
        elif character == "`" or quote is None and character in ";&|<>()*?[]~":
            return None
        elif quote is None and character == "{" and re.match(r"\{[^}]*?(?:,|\.\.)[^}]*\}", text[index:]):
            return None
        elif quote is None and character.isspace():
            if started:
                result.append("".join(word))
                word, started = [], False
        elif quote is None and character == "#" and not started:
            newline = text.find("\n", index)
            index = len(text) if newline < 0 else newline
        else:
            word.append(character)
            started = True
        index += 1
    if quote:
        return None
    if started:
        result.append("".join(word))
    return result


def array_body(text):
    """Find a literal array's closing delimiter, respecting shell text."""
    if not text.startswith("("):
        return None
    depth, quote, escaped, comment = 1, None, False, False
    for index, character in enumerate(text[1:], 1):
        if comment:
            comment = character != "\n"
        elif escaped:
            escaped = False
        elif character == "\\" and quote != "'":
            escaped = True
        elif quote:
            if character == quote:
                quote = None
        elif character in "\"'":
            quote = character
        elif character == "#" and (text[index - 1].isspace() or text[index - 1] == "("):
            comment = True
        elif character == "(":
            depth += 1
        elif character == ")":
            depth -= 1
            if not depth:
                return text[1:index]
    return None


class Bindings:
    """Unique scalar literals in a global prefix of metadata declarations."""

    def __init__(self, text):
        self.counts = Counter(match[1] for match in WRITES.finditer(text))
        self.unknown_unset = False
        for match in re.finditer(r"(?m)^[ \t]*(?:builtin[ \t]+)?unset[ \t]+([^\n]*)", text):
            targets = words(match[1])
            if targets is None:
                self.unknown_unset = True
                continue
            for target in targets:
                if target.startswith("-"):
                    continue
                name = re.fullmatch(r"(" + NAME + r")(?:\[[^\]\n]*\])?", target)
                if name:
                    self.counts[name[1]] += 1
                else:
                    self.unknown_unset = True
        boundary = re.search(r"(?m)^\s*(?:(?:function\s+)?[A-Za-z_]\w*\s*\(\s*\)\s*\{|(?:if|case|for|while|until)\s)", text)
        self.scope_limit = boundary.start() if boundary else len(text)
        self.values, self.limit = {}, len(text)
        position = 0
        while position < len(text):
            newline = text.find("\n", position)
            end = len(text) if newline < 0 else newline + 1
            line = text[position:end]
            if not line.strip() or line.lstrip().startswith("#"):
                position = end
                continue
            match = ASSIGNMENT.match(text, position)
            if not match or match[1] == "IFS":
                break
            array = array_body(text[match.end():])
            if array is not None:
                closing = match.end() + len(array) + 2
                newline = text.find("\n", closing)
                end = len(text) if newline < 0 else newline + 1
                if words(text[closing:end]) != []:
                    break
                raw = array
            else:
                raw = text[match.end():end]
            parsed = words(raw, lambda name: self.get(name, position))
            if parsed is None or array is None and (len(parsed) != 1 or raw[:1].isspace()):
                break
            if array is None and match[2] == "=" and self.unique(match[1]):
                self.values[match[1]] = (parsed[0], end)
            position = end
        self.limit = position

    def get(self, name, position):
        value = self.values.get(name)
        return value[0] if value and value[1] <= position <= self.limit else None

    def unique(self, name):
        return (not self.unknown_unset and self.counts[name] == 1
                and name not in SPECIAL and not name.startswith("BASH"))
