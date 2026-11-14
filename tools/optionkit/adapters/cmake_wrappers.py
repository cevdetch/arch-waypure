"""Recognize bounded declaration wrappers by their bodies, not their names."""

import re


def discover(files, parser):
    definitions = {}
    for relative, text in files:
        commands = list(parser.commands(text))
        guards, index = 0, 0
        while index < len(commands):
            command, tokens, line = commands[index]
            values = [token.value for token in tokens]
            if command == "if":
                guards += 1
            elif command == "endif":
                guards = max(0, guards - 1)
            if command not in {"macro", "function"} or not values:
                index += 1
                continue
            end, depth = index + 1, 1
            while end < len(commands) and depth:
                if commands[end][0] in {"macro", "function"}:
                    depth += 1
                elif commands[end][0] in {"endmacro", "endfunction"}:
                    depth -= 1
                end += 1
            body = [(name, [token.value for token in args]) for name, args, _ in commands[index + 1:end - 1]]
            definitions.setdefault(values[0].lower(), []).append({
                "parameters": values[1:], "body": body, "file": relative,
                "body_quoted": [(name, [token.quoted for token in args]) for name, args, _ in commands[index + 1:end - 1]],
                "literal_parameters": any(token.literal_dollars and "$" in token.value
                                          for _, args, _ in commands[index + 1:end - 1] for token in args),
                "line": line, "guarded": bool(guards) or depth != 0})
            index = end
    unique = {name: items[0] for name, items in definitions.items()
              if len(items) == 1 and not items[0]["guarded"] and not items[0]["literal_parameters"]}
    helpers = {}
    for name, definition in unique.items():
        body, params = definition["body"], definition["parameters"]
        if (len(params) == 1 and len(body) == 1 and body[0][0] == "list"
                and len(body[0][1]) == 3 and body[0][1][0] == "APPEND"
                and re.fullmatch(r"[A-Za-z_]\w*", body[0][1][1])
                and body[0][1][2] == "${" + params[0] + "}"):
            helpers[name] = {"target": body[0][1][1], "file": definition["file"]}
    result = {}
    for name, definition in unique.items():
        params, body = definition["parameters"], definition["body"]
        symbols = ["${" + param + "}" for param in params]
        writes = []
        evidence = {definition["file"]}
        if body and body[0][0] in helpers and symbols and body[0][1] == [symbols[0]]:
            helper = helpers[body[0][0]]
            writes.append(helper["target"])
            evidence.add(helper["file"])
            body = body[1:]
        template, style = None, "literal"
        if len(body) == 1 and len(params) in {2, 3, 5}:
            primitive, args = body[0]
            if primitive == "option" and args == symbols and len(params) in {2, 3}:
                template = (primitive, args)
            elif primitive == "cmake_dependent_option" and len(params) == 5 and args == symbols:
                template = (primitive, args)
        if len(params) == 2 and len(body) == 6:
            local = body[1][1][0] if len(body[1][1]) == 2 else ""
            expected = [("if", ["${ARGC}", "EQUAL", "3"]), ("set", [local, "${ARGV2}"]),
                        ("else", []), ("set", [local, "OFF"]), ("endif", []),
                        ("option", symbols + ["${" + local + "}"])]
            if re.fullmatch(r"[A-Za-z_]\w*", local) and body == expected:
                template, style = expected[-1], "optional-default"
                definition = {**definition, "default_variable": local}
                writes.append(local)
        if len(params) == 3 and len(body) == 2:
            expected = [("set", [symbols[0], symbols[2], "CACHE", "STRING", symbols[1]]),
                        ("set", ["HAVE_" + symbols[0], symbols[2]])]
            if body == expected:
                template = expected[0]
                writes.append("HAVE_" + symbols[0])
        if len(params) == 1 and len(body) == 11:
            prefix_args, upper_args = body[0][1], body[1][1]
            if len(prefix_args) == 5 and len(upper_args) == 3:
                parse_prefix, upper = prefix_args[0], upper_args[2]
                option_args = body[8][1]
                match = re.fullmatch(r"([A-Za-z_]\w*_)\$\{" + re.escape(upper) + r"\}", option_args[0]) if len(option_args) == 3 else None
                if match:
                    target = option_args[0]
                    default = "${" + target + "_DEFAULT}"
                    expected = [("cmake_parse_arguments", [parse_prefix, "", "", "DEPS", "${ARGN}"]),
                                ("string", ["TOUPPER", symbols[0], upper]),
                                ("if", ["NOT", "DEFINED", target + "_DEFAULT"]),
                                ("set", [target + "_DEFAULT", "ON"]), ("endif", []),
                                ("if", [parse_prefix + "_DEPS"]),
                                ("cmake_dependent_option", [target, option_args[1], default, "${" + parse_prefix + "_DEPS}", "OFF"]),
                                ("else", []), ("option", [target, option_args[1], default]),
                                ("endif", []), ("list", ["APPEND", match[1] + "SUBSYSTEMS", symbols[0]])]
                    if body == expected:
                        template, style = ("option", option_args), "subsystem"
                        definition = {**definition, "uppercase": upper}
                        writes.extend([upper, target + "_DEFAULT", match[1] + "SUBSYSTEMS"]
                                      + [parse_prefix + suffix for suffix in ("_DEPS", "_UNPARSED_ARGUMENTS", "_KEYWORDS_MISSING_VALUES")])
        if template:
            quoted = next(quotation[1] for statement, quotation in
                          zip(definition["body"], definition["body_quoted"]) if statement == template)
            result[name] = {**definition, "template": template, "quoted": quoted,
                            "style": style, "writes": writes, "evidence": sorted(evidence)}
    return result


def expand(definition, tokens):
    # Both the invocation and its forwarding template must preserve argument
    # boundaries. CMake splits unquoted lists and drops unquoted empty values.
    values = [token.value for token in tokens]
    uncertain = any("$" in token.value or not token.quoted and (not token.value or ";" in token.value) for token in tokens)
    parameters = definition["parameters"]
    template, quoted = definition["template"][1], definition["quoted"]
    command = definition["template"][0]
    if definition["style"] == "optional-default":
        if len(values) < 2:
            return None
        bindings = dict(zip(parameters, values))
        bindings[definition["default_variable"]] = values[2] if len(values) == 3 else "OFF"
    elif definition["style"] == "subsystem":
        if not values:
            return None
        bindings = {parameters[0]: values[0], definition["uppercase"]: values[0].upper()}
        dependency = len(values) > 2 and values[1] == "DEPS"
        if len(values) > 1 and not dependency:
            uncertain = True
        if dependency:
            command = "cmake_dependent_option"
            template = template + [";".join(values[2:]), "OFF"]
            quoted = next(args for name, args in definition["body_quoted"] if name == command)
    else:
        # These exact templates never inspect ARGC/ARGN; CMake accepts unused
        # extra arguments, and their helper writes still happen.
        if len(values) < len(parameters):
            return None
        bindings = dict(zip(parameters, values))
    expanded = [re.sub(r"\$\{(\w+)\}", lambda match: bindings.get(match[1], match[0]), value) for value in template]
    uncertain |= any(not quote and (not value or ";" in value or "$" in value) for value, quote in zip(expanded, quoted))
    writes = [re.sub(r"\$\{(\w+)\}", lambda match: bindings.get(match[1], match[0]), value) for value in definition["writes"]]
    return command, expanded, {"uncertain": uncertain, "writes": writes}
