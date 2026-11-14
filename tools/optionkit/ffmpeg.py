"""Capture and compare FFmpeg release options without configuring a build.

  cd /path/to/ffmpeg
  ffmpeg-options                          # capture every release under src/
  ffmpeg-options diff 9.0.2 9.0.3
  ffmpeg-options render 9.0.3 --pkgbuild PKGBUILD > /tmp/make-options.sh.log
  ffmpeg-options render 9.0.3 --added-since 9.0.2
  ffmpeg-options check 9.0.3               # checks ./PKGBUILD

Also works from src/ or a FFmpeg release directory. Snapshots live in the
package's options/<version>.json.log; explicit JSON paths work for diff/render/check.
Use snapshot SOURCE -o FILE for a single source tree or a candidate inventory.

Snapshots retain all advertised options, their exact help order, and component
names. Render includes all options by default, in configure --help order;
--features-only hides directory/toolchain arguments and help commands. With
--pkgbuild, active choices are mixed into that order; all other rows are comments.
Existing option comments and attached notes in the PKGBUILD are retained.
An option's [default] is upstream's help text, not a detected or inferred value.
"""

import difflib
import fnmatch
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import tempfile


FORMAT = 1
OPTION = re.compile(r"^\s+(--[A-Za-z0-9]\S*)(?:\s+(.*))?$")
TOGGLE = re.compile(r"--(?:enable|disable)-")


def option_key(spelling):
    """Normalize polarity and argument placeholders, preserving option identity."""
    name = spelling.split("=", 1)[0].split("[", 1)[0]
    if TOGGLE.match(name):
        return "feature:" + TOGGLE.sub("", name)
    return "option:" + name.removeprefix("--")


def parse_help(help_text):
    options = {}
    section = "Options"
    current = None
    for line in help_text.splitlines():
        match = OPTION.match(line)
        if match:
            spelling, description = match.groups()
            current = {
                "spelling": spelling,
                "section": section,
                "description": description or "",
            }
            forms = options.setdefault(option_key(spelling), [])
            forms.append(current)
        elif line.startswith(" " * 20) and current is not None:
            current["description"] += " " + line.strip()
        else:
            if line.lstrip().startswith("--"):
                raise ValueError(f"unrecognized option line in configure help: {line!r}")
            current = None
            if line and not line[0].isspace() and line.endswith(":"):
                section = line.removesuffix(":")
    if not options:
        raise ValueError("configure --help contained no recognizable options")
    for forms in options.values():
        for form in forms:
            form["description"] = " ".join(form["description"].split())
        # De-duplicate after joining continuation lines, not before.
        unique = {(form["spelling"], form["description"], form["section"]): form
                  for form in forms}
        forms[:] = sorted(unique.values(), key=lambda form: form["spelling"])
    return dict(sorted(options.items()))


def capture(source):
    source = source.resolve()
    configure = source / "configure"
    version = (source / "RELEASE").read_text().strip()
    if not re.fullmatch(r"[0-9][A-Za-z0-9._+-]*", version):
        raise ValueError(f"not a release version in {source / 'RELEASE'}: {version!r}")
    # configure creates ffbuild even for --help/--list-*; isolate those files.
    with tempfile.TemporaryDirectory(prefix="ffmpeg-options-") as directory:
        # A sparse source view also works when the original tree has config.h:
        # FFmpeg otherwise refuses out-of-tree invocations before reading --help.
        for entry in source.iterdir():
            if entry.name not in {"ffbuild", "config.h", "config_components.h"}:
                (Path(directory) / entry.name).symlink_to(entry, target_is_directory=entry.is_dir())
        env = {**os.environ, "LC_ALL": "C", "COLUMNS": "80"}

        def run(argument):
            result = subprocess.run(
                [str(configure), argument], cwd=directory, env=env,
                text=True, capture_output=True, timeout=60,
            )
            if result.returncode:
                raise ValueError(f"configure {argument} failed:\n{result.stdout}{result.stderr}")
            return result.stdout

        help_text = run("--help")
        options = parse_help(help_text)
        lists = sorted(
            form["spelling"]
            for forms in options.values() for form in forms
            if form["spelling"].startswith("--list-")
        )
        components = {}
        for flag in lists:
            names = run(flag).split()
            if any(not re.fullmatch(r"[A-Za-z0-9_]+", name) for name in names):
                raise ValueError(f"unexpected output from {flag}; refusing an incomplete snapshot")
            components[flag.removeprefix("--list-")] = sorted(set(names))
    return {
        "format": FORMAT,
        "version": version,
        "configure_sha256": hashlib.sha256(configure.read_bytes()).hexdigest(),
        "sections": re.findall(r"^(\S.*):$", help_text, flags=re.MULTILINE),
        "help_order": [match[1] for line in help_text.splitlines()
                       if (match := OPTION.match(line))],
        "options": options,
        "components": components,
    }


def load(path):
    data = json.loads(path.read_text())

    def require(condition):
        if not condition:
            raise ValueError

    try:
        require(isinstance(data, dict) and data.get("format") == FORMAT)
        require(isinstance(data["version"], str)
                and re.fullmatch(r"[A-Za-z0-9._+-]+", data["version"]))
        require(re.fullmatch(r"[a-f0-9]{64}", data["configure_sha256"]))
        require(isinstance(data["options"], dict) and data["options"])
        require(isinstance(data.get("sections", []), list))
        require(all(isinstance(section, str) and '\n' not in section and '\r' not in section
                    for section in data.get("sections", [])))
        for key, forms in data["options"].items():
            require(isinstance(forms, list) and forms)
            for form in forms:
                require(all(isinstance(form[field], str)
                            for field in ("spelling", "description", "section")))
                require(key == option_key(form["spelling"]))
                require(not any('\n' in value or '\r' in value for value in form.values()))
        require(isinstance(data["components"], dict))
        for kind, names in data["components"].items():
            require(re.fullmatch(r"[a-z]+s", kind))
            require(isinstance(names, list))
            require(all(re.fullmatch(r"[A-Za-z0-9_]+", name) for name in names))
        if "help_order" in data:
            order = data["help_order"]
            spellings = {form["spelling"] for forms in data["options"].values() for form in forms}
            require(isinstance(order, list) and all(isinstance(flag, str) for flag in order))
            require(set(order) == spellings)
    except (ValueError, KeyError, TypeError, AttributeError):
        raise ValueError(f"invalid or unsupported inventory: {path}") from None
    return data


def package_root(directory=None):
    directory = (directory or Path.cwd()).resolve()
    if (directory / "configure").is_file() and (directory / "RELEASE").is_file():
        return directory.parent.parent if directory.parent.name == "src" else directory
    return directory.parent if directory.name == "src" else directory


def resolve_inventory(value):
    path = Path(value).expanduser()
    if path.exists() or value.endswith((".json", ".json.log")) or "/" in value:
        return path
    directory = package_root() / "options"
    ignored = directory / f"{value}.json.log"
    legacy = directory / f"{value}.json"
    return legacy if legacy.exists() and not ignored.exists() else ignored


def write_snapshot(data, output, allow_identical=False):
    add_order = False
    if output.exists():
        existing = load(output)
        if allow_identical and existing == data:
            print(f"{output}: unchanged")
            return
        # Add missing order metadata only if every existing record and source
        # checksum matches. Never replace changed baseline content.
        add_order = (allow_identical and "help_order" not in existing
                     and {**existing, "help_order": data.get("help_order")} == data)
        if not add_order:
            raise ValueError(f"{output} already exists; use a new filename to preserve the baseline")
    output.parent.mkdir(parents=True, exist_ok=True)
    # Publish only a complete JSON file; hard-link creation cannot replace a baseline.
    with tempfile.NamedTemporaryFile(mode="w", dir=output.parent, delete=False) as temporary:
        candidate = Path(temporary.name)
        try:
            json.dump(data, temporary, indent=2, sort_keys=True)
            temporary.write("\n")
            temporary.close()
            if add_order:
                # Preserve any concurrent edit rather than replacing it.
                if load(output) != existing:
                    raise ValueError(f"{output} changed during metadata upgrade")
                os.replace(candidate, output)
            else:
                os.link(candidate, output)
        finally:
            candidate.unlink(missing_ok=True)
    print(f'{output}: {len(data["options"])} options, '
          f'{sum(map(len, data["components"].values()))} components')


def scan(directory=None):
    root = package_root(directory)
    sources = [path for path in (root / "src").glob("FFmpeg-*")
               if (path / "configure").is_file() and (path / "RELEASE").is_file()]
    if not sources and (root / "configure").is_file() and (root / "RELEASE").is_file():
        sources = [root]
    if not sources:
        raise ValueError(f"no FFmpeg release trees found under {root / 'src'}")
    for source in sorted(sources):
        data = capture(source)
        write_snapshot(data, root / "options" / f'{data["version"]}.json.log', allow_identical=True)


def signature(forms):
    # Section moves/rewrapping are not changes to the option itself.
    return sorted((form["spelling"], form["description"]) for form in forms)


def changes(old, new):
    before, after = old["options"], new["options"]
    return (
        sorted(after.keys() - before.keys()),
        sorted(before.keys() - after.keys()),
        sorted(key for key in before.keys() & after.keys()
               if signature(before[key]) != signature(after[key])),
    )


def help_lines(forms):
    return [f'{form["spelling"]}  {form["description"]}' for form in forms]


def compare(old, new):
    added, removed, changed = changes(old, new)
    print(f'FFmpeg {old["version"]} -> {new["version"]}')
    print(f"Options: {len(added)} added, {len(removed)} removed, {len(changed)} changed")
    for title, keys, inventory in (
        ("ADDED", added, new), ("REMOVED", removed, old),
    ):
        if keys:
            print(f"\n{title}")
            for key in keys:
                for line in help_lines(inventory["options"][key]):
                    print(f"  {line}")
    if changed:
        print("\nCHANGED (advertised spelling, argument syntax, description, or default hint)")
        for key in changed:
            print(f"  {key}")
            for line in difflib.ndiff(help_lines(old["options"][key]),
                                     help_lines(new["options"][key])):
                if line.startswith(("- ", "+ ")):
                    print(f"    {line}")
    before, after = old["components"], new["components"]
    for kind in sorted(before.keys() | after.keys()):
        added_names = sorted(set(after.get(kind, [])) - set(before.get(kind, [])))
        removed_names = sorted(set(before.get(kind, [])) - set(after.get(kind, [])))
        if added_names or removed_names:
            print(f"\n{kind.upper()}: {len(added_names)} added, {len(removed_names)} removed")
            for prefix, names in (("+", added_names), ("-", removed_names)):
                for name in names:
                    print(f"  {prefix} {name}")
    if old["configure_sha256"] != new["configure_sha256"] and not any((added, removed, changed)):
        print("\nconfigure changed with no advertised option changes; review upstream release notes.")


def render(data, previous=None, include_all=True, selected=None, entries_only=False,
           annotations=None):
    if selected is not None and previous is not None:
        raise ValueError("--pkgbuild and --added-since cannot be combined")
    if "help_order" not in data:
        raise ValueError("inventory has no help order; run ffmpeg-options scan with its matching source tree")
    keys = set(changes(previous, data)[0] if previous else data["options"])
    rows = []
    for spelling in data["help_order"]:
        key = option_key(spelling)
        form = next(form for form in data["options"][key] if form["spelling"] == spelling)
        rows.append((key, form))
    selected_by_key = {}
    for flag in selected or []:
        key = option_key(flag)
        if key not in data["options"]:
            raise ValueError(f"selected option is not advertised: {flag}")
        selected_by_key.setdefault(key, []).append(flag)
    notes = list(annotations or [])

    def take_note(active, flag, allow_related=False):
        for exact in (True, False) if allow_related else (True,):
            for index, note in enumerate(notes):
                if note["active"] != active:
                    continue
                matches = note["flag"] == flag if exact else (
                    option_key(note["flag"]) == option_key(flag)
                    and (base(note["flag"]) == base(flag)
                         or len(data["options"][option_key(flag)]) == 1))
                if matches:
                    return notes.pop(index)
        return None

    def base(flag):
        return flag.split("=", 1)[0].split("[", 1)[0]

    def emit(flag, form, active, note):
        if note:
            headings = set(data.get("sections", [])) | {form["section"]}
            for line in note["leading"]:
                if line.removeprefix("#").strip().removesuffix(":") not in headings:
                    print(f"  {line}")
        if note and note["comment"] is not None:
            comment = note["comment"]
        else:
            description = form["description"]
            if base(flag) != base(form["spelling"]):
                description = f'upstream {form["spelling"]}: {description}'
            comment = f"# {description}"
        prefix = "" if active else "# "
        print(f"  {prefix}{shlex.quote(flag)} {comment}")
    placements = {}
    for key, flags in selected_by_key.items():
        indices = [index for index, (row_key, _) in enumerate(rows) if row_key == key]
        # Prefer the same polarity/argument form. If only the opposite polarity
        # is advertised, substitute the selected flag at that feature's position.
        index = next((index for index in indices
                      if base(rows[index][1]["spelling"]) == base(flags[0])), indices[0])
        placements[index] = flags
    print(f'# FFmpeg {data["version"]}: upstream options for manual review.')
    print("# Uncomment remaining choices to select them; edit NAME/LEVEL placeholders first.")
    print("# Order follows configure --help; existing PKGBUILD comments take precedence.")
    if not entries_only:
        print("_make_options=(")
    section = None
    for index, (key, form) in enumerate(rows):
        if key not in keys:
            continue
        if (not include_all and key not in selected_by_key and not key.startswith("feature:")
                and key != "option:fatal-warnings"):
            continue
        if form["section"] != section:
            section = form["section"]
            print(f"\n  # {section}")
        if index in placements:
            for flag in placements[index]:
                emit(flag, form, True, take_note(True, flag))
        else:
            flag = form["spelling"].replace("[=arg]", "")
            flag = shlex.split(flag)[0]
            note = take_note(False, flag, allow_related=True)
            emit(note["flag"] if note else flag, form, False, note)
    if previous:
        for kind, names in sorted(data["components"].items()):
            added = sorted(set(names) - set(previous["components"].get(kind, [])))
            if added:
                print(f"\n  # Newly available {kind}; upstream defaults are unchanged here.")
                for name in added:
                    print(f"  # --enable-{kind[:-1]}={name}")
    if not entries_only:
        print(")")


def array_body(pkgbuild):
    # Read the array as text; never source/execute the PKGBUILD.
    match = re.search(r"^\s*(?:local\s+)?_make_options\s*=\((.*?)^\s*\)", pkgbuild.read_text(),
                      flags=re.MULTILINE | re.DOTALL)
    if not match:
        raise ValueError(f"cannot find a literal _make_options array in {pkgbuild}")
    return match[1]


def split_comment(line):
    """Find a Bash comment without mistaking quoted/escaped/in-word # for one."""
    quote = None
    escaped = False
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
    return line, None


def selected_flags(pkgbuild):
    body = "\n".join(split_comment(line)[0] for line in array_body(pkgbuild).splitlines())
    tokens = shlex.split(body, comments=False)
    if any(not token.startswith("--") or "$" in token or "`" in token for token in tokens):
        raise ValueError("_make_options contains nonliteral arguments; review those manually")
    return tokens


def pkgbuild_annotations(pkgbuild):
    annotations = []
    pending = []
    for line in array_body(pkgbuild).splitlines():
        line = line.strip()
        if not line:
            pending = []
            continue
        active = not line.startswith("#")
        content = line if active else line.removeprefix("#").lstrip()
        if not content.startswith("--"):
            if not active:
                pending.append(line)
            continue
        code, comment = split_comment(content)
        flags = shlex.split(code, comments=False)
        if any(not flag.startswith("--") for flag in flags):
            raise ValueError("cannot associate PKGBUILD comments with this option line")
        for index, flag in enumerate(flags):
            annotations.append({"flag": flag, "active": active,
                                "comment": comment if index == len(flags) - 1 else None,
                                "leading": pending if index == 0 else []})
        pending = []
    return annotations


def check_selected(data, pkgbuild):
    tokens = selected_flags(pkgbuild)
    missing = []
    for flag in tokens:
        if option_key(flag) not in data["options"]:
            missing.append(f"{flag}: option is not advertised by this release")
            continue
        component = re.fullmatch(r"--(?:enable|disable)-([^=]+)=(.+)", flag)
        if component and (kind := component[1] + "s") in data["components"]:
            for pattern in component[2].split(","):
                if not fnmatch.filter(data["components"][kind], pattern):
                    missing.append(f"{flag}: no {kind} match {pattern!r}")
    if missing:
        raise ValueError("selected options need review:\n  " + "\n  ".join(missing))
    print(f'{len(tokens)} selected flags found in FFmpeg {data["version"]} inventory '
          '(availability check only; dependencies/defaults are not evaluated)')
