"""Bounded source rules for Autoconf defaults; no shell or M4 evaluation."""

from dataclasses import dataclass
import fnmatch
import re

from .literals import NAME, SPECIAL, words


ASSIGN = re.compile(r"^(" + NAME + r")(\+?=)(.*)$", re.DOTALL)
REFERENCES = re.compile(r"\$(?:\{(" + NAME + r")\}|(" + NAME + r"))")
WRITES = re.compile(r"\b(" + NAME + r")(?:\[[^\]\n]*\])?\s*\+?=(?!=)")
CONDITION_TOKENS = re.compile(r'''&&|\|\||[&|]|(?:[^\s'"\\&|]+|'[^']*'|"(?:[^"\\]|\\.)*"|\\.)+''')
PLATFORM = {"host_os": "linux-gnu", "build_os": "linux-gnu", "host_cpu": "x86_64", "build_cpu": "x86_64"}


def here_documents(text):
    pattern = re.compile(r"(?m)^(?P<header>[^\n]*?)<<-?[ \t]*(?P<quote>['\"]?)(?P<marker>" + NAME
                         + r")(?P=quote)(?P<tail>[^\n]*)\n(?P<body>[\s\S]*?)^[ \t]*(?P=marker)[ \t]*(?:\n|$)")
    def replace(match):
        literal = bool(match['quote']) or not any(character in match['body'] for character in "$`")
        reader = re.search(r"(?:^|\s)cat\s*$", match['header'])
        safe_tail = not re.search(r"[;&|]", match['tail'])
        prefix = match['header'][:reader.start()] if reader else ""
        header = re.sub(r"\s*(?:\|\||&&)\s*$", "", prefix) if literal and reader and safe_tail else "waypure_unresolved_here_document"
        return header + "\n" * match[0].count("\n")
    return pattern.sub(replace, text)


def unquote(text):
    text = text.strip()
    while text.startswith("[") and text.endswith("]"):
        text = text[1:-1].strip()
    return text


def macro(text, start):
    match = re.match(r"([A-Z][A-Z0-9_]+)\s*\(", text[start:])
    if not match:
        return None
    index = field = start + match.end()
    depth, brackets, arguments = 1, 0, []
    while index < len(text):
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
            if character == "," and depth == 1 or not depth:
                arguments.append(unquote(text[field:index]))
                field = index + 1
        if not depth:
            return match[1], arguments, index + 1
        index += 1
    return None


def statements(text):
    result, buffer = [], []
    quote, escaped, substitution, index = None, False, 0, 0
    while index < len(text):
        character = text[index]
        if escaped:
            escaped = False
        elif character == "\\" and quote != "'":
            escaped = True
        elif quote:
            if character == quote:
                quote = None
        elif character in "\"'`":
            quote = character
        elif character == "#" and (not buffer or buffer[-1].isspace()):
            newline = text.find("\n", index)
            index = len(text) if newline < 0 else newline
            continue
        elif character == "(" and (substitution or index and text[index - 1] == "$"):
            substitution += 1
        elif character == ")" and substitution:
            substitution -= 1
        elif character in ";\n" and not substitution:
            if "".join(buffer).strip():
                result.append("".join(buffer).strip())
            buffer = []
            if text[index:index + 2] == ";;":
                result.append(";;")
                index += 1
            index += 1
            continue
        buffer.append(character)
        index += 1
    if "".join(buffer).strip():
        result.append("".join(buffer).strip())
    return result


def targets(text):
    names = ({match[1] for match in WRITES.finditer(text)}
             | {match[1] for match in re.finditer(r"\$\{(" + NAME + r"):?=", text)})
    for match in re.finditer(r"\b(?:AS_VAR_SET|AS_VAR_APPEND|AS_VAR_COPY|AS_UNSET|AC_SUBST)\s*\(", text):
        call = macro(text, match.start())
        if call and call[1] and (call[0] != 'AC_SUBST' or len(call[1]) > 1):
            names.add(call[1][0] if re.fullmatch(NAME, call[1][0]) else '*')
    return sorted(names)


def case_matches(pattern, value):
    alternatives, buffer, quote, escaped = [], [], None, False
    for character in pattern:
        if escaped:
            buffer.append({"*": "[*]", "?": "[?]", "[": "[[]"}.get(character, character))
            escaped = False
        elif character == "\\" and quote != "'":
            escaped = True
        elif quote:
            if character == quote:
                quote = None
            else:
                if character in "$`" and quote != "'":
                    return None
                buffer.append({"*": "[*]", "?": "[?]", "[": "[[]"}.get(character, character))
        elif character in "\"'":
            quote = character
        elif character in "$`()":
            return None
        elif character == "|":
            alternatives.append("".join(buffer))
            buffer = []
        elif character.isspace():
            continue
        else:
            buffer.append(character)
    if quote or escaped:
        return None
    alternatives.append("".join(buffer))
    return any(fnmatch.fnmatchcase(value, part) for part in alternatives)


class Reader:
    def __init__(self, text, source="configure.ac", definitions=None, active=()):
        self.source, self.macros = source, {}
        self.definitions = definitions if definitions is not None else {}
        self.active = active
        text = here_documents(text)
        text = re.sub(r"(?m)^[ \t]*dnl\b[^\n]*", "", text)
        parts, start, index, quote, escaped = [], 0, 0, None, False
        while index < len(text):
            character = text[index]
            if escaped:
                escaped = False
            elif character == "\\" and quote != "'":
                escaped = True
            elif quote:
                if character == quote:
                    quote = None
            elif character in "\"'`":
                quote = character
            elif character == "#" and (index == 0 or text[index - 1].isspace()):
                newline = text.find("\n", index)
                index = len(text) if newline < 0 else newline
                continue
            elif character.isupper() and (index == 0 or not re.match(r"\w", text[index - 1])):
                call = macro(text, index)
                if call:
                    command, arguments, end = call
                    key = "@macro" + str(len(self.macros))
                    self.macros[key] = self.macro_node(command, arguments, text[:index].count("\n") + 1)
                    parts.extend((text[start:index], "\n" + key + "\n"))
                    start = index = end
                    continue
            index += 1
        parts.append(text[start:])
        self.tokens, self.index = statements("".join(parts)), 0

    def macro_node(self, command, args, line):
        where = self.source + ":" + str(line)
        if command in {'AC_DEFUN', 'AC_DEFUN_ONCE'} and len(args) == 2 and re.fullmatch(NAME, args[0]):
            self.definitions[args[0]] = args[1]
            return {'kind': 'group', 'nodes': []}
        if command in self.definitions:
            body = self.definitions[command]
            if command not in self.active and len(self.active) < 4 and len(body) < 16384:
                body = re.sub(r'\$([1-9])', lambda match: args[int(match[1]) - 1] if int(match[1]) <= len(args) else '', body)
                return {'kind': 'group', 'nodes': Reader(body, where, self.definitions, self.active + (command,)).read()}
            return {'kind': 'opaque', 'targets': ['*'], 'reason': command + ' exceeds bounded M4 expansion at ' + where}
        if command in {'AS_VAR_SET', 'AS_VAR_APPEND', 'AS_VAR_COPY', 'AS_UNSET', 'AC_SUBST'}:
            if command == 'AC_SUBST' and len(args) < 2:
                return {'kind': 'group', 'nodes': []}
            if args and re.fullmatch(NAME, args[0]):
                if command == 'AS_UNSET':
                    return {'kind': 'unset', 'targets': [args[0]]}
                value = args[1] if len(args) > 1 else ''
                if command == 'AS_VAR_COPY':
                    value = '"${' + value + '}"' if re.fullmatch(NAME, value) else '$(unresolved_copy)'
                return {'kind': 'assign', 'variable': args[0], 'value': value,
                        'append': command == 'AS_VAR_APPEND'}
            return {'kind': 'opaque', 'targets': ['*'], 'reason': command + ' has a dynamic variable target at ' + where}
        if command in {"AC_ARG_ENABLE", "AC_ARG_WITH"} and args and re.fullmatch(r"[A-Za-z0-9_.+-]+", args[0]):
            prefix = "enable_" if command == "AC_ARG_ENABLE" else "with_"
            return {"kind": "option", "variable": prefix + re.sub(r"\W", "_", args[0]),
                    "name": ("feature:" if prefix == "enable_" else "with:") + args[0],
                    "given": self.branch(args[2], where) if len(args) > 2 else [],
                    "default": self.branch(args[3], where) if len(args) > 3 else []}
        if command == "AS_IF" and len(args) >= 2:
            otherwise = self.branch(args[-1], where) if len(args) % 2 else []
            for index in reversed(range(0, len(args) - 1, 2)):
                otherwise = [{"kind": "if", "condition": args[index],
                              "yes": self.branch(args[index + 1], where), "no": otherwise}]
            return {"kind": "group", "nodes": otherwise}
        if command == "AS_CASE" and len(args) >= 3:
            arms = [[args[index], self.branch(args[index + 1], where)] for index in range(1, len(args) - 1, 2)]
            if len(args) % 2 == 0:
                arms.append(["*", self.branch(args[-1], where)])
            return {"kind": "case", "value": args[0], "arms": arms}
        probe = {'AC_CHECK_LIB': (2, 3), 'AC_COMPILE_IFELSE': (1, 2), 'AC_LINK_IFELSE': (1, 2)}.get(command)
        if probe:
            yes, no = (self.branch(args[index], where) if len(args) > index else [] for index in probe)
            children = [{'kind': 'opaque', 'targets': ['LIBS'], 'reason': command + ' may update LIBS at ' + where}] if command == 'AC_CHECK_LIB' else []
            children.append({'kind': 'if', 'condition': '', 'probe': command, 'yes': yes, 'no': no})
            return {'kind': 'group', 'nodes': children}
        return {"kind": "opaque", "targets": targets("\n".join(args)) if command.startswith(('AC_', 'AM_', 'LT_')) else ['*'],
                "reason": command + " at " + where + " requires external/M4 evaluation"}

    def branch(self, text, where):
        return Reader(text, where, self.definitions, self.active).read()

    def split_prefix(self, prefix):
        text = self.tokens[self.index]
        rest = text[len(prefix):].strip()
        self.index += 1
        if rest:
            self.tokens.insert(self.index, rest)

    def conditional(self, condition):
        if self.index < len(self.tokens) and self.tokens[self.index].split(None, 1)[0] == 'then':
            self.split_prefix('then')
        yes = self.read(('else', 'elif', 'fi'))
        no = []
        if self.index < len(self.tokens) and self.tokens[self.index].startswith('elif '):
            condition_else = self.tokens[self.index][5:].strip()
            self.index += 1
            no = [self.conditional(condition_else)]
        else:
            if self.index < len(self.tokens) and self.tokens[self.index].split(None, 1)[0] == 'else':
                self.split_prefix('else')
                no = self.read(('fi',))
            if self.index < len(self.tokens) and self.tokens[self.index] == 'fi':
                self.index += 1
        return {'kind': 'if', 'condition': condition, 'yes': yes, 'no': no}

    def read(self, stop=()):
        nodes = []
        while self.index < len(self.tokens):
            text = self.tokens[self.index]
            head = text.split(None, 1)[0]
            if head in stop or text == ";;" and ";;" in stop:
                break
            self.index += 1
            if text in self.macros:
                nodes.append(self.macros[text])
            elif head == "if":
                nodes.append(self.conditional(text[2:].strip()))
            elif head == "case":
                value = re.sub(r"\s+in$", "", text[4:].strip())
                arms = []
                while self.index < len(self.tokens) and self.tokens[self.index] != "esac":
                    arm = re.match(r"^\(?(.+?)\)\s*(.*)$", self.tokens[self.index], re.DOTALL)
                    if not arm:
                        break
                    self.index += 1
                    if arm[2]:
                        self.tokens.insert(self.index, arm[2])
                    body = self.read((";;", "esac"))
                    arms.append([arm[1].strip(), body])
                    if self.index < len(self.tokens) and self.tokens[self.index] == ";;":
                        self.index += 1
                if self.index < len(self.tokens) and self.tokens[self.index] == "esac":
                    self.index += 1
                nodes.append({"kind": "case", "value": value, "arms": arms})
            elif head in {"for", "while", "until"} or re.match(r"(?:function\s+)?" + NAME + r"\s*\(\s*\)\s*\{", text):
                end = "done" if head in {"for", "while", "until"} else "}"
                while self.index < len(self.tokens) and self.tokens[self.index] != end:
                    self.index += 1
                if self.index < len(self.tokens):
                    self.index += 1
                nodes.append({"kind": "opaque", "targets": ['*'], "reason": "unsupported loop/function in " + self.source})
            elif match := ASSIGN.fullmatch(text):
                parsed = words(match[3], lambda name: "value")
                if match[3][:1].isspace() or parsed is not None and len(parsed) > 1:
                    nodes.append({"kind": "opaque", "targets": ["*"], "reason": "command/multiple assignments after " + match[1]})
                else:
                    nodes.append({"kind": "assign", "variable": match[1], "value": match[3], "append": match[2] != "="})
            elif head == "unset":
                names = words(text[5:].strip())
                nodes.append({"kind": "unset", "targets": [name for name in names if not name.startswith("-")] if names is not None else ["*"]})
            elif head == 'printf' and '-v' in CONDITION_TOKENS.findall(text):
                nodes.append({'kind': 'opaque', 'targets': ['*'], 'reason': 'printf variable assignment in ' + self.source})
            elif "&&" in text or "||" in text or re.search(r"\$\{" + NAME + r":?=|\$\(\(", text):
                nodes.append({"kind": "opaque", "targets": targets(text) or ["*"],
                              "reason": "unsupported conditional/expansion assignment in " + self.source + ": " + text[:100]})
            elif re.fullmatch(r"[A-Z][A-Z0-9_]*", text):
                nodes.append(self.macro_node(text, [], 0))
            elif head in {":", "echo", "printf", "test", "[", "true", "false", "cat"}:
                continue
            else:
                nodes.append({"kind": "opaque", "targets": targets(text) or ["*"],
                              "reason": "unsupported shell statement in " + self.source + ": " + text[:100]})
        return nodes


def written(nodes):
    result = set()
    for node in nodes:
        if "variable" in node:
            result.add(node["variable"])
        result.update(node.get("targets", []))
        for key in ("yes", "no", "given", "default", "nodes"):
            result.update(written(node.get(key, [])))
        for _, body in node.get("arms", []):
            result.update(written(body))
    return result - {"*"}


def option_variables(nodes):
    result = {}
    for node in nodes:
        if node["kind"] == "option":
            result[node["variable"]] = node["name"]
        for key in ("yes", "no", "given", "default", "nodes"):
            result.update(option_variables(node.get(key, [])))
        for _, body in node.get("arms", []):
            result.update(option_variables(body))
    return result


def references(text):
    return {match[1] or match[2] for match in REFERENCES.finditer(text)}


def simple_condition(text):
    tokens = CONDITION_TOKENS.findall(text)
    segments, start = [], 0
    for index, token in enumerate(tokens):
        if token in {'&&', '||'}:
            segments.append(tokens[start:index])
            start = index + 1
    segments.append(tokens[start:])
    return not any(token in {'&', '|'} for token in tokens) and all(
        part and (part[0] in {'test', '['} or part in (['true'], ['false'])) for part in segments)


def slice_program(nodes, wanted):
    """Keep statements and guards that can affect the requested variables."""
    needed, result = set(wanted), []
    for node in reversed(nodes):
        kind = node['kind']
        if kind == 'assign':
            effects = targets(node['value']) if re.search(r"\$\{" + NAME + r":?=", node['value']) else []
            if node['variable'] not in needed and not needed.intersection(effects) and not re.search(r"\$\(\(", node['value']):
                continue
            needed.discard(node['variable'])
            needed.update(references(node['value']))
            result.append(node)
        elif kind == 'option':
            given, a = slice_program(node['given'], needed)
            default, b = slice_program(node['default'], needed)
            if node['variable'] not in needed and not given and not default:
                continue
            needed = a | b | {node['variable']}
            if 'enableval' in needed or 'withval' in needed:
                needed.add(node['variable'])
            result.append({**node, 'given': given, 'default': default})
        elif kind == 'group':
            children, needed = slice_program(node['nodes'], needed)
            if children:
                result.append({**node, 'nodes': children})
        elif kind == 'if':
            yes, a = slice_program(node['yes'], needed)
            no, b = slice_program(node['no'], needed)
            safe_condition = node.get('probe') or simple_condition(node['condition'])
            if not yes and not no and safe_condition and not re.search(r"\$\{" + NAME + r":?=|\$\(\(", node['condition']):
                continue
            needed = a | b | references(node['condition'])
            result.append({**node, 'yes': yes, 'no': no})
        elif kind == 'case':
            exhaustive = any(pattern.strip() == '*' for pattern, _ in node['arms'])
            arms, required = [], set() if exhaustive else set(needed)
            for pattern, body in node['arms']:
                filtered, before = slice_program(body, needed)
                arms.append([pattern, filtered])
                required.update(before)
            if not any(body for _, body in arms) and not re.search(r"\$\{" + NAME + r":?=|\$\(\(", node['value']):
                continue
            needed = required | references(node['value'])
            result.append({**node, 'arms': arms})
        elif kind in {'opaque', 'unset'}:
            names = needed if '*' in node['targets'] else needed.intersection(node['targets'])
            if names:
                result.append({**node, 'targets': ['*'] if kind == 'unset' and '*' in node['targets'] else sorted(names)})
                needed = needed - set(names)
    return list(reversed(result)), needed


@dataclass(frozen=True)
class Value:
    value: str | None
    why: tuple = ()


def reasons(*values):
    unique = []
    for value in values:
        for reason in value.why:
            if reason not in unique:
                unique.append(reason)
                if len(unique) == 8:
                    return tuple(unique) + ('additional source conditions omitted',)
    return tuple(unique)


def evaluate(program, selected=None, uncertain=None):
    variables = option_variables(program)
    all_names = written(program)
    inputs = all_names | references(str(program))
    environment = {name: Value(value, (name + "=" + value,)) for name, value in PLATFORM.items()}
    selected = selected or {}
    for name, entry in selected.items():
        if name in {"option:host", "option:build"}:
            prefix = name.split(":")[1]
            for target in ({prefix, 'host'} if prefix == 'build' else {prefix}):
                for suffix in ("os", "cpu"):
                    environment[target + "_" + suffix] = Value(None, (name + " override requires target context",))
        elif name.startswith(("feature:", "with:")):
            prefix, suffix = name.split(":", 1)
            variable = ("enable_" if prefix == "feature" else "with_") + re.sub(r"\W", "_", suffix)
            if variable not in inputs:
                continue
            value = {"ON": "yes", "OFF": "no"}.get(entry["value"], entry["value"])
            if value is None or any(character in str(value) for character in "$`"):
                value = None
            environment[variable] = Value(value, (name + "=" + str(entry["value"]),))
    for name, reason in (uncertain or {}).items():
        if name.startswith(("feature:", "with:")):
            prefix, suffix = name.split(":", 1)
            variable = ("enable_" if prefix == "feature" else "with_") + re.sub(r"\W", "_", suffix)
            environment[variable] = Value(None, (reason,))

    def taint(env, names, why):
        for name in (all_names | env.keys()) if "*" in names else names:
            env[name] = Value(None, why)

    def read(raw, env):
        used = []
        assigned = [match[1] for match in re.finditer(r"\$\{(" + NAME + r"):?=", raw)]
        if assigned or re.search(r"\$\(\(", raw):
            taint(env, assigned or ["*"], ("unsupported expansion side effect",))
        def lookup(name):
            value = env.get(name, Value(None, ("unresolved variable " + name,)))
            if name in SPECIAL or name.startswith("BASH"):
                value = Value(None, ("dynamic shell variable " + name,))
            if name in variables and value.value is not None:
                display = {"yes": "ON", "no": "OFF"}.get(value.value, value.value)
                value = Value(value.value, (variables[name] + "=" + display,) + value.why)
            used.append(value)
            return value.value
        parsed = words(raw, lookup)
        if parsed is not None and len(parsed) == 1:
            return Value(parsed[0], reasons(*used))
        if raw == "":
            return Value("")
        return Value(None, reasons(*used) or ("unsupported shell value: " + raw[:100],))

    def condition(raw, env):
        # Keep quotes until each operand is expanded; unknown operands are not
        # substituted with empty strings and cannot become false proofs.
        if raw in {"true", "false"}:
            return raw == "true", ()
        if not simple_condition(raw):
            taint(env, ["*"], ("unsupported condition command: " + raw,))
            return None, ("unsupported condition: " + raw,)
        tokens = CONDITION_TOKENS.findall(raw)
        operators = [index for index, token in enumerate(tokens) if token in {'&&', '||'}]
        if operators:
            start, outcome, evidence, operation = 0, None, (), None
            for end in operators + [len(tokens)]:
                value, why = condition(' '.join(tokens[start:end]), env)
                if operation is None:
                    outcome = value
                elif operation == '&&':
                    outcome = False if False in (outcome, value) else None if None in (outcome, value) else True
                else:
                    outcome = True if True in (outcome, value) else None if None in (outcome, value) else False
                evidence = tuple(dict.fromkeys(evidence + why))
                if end < len(tokens):
                    operation = tokens[end]
                    start = end + 1
            return outcome, evidence
        if tokens[:1] == ["test"]:
            tokens = tokens[1:]
        elif tokens[:1] == ["["] and tokens[-1:] == ["]"]:
            tokens = tokens[1:-1]
        else:
            taint(env, ["*"], ("unsupported condition command: " + raw,))
            return None, ("unsupported condition: " + raw,)
        def test(items):
            for operator in ("-o", "-a"):
                if operator in items:
                    index = items.index(operator)
                    a, why_a = test(items[:index])
                    b, why_b = test(items[index + 1:])
                    value = (True if True in (a, b) else None if None in (a, b) else False) if operator == "-o" else (False if False in (a, b) else None if None in (a, b) else True)
                    return value, tuple(dict.fromkeys(why_a + why_b))
            if items[:1] == ["!"]:
                value, why = test(items[1:])
                return None if value is None else not value, why
            values = [read(item, env) for item in items]
            why = reasons(*values)
            if any(value.value is None for value in values):
                return None, why
            parts = [value.value for value in values]
            if len(parts) == 3 and parts[1] in {"=", "==", "!="}:
                return (parts[0] == parts[2]) != (parts[1] == "!="), why
            if len(parts) == 2 and parts[0] in {"-n", "-z"}:
                return bool(parts[1]) != (parts[0] == "-z"), why
            if len(parts) == 1:
                return bool(parts[0]), why
            return None, ("unsupported condition: " + raw,)
        return test(tokens)

    def merge(outcomes, why):
        merged = {}
        for name in set().union(*(outcome.keys() for outcome in outcomes)):
            values = [outcome.get(name, Value(None, ("unset variable " + name,))) for outcome in outcomes]
            same = all(value.value == values[0].value for value in values)
            merged[name] = Value(values[0].value if same else None,
                                 reasons(*values) + (() if same else why))
        return merged

    def execute(nodes, env, guards=()):
        for node in nodes:
            kind = node["kind"]
            if kind == "assign":
                value = read(node["value"], env)
                if node["append"] or node["value"][:1].isspace():
                    value = Value(None, ("unsupported assignment to " + node["variable"],))
                env[node["variable"]] = Value(value.value, tuple(dict.fromkeys(value.why + guards)))
            elif kind == "option":
                present = node["variable"] in env
                if present:
                    env["enableval" if node["name"].startswith("feature:") else "withval"] = env[node["variable"]]
                env = execute(node["given"] if present else node["default"], env, guards)
            elif kind == "group":
                env = execute(node["nodes"], env, guards)
            elif kind == "if":
                value, why = (None, (node['probe'] + ' requires an external probe',)) if node.get('probe') else condition(node["condition"], env)
                if value is None:
                    env = merge([execute(node["yes"], dict(env), guards), execute(node["no"], dict(env), guards)], why)
                else:
                    env = execute(node["yes"] if value else node["no"], env, guards + why)
            elif kind == "case":
                value = read(node["value"], env)
                if value.value is None:
                    outcomes = [execute(body, dict(env), guards) for _, body in node["arms"]]
                    if not any(pattern.strip() == "*" for pattern, _ in node["arms"]):
                        outcomes.append(dict(env))
                    env = merge(outcomes, value.why) if outcomes else env
                else:
                    outcomes = []
                    unresolved = False
                    for pattern, body in node["arms"]:
                        matches = case_matches(pattern, value.value)
                        if matches is None:
                            unresolved = True
                            outcomes.append(execute(body, dict(env), guards))
                        elif matches:
                            outcomes.append(execute(body, dict(env), guards + value.why))
                            break
                    else:
                        outcomes.append(dict(env))
                    if outcomes:
                        env = merge(outcomes, ("unresolved case pattern",)) if unresolved else outcomes[0]
                    affected = set().union(*(written(body) for _, body in node['arms']))
                    for name in affected & env.keys():
                        env[name] = Value(env[name].value, tuple(dict.fromkeys(env[name].why + value.why)))
            elif kind == "unset":
                for name in node["targets"]:
                    if name == "*":
                        taint(env, ["*"], ("unknown unset target",))
                    else:
                        env.pop(name.split("[", 1)[0], None)
            elif kind == "opaque":
                taint(env, node["targets"], (node["reason"],))
        return env

    return execute(program, environment)
