"""Shared recorded package metadata; no recipe execution or build planning."""

from dataclasses import dataclass, field
from pathlib import Path


class SrcinfoError(Exception):
    pass


@dataclass
class PackageInfo:
    dir: Path
    pkgnames: list[str] = field(default_factory=list)
    version: str = ""
    depends: list[str] = field(default_factory=list)
    makedepends: list[str] = field(default_factory=list)
    checkdepends: list[str] = field(default_factory=list)
    provides: list[str] = field(default_factory=list)
    source_text: str = field(default="", repr=False)


def find_pkgbuild_dirs(repo: Path) -> list[Path]:
    if not repo.is_dir():
        raise SrcinfoError(f"not a directory: {repo}")
    return sorted(p for p in repo.iterdir() if p.is_dir() and (p / "PKGBUILD").is_file())


def parse_srcinfo_text(text: str, pkgdir: Path) -> PackageInfo:
    pkgver = pkgrel = epoch = None
    info = PackageInfo(dir=pkgdir, source_text=text)
    for raw in text.splitlines():
        line = raw.strip()
        if not line or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if key in {'depends_x86_64', 'makedepends_x86_64', 'checkdepends_x86_64', 'provides_x86_64'}:
            key = key.removesuffix('_x86_64')
        if key == "pkgver":
            pkgver = value
        elif key == "pkgrel":
            pkgrel = value
        elif key == "epoch":
            epoch = value
        elif key == "pkgname":
            info.pkgnames.append(value)
        elif key == "depends" and value:
            info.depends.append(value)
        elif key == "makedepends" and value:
            info.makedepends.append(value)
        elif key == "checkdepends" and value:
            info.checkdepends.append(value)
        elif key == "provides" and value:
            info.provides.append(value)
    if not info.pkgnames or pkgver is None or pkgrel is None:
        raise SrcinfoError(f"{pkgdir}: incomplete SRCINFO (need pkgname/pkgver/pkgrel)")
    info.version = f"{epoch}:{pkgver}-{pkgrel}" if epoch else f"{pkgver}-{pkgrel}"
    return info


def fields(text):
    result = {}
    for line in text.splitlines():
        key, separator, value = line.strip().partition(" = ")
        if separator:
            result.setdefault(key, []).append(value)
    return result


def package_fields(text):
    base, packages, target = {}, {}, None
    for line in text.splitlines():
        key, separator, value = line.strip().partition(" = ")
        if not separator:
            continue
        if key == "pkgname":
            target = packages.setdefault(value, {})
        else:
            (base if target is None else target).setdefault(key, []).append(value)
    return {name: {**base, **values} for name, values in packages.items()}


def entry_values(entry, name):
    return [value for value in entry.get(name, []) + entry.get(name + "_x86_64", []) if value]
