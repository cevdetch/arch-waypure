"""Read official Arch packaging at immutable Git commits; never source recipes."""

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import tempfile

from .core import atomic_text, text_diff, write_snapshot
from .pkgbuild import LiteralRecipe, scalar


FORMAT = "waypure-arch-1"
COMMIT = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")


def git(repository, *arguments, binary=False):
    result = subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "-c", "protocol.ext.allow=never",
                             "-C", str(repository), *arguments], capture_output=True,
                            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"}, timeout=60)
    if result.returncode:
        raise ValueError("Git read failed: " + result.stderr.decode(errors="replace").strip())
    return result.stdout if binary else result.stdout.decode(errors="replace")


def metadata(files, pkgbase):
    info = {}
    text = files.get(".SRCINFO", {}).get("text", "")
    for line in text.splitlines():
        match = re.match(r"\s*(pkgbase|epoch|pkgver|pkgrel)\s*=\s*(.*)", line)
        if match:
            info[match[1]] = match[2]
    recipe = files.get("PKGBUILD", {}).get("text", "")
    for name in ("pkgbase", "epoch", "pkgver", "pkgrel"):
        if name in info:
            continue
        match = re.search(r"^\s*" + name + r"\s*=\s*([^\n]+)", recipe, re.MULTILINE)
        if match:
            try:
                values = shlex.split(match[1], comments=True)
            except ValueError:
                continue
            if len(values) == 1 and not any(character in values[0] for character in "$`()"):
                info[name] = values[0]
    base = info.get("pkgbase", pkgbase)
    if base != pkgbase:
        raise ValueError(f"Arch repository pkgbase is {base}, requested {pkgbase}")
    epoch = info.get("epoch", "0")
    version = info.get("pkgver", "unknown") + "-" + info.get("pkgrel", "unknown")
    if epoch != "0":
        version = epoch + ":" + version
    return info, version


def local_snapshot(repository, pkgbase, ref="HEAD", origin=None):
    if not re.fullmatch(r"[a-z0-9][a-z0-9@+_.-]*", pkgbase):
        raise ValueError("invalid Arch pkgbase name")
    if not ref or ref.startswith("-") or "\x00" in ref:
        raise ValueError("invalid Git ref")
    commit = git(repository, "rev-parse", "--verify", "--end-of-options", ref + "^{commit}").strip()
    if not COMMIT.fullmatch(commit):
        raise ValueError("Git did not resolve an immutable commit")
    files, total = {}, 0
    for record in git(repository, "ls-tree", "-rz", "--full-tree", commit, binary=True).split(b"\0"):
        if not record:
            continue
        header, path = record.split(b"\t", 1)
        mode, kind, object_id = header.decode().split()
        name = path.decode(errors="surrogateescape")
        if kind != "blob":
            continue
        item = {"git_oid": object_id, "mode": mode}
        size = int(git(repository, "cat-file", "-s", object_id))
        if size <= 2 * 1024 * 1024 and total + size <= 32 * 1024 * 1024:
            content = git(repository, "cat-file", "blob", object_id, binary=True)
            total += len(content)
            try:
                if b"\0" not in content:
                    item["text"] = content.decode()
            except UnicodeDecodeError:
                pass
        files[name] = item
    if "PKGBUILD" not in files or "text" not in files["PKGBUILD"]:
        raise ValueError("Arch snapshot lacks a readable PKGBUILD")
    info, version = metadata(files, pkgbase)
    return {"format": FORMAT, "pkgbase": pkgbase, "commit": commit,
            "source": origin or str(Path(repository).resolve()),
            "commit_date": git(repository, "show", "-s", "--format=%cI", commit).strip(),
            "metadata": info, "version": version, "files": files}


def snapshot(pkgbase, local=None, ref=None):
    if local:
        return local_snapshot(local, pkgbase, ref or "HEAD")
    if not re.fullmatch(r"[a-z0-9][a-z0-9@+_.-]*", pkgbase):
        raise ValueError("invalid Arch pkgbase name")
    ref = ref or "main"
    if ref.startswith("-") or "\x00" in ref:
        raise ValueError("invalid Git ref")
    url = f"https://gitlab.archlinux.org/archlinux/packaging/packages/{pkgbase}.git"
    with tempfile.TemporaryDirectory(prefix="build-options-arch-") as directory:
        repository = Path(directory)
        git(repository, "init", "--bare", "-q")
        git(repository, "fetch", "--quiet", "--depth=1", url, ref)
        return local_snapshot(repository, pkgbase, "FETCH_HEAD", url)


def load(path):
    data = json.loads(path.read_text())
    if not isinstance(data, dict) or data.get("format") != FORMAT or not COMMIT.fullmatch(data.get("commit", "")):
        raise ValueError(f"invalid Arch snapshot: {path}")
    if not isinstance(data.get("files"), dict) or "PKGBUILD" not in data["files"]:
        raise ValueError(f"incomplete Arch snapshot: {path}")
    return data


def compare(old, new):
    if old["pkgbase"] != new["pkgbase"]:
        raise ValueError("compare the same Arch pkgbase")
    lines = [f"Arch {new['pkgbase']}: {old['version']} -> {new['version']}",
             f"Commits: {old['commit']} -> {new['commit']}"]
    changed = False
    for name in sorted(old["files"].keys() | new["files"].keys()):
        before, after = old["files"].get(name), new["files"].get(name)
        if before == after:
            continue
        changed = True
        lines.append(("+ " if before is None else "- " if after is None else "~ ") + name)
        if before and after and "text" in before and "text" in after:
            lines.append(text_diff(before["text"], after["text"], old["commit"][:12] + "/" + name,
                                   new["commit"][:12] + "/" + name).rstrip())
        elif after and "text" in after:
            lines.append(after["text"].rstrip())
    if not changed:
        lines.append("No Arch packaging file changes.")
    return "\n".join(lines) + "\n"


def recorded_snapshot(receipt, directory):
    pointer = json.loads(receipt.read_text())
    if not isinstance(pointer, dict):
        raise ValueError(f"invalid Arch snapshot receipt: {receipt}")
    name = pointer.get("snapshot", "")
    if not isinstance(name, str) or not re.fullmatch(r"[0-9a-f]{40}(?:[0-9a-f]{24})?\.json\.log", name):
        raise ValueError(f"invalid Arch snapshot receipt: {receipt}")
    result = load(directory / name)
    if name != result["commit"] + ".json.log":
        raise ValueError(f"Arch snapshot commit does not match its receipt: {receipt}")
    return result


def pkgbase(root, config, recipe=None):
    name = config.get("package", {}).get("arch")
    if name is None:
        if recipe is None:
            path = root / 'PKGBUILD'
            recipe = LiteralRecipe.read(path) if path.is_file() else LiteralRecipe('')
        name = recipe.scalar('pkgbase') or recipe.scalar('_pkgname') or root.name
        name = name.removesuffix("-waypure")
    if not isinstance(name, str) or not re.fullmatch(r"[a-z0-9][a-z0-9@+_.-]*", name):
        raise ValueError("cannot infer official Arch pkgbase; set package.arch in build-options.toml")
    return name


def review_arch(root, config, release_version, offline=False, local=None):
    base = pkgbase(root, config)
    return _review_arch(root, base, release_version, offline, local)


def _review_arch(root, pkgbase, release_version, offline, local):
    if not re.fullmatch(r"[a-z0-9][a-z0-9@+_.-]*", pkgbase) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+~-]*", release_version):
        raise ValueError("invalid Arch pkgbase or release label")
    directory = root / "options/arch"
    receipt = directory / "current.json.log"
    old = None
    if receipt.is_file():
        old = recorded_snapshot(receipt, directory)
        if old["pkgbase"] != pkgbase:
            if offline:
                raise ValueError(f"recorded Arch snapshot is for {old['pkgbase']}, configured pkgbase is {pkgbase}")
            old = None
    anchor = directory / (pkgbase + "-" + release_version + ".baseline.json.log")
    baseline = recorded_snapshot(anchor, directory) if anchor.is_file() else None
    if baseline and baseline["pkgbase"] != pkgbase:
        raise ValueError("Arch comparison anchor is for another pkgbase")
    try:
        if offline:
            if old is None:
                raise ValueError("no recorded Arch snapshot; first run review --arch")
            current = old
        else:
            current = snapshot(pkgbase, local)
            path = directory / (current["commit"] + ".json.log")
            write_snapshot(current, path)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        message = "Arch comparison unavailable: " + str(error)
        return ["ERROR " + message], [message]
    # Hold the previous packaging commit fixed throughout this source release.
    # Repeated/offline reviews must keep showing the same cumulative changes.
    if baseline is None:
        baseline = old or current
        write_snapshot({"snapshot": baseline["commit"] + ".json.log"}, anchor)
    delta = root / "options" / ("arch-" + baseline["commit"][:12] + "-to-" + current["commit"][:12] + ".changes.log")
    text = compare(baseline, current)
    atomic_text(delta, text)
    if not offline:
        atomic_text(receipt, json.dumps({"snapshot": current["commit"] + ".json.log", "checked_at": datetime.now(timezone.utc).isoformat()}, indent=2) + "\n")
    local_diff = root / "options/arch-vs-local.diff.log"
    differences = []
    for name in ("PKGBUILD", ".SRCINFO"):
        path = root / name
        entry = current["files"].get(name)
        if entry and "text" in entry and path.is_file():
            differences.append(text_diff(entry["text"], path.read_text(), "Arch/" + name, "Waypure/" + name))
    atomic_text(local_diff, "".join(differences) or "No local/Arch recipe differences.\n")
    lines = [f"Arch snapshot: {current['version']} commit {current['commit']} ({'OFFLINE: recorded state, freshness unverified' if offline else 'fetched for this review'})",
             f"Arch comparison anchor for release {release_version}: {baseline['commit']}",
             f"Arch changes: {delta}", f"Arch versus local: {local_diff}"]
    errors = []
    epoch = current["metadata"].get("epoch", "0")
    local_epoch = scalar(root / "PKGBUILD", "epoch") or "0"
    if epoch != local_epoch:
        message = f"Epoch differs: Arch={epoch}, local={local_epoch}; review replacement version ordering"
        lines.append("ERROR " + message)
        errors.append(message)
    return lines, errors
