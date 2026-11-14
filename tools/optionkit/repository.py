"""Read-only repository audit, using shared option and build metadata helpers."""

import argparse
from functools import cmp_to_key
import json
from pathlib import Path
import re
import subprocess

from . import adapters, arch, language_source
from .alpm import BackendUnavailable, OFFICIAL_REPOSITORIES, compare_versions, get_backend, reset_backend
from .analysis import check
from .artifacts import audit_artifact
from .cli import backend_hint, inventory_path, source_path
from .core import digest, load
from .configured_policy import audit_build
from .dependencies import bare_name
from .freshness import newer_inputs, package_artifacts
from .installed_policy import InstalledGraph
from .local_policy import LocalGraph
from .metadata import SrcinfoError, entry_values, fields, find_pkgbuild_dirs, parse_srcinfo_text
from .literals import array_body, words
from .linting import run_namcap
from .pkgbuild import LiteralRecipe, read_array
from .policies import intentions, load_config, selected_profiles
from .runtime_policy import load_policy
from .terminal import Terminal, add_color_argument


def sync_versions(names=None):
    try:
        return get_backend().sync_versions(repositories=OFFICIAL_REPOSITORIES, names=names)
    except BackendUnavailable:
        return {}


def recipe_arrays(recipe):
    text, bindings = recipe.text, recipe.bindings
    pattern = (r"(?m)^(?P<indent>[ \t]*)(?:local\s+)?(?P<key>depends|makedepends|checkdepends|optdepends|provides|conflicts|sha256sums|license|source|arch|replaces)"
               r"(?P<arch>_x86_64)?(?P<index>\[[^\]\n]*\])?[ \t]*(?P<operation>\+?=)")
    declarations = {}
    for match in re.finditer(pattern, text):
        name = match['key'] + (match['arch'] or "")
        array = array_body(text[match.end():]) if not match['index'] else None
        values = None
        if array is not None:
            closing = match.end() + len(array) + 2
            newline = text.find("\n", closing)
            tail = text[closing:] if newline < 0 else text[closing:newline]
            if words(tail) == []:
                values = words(array, lambda reference: bindings.get(reference, match.start()))
        certain = (values is not None and match.start() < bindings.scope_limit and not match['indent']
                   and match['operation'] == "=" and bindings.unique(name))
        declarations.setdefault(name, []).append((values, certain))
    for name, records in declarations.items():
        values, certain = records[-1]
        yield name, values, certain and len(records) == 1


def audit_language(root, config, version, policy, row, finding, build):
    try:
        source = source_path(root, config, version=version)
        backend = backend_hint(root, config, source=source) or adapters.detect(source)
    except (OSError, ValueError) as error:
        try:
            cached = load(inventory_path(version, root))
        except (OSError, ValueError):
            return False
        if cached['backend'] not in language_source.BACKENDS:
            return False
        row['checks']['options'] = 'SKIP'
        row['checks']['source'] = 'MISSING'
        finding('warning', 'source.missing', f'{cached["backend"]} catalogue exists, but current source cannot be checked: {error}')
        if build is not None:
            finding('error', 'build.unavailable', 'Cargo/Python configured builds require package-specific evidence; use source-check')
            row['checks']['build'] = 'FAIL'
        return True
    if backend not in language_source.BACKENDS:
        return False
    row['checks']['options'] = 'SKIP'
    if build is not None:
        finding('error', 'build.unavailable', 'Cargo/Python configured builds require package-specific evidence; use source-check')
        row['checks']['build'] = 'FAIL'
    try:
        result = language_source.audit(source, backend, policy)
        expected = config.get('package', {}).get('source_version', version)
        if result['version'] not in {'unknown', 'undefined', expected}:
            finding('error', 'source.version', f'Source reports {result["version"]}; expected {expected}')
        row['source'] = result
        row['findings'].extend(result['findings'])
        relevant = [item for item in row['findings'] if item['code'].startswith(('source.', 'cargo.', 'python.'))]
        levels = {item['severity'] for item in relevant}
        row['checks']['source'] = 'FAIL' if 'error' in levels else 'WARN' if 'warning' in levels else 'PASS'
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        row['checks']['source'] = 'FAIL'
        finding('error', 'source.unavailable', str(error))
    return True


def audit_package(root, policy, infos, sync, graph, recipe, artifacts=False, metadata_only=False, namcap=False, installed=None, build=None):
    row = {"package": root.name, "version": "unknown", "status": "PASS",
           "checks": {name: "SKIP" for name in ("recipe", "options", "arch", "artifacts", "namcap", "installed", "build", "source")},
           "findings": [], "artifacts": []}

    def finding(severity, code, message):
        row["findings"].append({"severity": severity, "code": code, "message": message})

    def finish():
        levels = {item["severity"] for item in row["findings"]}
        row["status"] = "FAIL" if "error" in levels else "WARN" if "warning" in levels else "PASS"
        return row

    def lint(path):
        result = run_namcap(path)
        row['findings'].extend(result['findings'])
        ranks = {'SKIP': -1, 'PASS': 0, 'WARN': 1, 'MISSING': 2, 'FAIL': 3}
        if ranks[result['status']] > ranks[row['checks']['namcap']]:
            row['checks']['namcap'] = result['status']

    if namcap:
        lint(root/'PKGBUILD')

    if root not in infos:
        finding("error", "srcinfo.missing", "Missing or invalid .SRCINFO; regenerate it with makepkg --printsrcinfo")
        row["checks"]["recipe"] = "FAIL"
        return finish()
    info, metadata = infos[root], fields(infos[root].source_text)
    row["version"] = info.version
    for key in ("pkgver", "pkgrel", "epoch"):
        actual = recipe.scalar(key)
        if key == "epoch" and not re.search(r"(?m)^\s*epoch\s*=", recipe.text):
            actual = "0"
        recorded = metadata.get(key, ["0" if key == "epoch" else ""])[0]
        if actual is not None and actual != recorded:
            finding("error", "srcinfo.version", f"{key}: PKGBUILD={actual}, .SRCINFO={recorded}")
        elif actual is None:
            finding("warning", "srcinfo.dynamic", f"{key} is computed; only recorded .SRCINFO can be inspected")
    outputs = graph.outputs[root]
    for severity, kind, key, value in policy.metadata(metadata):
        finding(severity, f'dependency.{kind}', f'.SRCINFO {key}: {value}')
    name = recipe.scalar('pkgname')
    names = [name] if name is not None else None
    text = recipe.text
    declaration = re.search(r"(?m)^pkgname=", text)
    bindings = recipe.bindings
    if names is None and declaration and bindings.unique('pkgname') and declaration.start() < bindings.scope_limit:
        body = array_body(text[declaration.end():])
        if body is not None:
            closing = declaration.end() + len(body) + 2
            newline = text.find('\n', closing)
            if words(text[closing:newline if newline >= 0 else len(text)]) == []:
                names = words(body, lambda reference: bindings.get(reference, declaration.start()))
    base = recipe.scalar('pkgbase')
    if base is None and not re.search(r"(?m)^\s*pkgbase\s*=", text):
        base = names[0] if names else None
    identities = []
    recorded_base = metadata.get("pkgbase", [""])[0]
    if base is not None and base != recorded_base:
        identities.append(f"pkgbase: recipe={base}, .SRCINFO={recorded_base}")
    if names is not None and set(outputs) != set(names):
        identities.append(f"pkgname: recipe={names}, .SRCINFO={sorted(outputs)}")
    if identities:
        finding("error", "srcinfo.identity", "; ".join(identities))
    for severity, kind, key, value in policy.metadata({'pkgname': names or []}):
        finding(severity, f'dependency.{kind}', f'Recipe {key}: {value}')
    base_text = re.split(r"(?m)^\s*pkgname\s*=", info.source_text, maxsplit=1)[0]
    comparable = next(iter(outputs.values())) if len(outputs) == 1 else fields(base_text)
    for key, values, certain in recipe_arrays(recipe):
        if not certain:
            finding("warning", "srcinfo.dynamic", f"{key} has computed/scoped values or mutations; verify generated .SRCINFO before building")
            continue
        recorded = [value for value in comparable.get(key, []) if value]
        ordered = key.startswith(('source', 'sha256sums'))
        different = values != recorded if ordered else sorted(values) != sorted(recorded)
        if different:
            finding("error", "srcinfo.fields", f"{key}: recipe={values}, .SRCINFO={recorded}")
        for severity, kind, field, value in policy.metadata({key: values}):
            finding(severity, f'dependency.{kind}', f'Recipe {field}: {value}')
    checksum_fields = {name for name in metadata if name == 'sha256sums' or name.startswith('sha256sums_')}
    source_fields = {name for name in metadata if name == 'source' or name.startswith('source_')}
    checksums = [value for name in checksum_fields for value in metadata[name]]
    for name in sorted(source_fields | {name.replace('sha256sums', 'source', 1) for name in checksum_fields}):
        sources = [value for value in metadata.get(name, []) if value]
        hashes = [value for value in metadata.get(name.replace('source', 'sha256sums', 1), []) if value]
        if len(sources) != len(hashes):
            finding('error', 'source.checksums', f'{name}: {len(sources)} source(s), {len(hashes)} SHA256 checksum(s); counts must match')
    if source_fields and not checksums:
        finding("error", "source.sha256", "No SHA256 checksums recorded")
    for checksum in checksums:
        if checksum == "SKIP" and re.search(r"(?:^|[.\-])rc\d*", metadata["pkgver"][0], re.I):
            continue
        if not re.fullmatch(r"[0-9a-fA-F]{64}", checksum):
            finding("error", "source.sha256", f"Invalid SHA256 checksum: {checksum}")
    for dependency in info.depends + info.makedepends + info.checkdepends:
        name = bare_name(dependency)
        if name.endswith("-waypure"):
            if name not in info.pkgnames or dependency != name + "=" + info.version:
                finding("error", "dependency.pin", f"{dependency}: use the official name; only exact versioned sibling pins are allowed")
    for name in info.pkgnames:
        if name.endswith("-waypure"):
            entry = outputs[name]
            provides = {bare_name(value) for value in entry.get("provides", [])}
            conflicts = {bare_name(value) for value in entry.get("conflicts", [])}
            if not any(not value.endswith((".so", "-waypure")) for value in provides & conflicts):
                finding("error", "package.replacement", f"{name} needs matching provides/conflicts for its official package")
    paths, uncertain = graph.paths(root, policy)
    for path, severity in paths:
        finding(severity, "dependency.path", path)
    for message in uncertain:
        finding("warning", "dependency.provider", message)
    levels = {item['severity'] for item in row['findings']}
    row['checks']['recipe'] = 'FAIL' if 'error' in levels else 'WARN' if 'warning' in levels else 'PASS'
    if installed is not None:
        dependencies = sorted({value for entry in outputs.values() for value in entry_values(entry, 'depends')})
        paths, uncertain = installed.paths(root.name, dependencies, policy)
        for path, severity in paths:
            finding(severity, 'installed.path', path)
        for message in uncertain:
            finding('warning', 'installed.provider', message)
        row['checks']['installed'] = ('FAIL' if any(severity == 'error' for _, severity in paths)
                                      else 'WARN' if paths or uncertain else 'PASS')
        finding('note', 'installed.scope', 'Runtime closure from the cached installed ALPM snapshot; separate from local recipe plans')
    try:
        config = load_config(root)
        version = recipe.scalar('pkgver') or metadata['pkgver'][0]
        if not metadata_only and not audit_language(root, config, version, policy, row, finding, build):
            try:
                data = load(inventory_path(version, root))
            except (OSError, ValueError, KeyError) as error:
                if build is not None:
                    row['checks']['build'] = 'FAIL'
                    finding('error', 'build.unavailable', f'Cannot audit configured build without the matching release catalogue: {error}')
                row["checks"]["options"] = "MISSING"
                unsupported = None
                try:
                    source = source_path(root, config, version=version)
                    adapters.detect(source)
                except adapters.UnsupportedBackend as exception:
                    unsupported = str(exception)
                except (OSError, ValueError):
                    pass
                if unsupported:
                    row["checks"]["options"] = "SKIP"
                    finding("warning", "options.unsupported", unsupported)
                else:
                    finding("warning", "options.missing", f"{error}; run waypure-options review --prepare")
            else:
                array = read_array(root / "PKGBUILD", data["backend"], config.get("package", {}).get("array"))
                desired = intentions(data, config, selected_profiles(config))
                if build is not None:
                    try:
                        result = audit_build(build, data, array.active, desired, policy)
                    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
                        row['checks']['build'] = 'FAIL'
                        finding('error', 'build.unavailable', str(error))
                    else:
                        row['build'] = result
                        row['findings'].extend(result['findings'])
                        levels = {item['severity'] for item in result['findings']}
                        row['checks']['build'] = 'FAIL' if 'error' in levels else 'WARN' if 'warning' in levels else 'PASS'
                errors, notes = check(data, array, desired)
                for message in errors:
                    finding("error", "options.invalid", message)
                for message in notes:
                    finding("warning" if any(marker in message for marker in (": deprecated:", ": unadvertised")) else "note", "options.note", message)
                row["checks"]["options"] = "FAIL" if errors else "WARN" if any(marker in note for note in notes for marker in (": deprecated:", ": unadvertised")) else "PASS"
                try:
                    source = source_path(root, config, version=version).resolve()
                except ValueError:
                    if not errors:
                        row["checks"]["options"] = "CACHED"
                    finding("note", "options.cached", "Using recorded source catalogue; extracted source is unavailable or ambiguous")
                else:
                    for name, expected in data.get("sources", {}).items():
                        path = (source / name).resolve()
                        if not path.is_relative_to(source) or not path.is_file() or digest(path.read_text()) != expected:
                            finding("warning", "options.stale", f"Source evidence changed: {name}; rerun waypure-options review")
                            if not errors:
                                row["checks"]["options"] = "STALE"
                            break
        base = arch.pkgbase(root, config, recipe)
        receipt = root / "options/arch/current.json.log"
        observations = []
        if receipt.is_file():
            snapshot = arch.recorded_snapshot(receipt, receipt.parent)
            if snapshot["pkgbase"] != base:
                raise ValueError(f"recorded Arch pkgbase {snapshot['pkgbase']} differs from {base}")
            observations.append({"version": snapshot["version"], "origin": "recorded Git commit " + snapshot["commit"][:12]})
        if base in sync:
            observations.append({"version": sync[base], "origin": "cached official sync database"})
        row["arch_observations"] = observations
        if observations:
            try:
                newest = max(observations, key=cmp_to_key(lambda left, right: compare_versions(left["version"], right["version"])))
            except BackendUnavailable:
                newest = observations[-1]
            official, origin = newest["version"], newest["origin"]
            disagreement = len({item["version"] for item in observations}) > 1
            if disagreement:
                finding("warning", "arch.evidence", "Cached observations differ: " + "; ".join(item["version"] + " (" + item["origin"] + ")" for item in observations))
            row["arch_version"], row["arch_origin"] = official, origin
            before, after = info.version.rsplit("-", 1)[0], official.rsplit("-", 1)[0]
            local_epoch = before.split(":", 1)[0] if ":" in before else "0"
            arch_epoch = after.split(":", 1)[0] if ":" in after else "0"
            mismatch = False
            if local_epoch != arch_epoch:
                finding("error", "arch.epoch", f"Epoch differs: local={local_epoch}, Arch={arch_epoch} ({origin})")
            elif before != after:
                try:
                    mismatch = bool(compare_versions(before, after))
                except BackendUnavailable:
                    mismatch = True
                    finding("warning", "arch.version", f"Version strings differ; ALPM comparison unavailable: {before}, {after}")
                else:
                    if mismatch:
                        finding("warning", "arch.version", f"Release differs: local={before}, Arch={after} ({origin})")
            row["checks"]["arch"] = "FAIL" if local_epoch != arch_epoch else "WARN" if mismatch or disagreement else "CACHED"
        else:
            row["checks"]["arch"] = "MISSING"
            finding("warning", "arch.missing", "No recorded Arch commit or official sync-database version")
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        finding("error", "review.unavailable", str(error))
    if artifacts:
        for name in info.pkgnames:
            candidates = package_artifacts(root, name, info.version)
            if not candidates:
                finding("warning", "artifact.missing", f"No archive for {name} {info.version}")
                continue
            path = max(candidates, key=lambda candidate: candidate.stat().st_mtime_ns)
            result = audit_artifact(path, policy)
            row["artifacts"].append(result)
            row["findings"].extend(result["findings"])
            if namcap:
                lint(path)
            if result["pkgname"] != name or result["pkgver"] != info.version:
                finding("error", "artifact.version", f"{path.name}: archive identity/version differs from .SRCINFO")
            newer = newer_inputs(root, path)
            if newer:
                finding('warning', 'artifact.stale', f'{path.name}: newer input(s): ' + ', '.join(newer))
        relevant = [item for item in row["findings"] if item["code"].startswith(("artifact.", "archive."))]
        row["checks"]["artifacts"] = ("FAIL" if any(item["severity"] == "error" for item in relevant)
                                      else "MISSING" if any(item['code'] == 'artifact.missing' for item in relevant)
                                      else "STALE" if any(item['code'] == 'artifact.stale' for item in relevant)
                                      else "WARN" if relevant else "PASS")
    return finish()


def main():
    parser = argparse.ArgumentParser(description="Audit recipes, release options and package archives. Default checks do not execute recipes; --namcap evaluates trusted inputs.")
    parser.add_argument("packages", nargs="*", help="package directories to audit (default: all)")
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--policy", type=Path, default=Path(__file__).resolve().parents[1] / "waypure-policy.toml")
    parser.add_argument("--json", action="store_true", help="emit a machine-readable report")
    parser.add_argument("--strict", action="store_true", help="also return failure for warnings/missing evidence")
    parser.add_argument("--metadata-only", action="store_true", help="skip source-option checks")
    parser.add_argument("--artifacts", action="store_true", help="inspect current-version archives and ELF dependencies")
    parser.add_argument('--namcap', action='store_true', help='also run namcap on trusted recipes and selected archives; evaluates recipes and may invoke ldd')
    parser.add_argument('--installed-deps', action='store_true', help='also trace actual installed runtime providers through cached native ALPM metadata (requires pyalpm)')
    parser.add_argument('--build', type=Path, help='audit existing Meson/CMake configured evidence for exactly one selected package')
    parser.add_argument("-v", "--verbose", action="store_true", help="include explanatory notes")
    add_color_argument(parser)
    args = parser.parse_args()
    reset_backend()
    try:
        repo = args.repo.resolve()
        directories = [path for path in find_pkgbuild_dirs(repo) if not path.is_symlink()]
        selected = set(args.packages) or {path.name for path in directories}
        unknown = selected - {path.name for path in directories}
        if unknown:
            raise ValueError("unknown package directories: " + ", ".join(sorted(unknown)))
        if not selected:
            raise ValueError("no package directories found")
        if args.build is not None and (len(selected) != 1 or args.metadata_only):
            raise ValueError('--build requires one package and cannot be combined with --metadata-only')
        policy, infos = load_policy(args.policy), {}
        for path in directories:
            try:
                infos[path] = parse_srcinfo_text((path / ".SRCINFO").read_text(), path)
            except (OSError, ValueError, SrcinfoError):
                continue
        graph = LocalGraph(infos)
        recipes = {path: LiteralRecipe.read(path / 'PKGBUILD') for path in directories if path.name in selected and path in infos}
        names = set()
        for path in directories:
            if path.name in selected and path in infos:
                try:
                    names.add(arch.pkgbase(path, load_config(path), recipes[path]))
                except (OSError, ValueError):
                    # The package audit reports invalid configuration itself.
                    continue
        sync = sync_versions(names=names)
        installed = InstalledGraph(get_backend().installed_runtime()) if args.installed_deps else None
        rows = [audit_package(path, policy, infos, sync, graph, recipes.get(path), args.artifacts, args.metadata_only, args.namcap, installed, args.build)
                for path in directories if path.name in selected]
        findings = [finding for row in rows for finding in row["findings"]]
        summary = {level + "s": sum(item["severity"] == level for item in findings) for level in ("error", "warning", "note")}
        report = {"format": "waypure-audit-1", "repo": str(repo), "packages": rows, "summary": summary,
                  "scope": "Recorded metadata/catalogues, available static Cargo/Python source evidence and local runtime dependency paths; Arch evidence is cached. Installed runtime evidence, configured-build evidence, archive/ELF inspection and native namcap are opt-in. Namcap evaluates recipes and may invoke ldd."}
        if args.json:
            print(json.dumps(report, indent=2))
        else:
            terminal = Terminal(args.color)
            print(terminal.paint(f"{'PACKAGE':<25} {'VERSION':<20} {'RECIPE':<9} {'OPTIONS':<9} {'ARCH':<9} {'ARTIFACTS':<10} {'NAMCAP':<9} {'INSTALLED':<10} {'BUILD':<9} {'SOURCE':<9} RESULT", "HEADER"))
            for row in rows:
                cells = row["checks"]
                statuses = " ".join(terminal.cell(cells[name], width) for name, width in
                                    (("recipe", 9), ("options", 9), ("arch", 9), ("artifacts", 10), ('namcap', 9), ('installed', 10), ('build', 9), ('source', 9)))
                print(f"{row['package']:<25} {row['version']:<20} {statuses} {terminal.cell(row['status'])}")
                for finding in row["findings"]:
                    if finding["severity"] != "note" or args.verbose:
                        label = finding['severity'].upper()
                        print(f"  {terminal.paint(label, label)} [{finding['code']}] {finding['message']}")
            errors = terminal.paint(summary["errors"], "ERROR" if summary["errors"] else "PASS")
            warnings = terminal.paint(summary["warnings"], "WARNING" if summary["warnings"] else "PASS")
            scope = 'native namcap enabled.' if args.namcap else 'no network reads.'
            print(f"{len(rows)} packages; {errors} errors, {warnings} warnings. Arch evidence is cached; {scope}")
        return int(bool(summary["errors"] or args.strict and summary["warnings"]))
    except (OSError, ValueError, subprocess.SubprocessError, SrcinfoError) as error:
        parser.exit(2, "waypure-audit: " + str(error) + "\n")
