"""Shared command-line workflow."""

import argparse
from functools import cmp_to_key
from pathlib import Path
import re
import subprocess
from .alpm import compare_versions
import sys

from . import adapters, language_source
from .analysis import Model, check, read_observed
from .core import VERSION_LABEL, atomic_text, compare, load, record_catalogue, recorded_catalogue, write_snapshot
from .pkgbuild import read_array, scalar
from .policies import intentions, load_config, project, selected_profiles
from .render import render
from .terminal import Terminal, add_color_argument
from .runtime_policy import load_policy
import json


BACKENDS = ("qt6", "ffmpeg", "meson", "cmake", "autoconf", "cargo", "python")


def root_path(directory):
    root = (directory or Path.cwd()).resolve()
    if root.name == "src" and (root.parent / "PKGBUILD").is_file():
        return root.parent
    if root.parent.name == "src" and (root.parent.parent / "PKGBUILD").is_file():
        return root.parent.parent
    return root


def inventory_path(value, root):
    path = Path(value).expanduser()
    if path.exists() or "/" in value or value.endswith((".json", ".json.log")):
        return path
    current = recorded_catalogue(root / "options", value)
    if current:
        return current
    for suffix in (".catalog.json.log", ".json.log", ".json"):
        candidate = root / "options" / (value + suffix)
        if candidate.is_file():
            return candidate
    raise ValueError(f"no inventory for {value}; run review or snapshot first")


def backend_hint(root, config, explicit=None, source=None):
    if explicit:
        return explicit
    configured = config.get("package", {}).get("backend")
    if configured:
        if configured not in BACKENDS:
            raise ValueError(f"unknown package.backend: {configured}")
        return configured
    recipe = root / "PKGBUILD"
    text = recipe.read_text() if recipe.is_file() else ""
    if source and ((source / ".cmake.conf").is_file() or (source / "RELEASE").is_file()):
        return adapters.detect(source)
    arrays = {"meson": "_meson_options", "cmake": "_cmake_options", "autoconf": "_make_options"}
    present = [name for name, array in arrays.items() if re.search(r"\b" + array + r"\s*=\(", text)]
    markers = {"meson": ("meson.build",), "cmake": ("CMakeLists.txt",), "autoconf": ("configure.ac", "configure.in", "configure")}
    return present[0] if len(present) == 1 and (source is None or any((source / marker).is_file() for marker in markers[present[0]])) else None


def source_path(root, config, explicit=None, version=None):
    hint = explicit or config.get("package", {}).get("source")
    if hint:
        path = Path(hint).expanduser()
        if not path.is_absolute():
            matches = sorted(root.glob(hint))
            if len(matches) != 1:
                raise ValueError(f"source hint {hint!r} matches {len(matches)} paths; select --source")
            path = matches[0]
        if not path.is_dir():
            raise ValueError(f"source directory not found: {path}")
        return path
    if not (root / "PKGBUILD").is_file():
        adapters.detect(root)
        return root
    version = version or scalar(root / "PKGBUILD", "pkgver")
    base = root / "src"
    candidates = [path for path in sorted(base.iterdir()) if path.is_dir() and any(
        (path / marker).is_file() for marker in adapters.SOURCE_MARKERS)] if base.is_dir() else []
    if version:
        matching = [path for path in candidates if re.search(r"(?<![0-9])" + re.escape(version) + r"(?![0-9])", path.name)]
        if len(matching) == 1:
            return matching[0]
        if len(matching) > 1:
            raise ValueError(f"multiple source trees for {version}; select --source")
        raise ValueError(f"no source tree for pkgver={version}; obtain/extract that release first, or select --source")
    if len(candidates) != 1:
        raise ValueError("cannot choose a source tree; select --source and --version")
    return candidates[0]


def capture_current(root, config, args):
    explicit = getattr(args, "version", None)
    if explicit and not re.fullmatch(VERSION_LABEL, explicit):
        raise ValueError("invalid release version: " + explicit)
    source = source_path(root, config, getattr(args, "source", None), getattr(args, "version", None))
    data = adapters.capture(source, backend_hint(root, config, getattr(args, "backend", None), source))
    if virtual_workspace(data):
        identify_workspace(data, root.name, 'Package directory')
    declared = scalar(root / "PKGBUILD", "pkgver")
    expected = config.get("package", {}).get("source_version")
    if expected is not None and data["version"] != expected:
        raise ValueError(f"source reports {data['version']}, package.source_version expects {expected}; verify the source mapping")
    if explicit or expected is not None:
        data["source_version"] = data["version"]
        data["version"] = explicit or declared or data["version"]
        data["version_origin"] = ("Explicit --version" if explicit else "Verified package.source_version mapping") + f" {data['source_version']} -> {data['version']}"
        data["notes"].append(data["version_origin"])
    if data["version"] in {"unknown", "undefined"}:
        if not declared:
            raise ValueError("source version is computed; provide --version")
        data["version"] = declared
        data["version_origin"] = "PKGBUILD pkgver; source metadata is computed and was not executed"
        data["notes"].append(data["version_origin"])
    if declared and data["version"] != declared and not getattr(args, "version", None):
        raise ValueError(f"source is {data['version']}, PKGBUILD is {declared}; verify the release or use --version")
    return data


def context(root, config, args, data):
    path = getattr(args, "pkgbuild", None) or root / "PKGBUILD"
    if not path.is_absolute():
        path = root / path
    array = read_array(path, data["backend"], getattr(args, "array", None) or config.get("package", {}).get("array"))
    names = [] if getattr(args, "no_profiles", False) else selected_profiles(config, getattr(args, "profile", None))
    desired = intentions(data, config, names)
    observed = read_observed(args.build, data) if getattr(args, "build", None) else {}
    return array, desired, observed


def previous_catalogue(root, data):
    candidates = []
    for path in (root / "options").glob("*.json*"):
        try:
            candidate = load(path)
        except (OSError, ValueError, KeyError):
            continue
        if (candidate["project"], candidate["backend"]) == (data["project"], data["backend"]) and candidate["version"] != data["version"]:
            candidates.append(candidate)

    def version_compare(left, right):
        return compare_versions(left['version'], right['version'], natural=True)

    older = [candidate for candidate in candidates if version_compare(candidate, data) < 0]
    if not older:
        return None
    previous = sorted(older, key=cmp_to_key(version_compare))[-1]
    # Variant enumeration order says nothing about which evidence is current.
    return load(inventory_path(previous["version"], root))


def virtual_workspace(data):
    return data['backend'] == 'cargo' and not any(
        item['manifest'] == data['cargo']['manifest'] for item in data['cargo']['packages'])


def identify_workspace(data, identity, origin):
    data['source_project'] = data['project']
    data['project'] = identity
    data['project_origin'] = origin + ' for virtual Cargo workspace; native project identity is unavailable'
    data['notes'].append(data['project_origin'])


def source_report(root, config, args, data=None):
    version = getattr(args, 'version', None) or (data['version'] if data else None)
    source = source_path(root, config, getattr(args, 'source', None), version)
    backend = data['backend'] if data else backend_hint(root, config, getattr(args, 'backend', None), source) or adapters.detect(source)
    if selected_profiles(config, getattr(args, 'profile', None)) and not getattr(args, 'no_profiles', False):
        raise ValueError('Cargo/Python source checks use typed metadata; generic flag profiles do not apply')
    if getattr(args, 'build', None):
        raise ValueError('Cargo/Python --build observations are unavailable; use source-check with explicit source selections')
    policy_path = getattr(args, 'policy', None) or Path(__file__).resolve().parents[1] / 'waypure-policy.toml'
    result = language_source.audit(source, backend, load_policy(policy_path),
                                 resolve=getattr(args, 'resolve', False), features=getattr(args, 'features', ()),
                                 no_default_features=getattr(args, 'no_default_features', False),
                                 all_features=getattr(args, 'all_features', False), packages=getattr(args, 'packages', ()),
                                 target=getattr(args, 'target', 'x86_64-unknown-linux-gnu'),
                                 cargo_home=getattr(args, 'cargo_home', None), manifest=getattr(args, 'manifest', None),
                                 extras=getattr(args, 'extras', ()))
    expected = config.get('package', {}).get('source_version') or (
        data.get('source_version', data['version']) if data else scalar(root / 'PKGBUILD', 'pkgver'))
    if expected and result['version'] not in {'unknown', 'undefined', expected}:
        raise ValueError(f'source version is {result["version"]}; expected {expected}')
    if data and data.get('project_origin', '').startswith(('Package directory for virtual Cargo workspace',
                                                          'Explicit --project for virtual Cargo workspace')):
        declarations = result['evidence']['declarations']
        if any(item['manifest'] == declarations['manifest'] for item in declarations['packages']):
            raise ValueError('source now declares a Cargo package; catalogue describes a virtual workspace')
        result['project'] = data['project']
        result['project_origin'] = data['project_origin']
    if data and result['project'] != data['project']:
        raise ValueError(f'source project is {result["project"]}; catalogue is {data["project"]}')
    return result


def review(root, config, args):
    if args.prepare:
        subprocess.run(["makepkg", "--nobuild", "--noprepare", "--nodeps"],
                       cwd=root, stdout=sys.stderr, check=True)
    data = capture_current(root, config, args)
    previous = load(inventory_path(args.old, root)) if args.old else previous_catalogue(root, data)
    baseline, created = record_catalogue(data, root / "options")
    language = data['backend'] in language_source.BACKENDS
    warnings = []
    if language:
        result = source_report(root, config, args, data)
        errors = [item['message'] for item in result['findings'] if item['severity'] == 'error']
        notes = [item['message'] for item in result['findings'] if item['severity'] == 'note']
        warnings = [item['message'] for item in result['findings'] if item['severity'] == 'warning']
        output = root / 'options' / (data['version'] + '.source.json.log')
        atomic_text(output, json.dumps(result, indent=2) + '\n')
    else:
        array, desired, observed = context(root, config, args, data)
        errors, notes = check(data, array, desired, observed, strict=args.strict)
        valid_intentions = {name: item for name, item in desired.items() if name in data["options"]}
        proposed = project(array, valid_intentions, data)
        output = root / "options" / (data["version"] + ".options.sh.log")
        atomic_text(output, render(data, proposed, prune=True, include_all=args.all, observed=observed))
    report = [f"{data['project']} {data['version']} packaging review",
              f"Source catalogue: {baseline.name} ({'created' if created else 'unchanged'})",
              compare(previous, data, args.all).rstrip() if previous else "First catalogue: no older release recorded."]
    report.extend("ERROR " + error for error in errors)
    report.extend("NOTE " + note for note in notes)
    report.extend('WARNING ' + warning for warning in warnings)
    if args.arch or args.arch_local or args.offline:
        from .arch import review_arch
        try:
            arch_lines, arch_errors = review_arch(root, config, data["version"], offline=args.offline, local=args.arch_local)
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            message = "Arch comparison unavailable: " + str(error)
            arch_lines, arch_errors = ["ERROR " + message], [message]
        report.extend(arch_lines)
        errors.extend(arch_errors)
    report.extend([f"{'Source evidence' if language else 'Proposed array'}: {output.name}",
                   f"Result: {len(errors)} error(s); the PKGBUILD has not been rewritten.",
                   "Review the proposal, regenerate .SRCINFO after recipe edits, then build last."])
    report_path = root / "options" / (data["version"] + ".review.log")
    atomic_text(report_path, "\n".join(report) + "\n")
    print(Terminal(args.color).report("\n".join(report)))
    print("Report:", report_path)
    print('Source evidence:' if language else 'Array:', output)
    return bool(errors or args.strict and warnings)


def main():
    parser = argparse.ArgumentParser(description="Review release options, named policies and official Arch packaging.")
    parser.add_argument("-C", "--directory", type=Path, help="package directory (default: current directory)")
    add_color_argument(parser)
    parser.set_defaults(source=None, backend=None, version=None, all=False, old=None, arch=False,
                        arch_local=None, offline=False, pkgbuild=None, array=None, profile=None,
                        no_profiles=False, build=None, strict=False, prepare=False)
    commands = parser.add_subparsers(dest="command")

    def source_arguments(command):
        command.add_argument("--backend", choices=BACKENDS)
        command.add_argument("--version", help="explicit release version when source metadata is computed")

    def selection_arguments(command):
        command.add_argument("--pkgbuild", type=Path)
        command.add_argument("--array", help="override the packaging array name")
        command.add_argument("--profile", action="append", help="select a named group; repeat to combine")
        command.add_argument("--no-profiles", action="store_true", help="inspect the recipe without configured policies")
        command.add_argument("--build", type=Path, help="read an existing build's observations separately")

    snapshot = commands.add_parser("snapshot", help="capture one release without building")
    snapshot.add_argument("source", type=Path)
    snapshot.add_argument("-o", "--output", type=Path, required=True)
    snapshot.add_argument('--project', help='explicit stable identity for a virtual Cargo workspace')
    source_arguments(snapshot)
    diff = commands.add_parser("diff", help="compare release option catalogues")
    diff.add_argument("old")
    diff.add_argument("new")
    diff.add_argument("--all", action="store_true", help="include setup/platform options")
    for name in ("render", "check", "explain"):
        command = commands.add_parser(name)
        add_color_argument(command, default=argparse.SUPPRESS)
        command.add_argument("inventory", help="version or snapshot path; legacy Qt/FFmpeg snapshots are accepted")
        selection_arguments(command)
        if name == "render":
            command.add_argument("--all", action="store_true")
            command.add_argument("--prune-redundant", action="store_true")
            command.add_argument("--entries-only", action="store_true")
            command.add_argument("-o", "--output", type=Path)
        if name == "explain":
            command.add_argument("option")
        if name == "check":
            command.add_argument("--strict", action="store_true", help="fail on selected deprecated options")
    reviewer = commands.add_parser("review", help="write a release diff, policy review and copyable .sh.log array")
    reviewer.add_argument("--source", type=Path)
    reviewer.add_argument("--old", help="explicit previous catalogue/version")
    reviewer.add_argument("--all", action="store_true")
    reviewer.add_argument("--arch", action="store_true", help="fetch and compare official Arch packaging")
    reviewer.add_argument("--arch-local", type=Path, help="read a local Arch packaging Git repository")
    reviewer.add_argument("--offline", action="store_true", help="use the recorded Arch commit; never claim it is current upstream")
    reviewer.add_argument("--prepare", action="store_true", help="run makepkg to verify/extract sources, skipping prepare and compilation")
    reviewer.add_argument("--strict", action="store_true", help="fail on selected deprecated options")
    source_arguments(reviewer)
    selection_arguments(reviewer)
    source_checker = commands.add_parser('source-check', help='inspect Cargo/Python declarations and optional frozen Cargo resolver evidence')
    source_checker.add_argument('--source', type=Path)
    source_arguments(source_checker)
    source_checker.add_argument('--json', action='store_true')
    source_checker.add_argument('--strict', action='store_true', help='also fail incomplete/conditional source evidence warnings')
    source_checker.add_argument('--policy', type=Path)
    source_checker.add_argument('--resolve', action='store_true', help='run frozen offline Cargo metadata; executes no build scripts')
    source_checker.add_argument('--features', action='append', default=[], help='Cargo feature or comma-separated features; repeat to combine')
    source_checker.add_argument('--no-default-features', action='store_true')
    source_checker.add_argument('--all-features', action='store_true')
    source_checker.add_argument('--package', dest='packages', action='append', default=[], help='selected Cargo workspace root; repeat for multiple roots')
    source_checker.add_argument('--target', default='x86_64-unknown-linux-gnu')
    source_checker.add_argument('--cargo-home', type=Path, help='existing Cargo dependency cache; no downloads')
    source_checker.add_argument('--manifest', type=Path, help='Cargo manifest within the selected source tree')
    source_checker.add_argument('--extra', dest='extras', action='append', default=[], help='selected Python extra; repeat to combine')
    arch = commands.add_parser("arch", help="snapshot or compare official Arch packaging at Git commits")
    arch_commands = arch.add_subparsers(dest="arch_command", required=True)
    arch_snapshot = arch_commands.add_parser("snapshot")
    arch_snapshot.add_argument("--pkgbase", required=True)
    arch_snapshot.add_argument("--local", type=Path, help="read an existing packaging Git repository")
    arch_snapshot.add_argument("--ref", help="Git ref; defaults to main remotely or HEAD locally")
    arch_snapshot.add_argument("-o", "--output", type=Path, required=True)
    arch_diff = arch_commands.add_parser("diff")
    arch_diff.add_argument("old", type=Path)
    arch_diff.add_argument("new", type=Path)
    for command in (snapshot, diff, reviewer, source_checker, arch, arch_snapshot, arch_diff):
        add_color_argument(command, default=argparse.SUPPRESS)
    args = parser.parse_args()
    terminal = Terminal(args.color)
    try:
        root = root_path(args.directory)
        config = load_config(root)
        if args.command == "arch":
            from . import arch as arch_backend
            if args.arch_command == "snapshot":
                data = arch_backend.snapshot(args.pkgbase, args.local, args.ref)
                write_snapshot(data, args.output)
                print(f"{args.output}: Arch {data['pkgbase']} {data['version']} at {data['commit']}")
            else:
                print(terminal.report(arch_backend.compare(arch_backend.load(args.old), arch_backend.load(args.new))), end="")
        elif args.command == 'source-check':
            result = source_report(root, config, args)
            if args.json:
                print(json.dumps(result, indent=2))
            else:
                print(f'{result["project"]} {result["version"]}: {result["backend"]} source evidence')
                for item in result['findings']:
                    print(f'{item["severity"].upper()} [{item["code"]}] {item["message"]}')
            return int(any(item['severity'] == 'error' or args.strict and item['severity'] == 'warning' for item in result['findings']))
        elif args.command == "snapshot":
            data = adapters.capture(args.source, args.backend, args.version)
            if args.project:
                if not virtual_workspace(data):
                    raise ValueError('--project applies to virtual Cargo workspace snapshots')
                identify_workspace(data, args.project, 'Explicit --project')
            changed = write_snapshot(data, args.output)
            print(f"{args.output}: {data['project']} {data['version']}, {len(data['options'])} options ({'created' if changed else 'unchanged'})")
        elif args.command == "diff":
            print(terminal.report(compare(load(inventory_path(args.old, root)), load(inventory_path(args.new, root)), args.all)), end="")
        elif args.command in {"render", "check", "explain"}:
            data = load(inventory_path(args.inventory, root))
            if data['backend'] in language_source.BACKENDS:
                if args.command != 'check':
                    raise ValueError('Cargo/Python catalogues contain typed metadata; use source-check, not configure option arrays')
                result = source_report(root, config, args, data)
                for item in result['findings']:
                    print(f'{item["severity"].upper()} [{item["code"]}] {item["message"]}')
                return int(any(item['severity'] == 'error' or args.strict and item['severity'] == 'warning' for item in result['findings']))
            array, desired, observed = context(root, config, args, data)
            if args.command == "render":
                unknown = [name for name in desired if name not in data["options"]]
                if unknown:
                    raise ValueError("profile options absent from this release: " + ", ".join(unknown))
                text = render(data, project(array, desired, data), args.prune_redundant, args.all, observed, args.entries_only)
                if args.output:
                    atomic_text(args.output, text)
                    print(args.output)
                else:
                    print(text, end="")
            elif args.command == "check":
                errors, notes = check(data, array, desired, observed, strict=args.strict)
                for message in errors:
                    print(terminal.paint("ERROR", "ERROR") + " " + message)
                for message in notes:
                    print(terminal.paint("NOTE", "NOTE") + " " + message)
                print(terminal.paint(f"{data['project']} {data['version']}: {len(errors)} error(s), {len(notes)} note(s)",
                                     "FAIL" if errors else "PASS"))
                return int(bool(errors))
            else:
                if args.option not in data["options"]:
                    raise ValueError("option not found: " + args.option)
                state = Model(data, array.active, ignore=args.option, defaults=array.defaults, uncertain=array.uncertain).state(args.option)
                print(f"{args.option}: without its explicit flag = {state.value or 'UNKNOWN'}")
                print("Source reasons:", "; ".join(state.reasons) or "declared default or unresolved source logic")
                rules = data["options"][args.option]["rules"]
                print("Rules:", {name: value for name, value in rules.items() if name not in {'autoconf_program', 'autoconf_rule_hash'}})
                if args.option in observed:
                    print("Observed build value:", observed[args.option])
        else:
            return int(review(root, config, args))
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        print(Terminal(args.color, sys.stderr).paint(Path(sys.argv[0]).name + ':', "ERROR") + " " + str(error), file=sys.stderr)
        return 1
    return 0
