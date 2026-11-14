"""Shared package-archive discovery and source timestamps; no recipe execution."""

from pathlib import Path
import re


PACKAGE_ARCHIVE = re.compile(r'\.pkg\.tar(?:\.(?:zst|xz|gz|bz2|lzo|lrz|lz4|lz|Z))?$')


def is_package_archive(path):
    return bool(PACKAGE_ARCHIVE.search(path.name))


def source_inputs(directory):
    return [path for path in Path(directory).iterdir()
            if path.is_file() and '.pkg.tar' not in path.name and not path.name.endswith('.log')]


def package_artifacts(directory, name, version):
    prefixes = tuple(name + '-' + label + '-' for label in {version, version.split(':', 1)[-1]})
    return [path for path in Path(directory).glob('*.pkg.tar*')
            if path.name.startswith(prefixes) and path.is_file() and is_package_archive(path)]


def newer_inputs(directory, artifact):
    timestamp = artifact.stat().st_mtime_ns
    return sorted(path.name for path in source_inputs(directory) if path.stat().st_mtime_ns > timestamp)
