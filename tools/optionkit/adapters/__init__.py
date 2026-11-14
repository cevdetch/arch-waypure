"""Build-system-specific option collectors."""

from pathlib import Path
import re
from ..core import VERSION_LABEL


SOURCE_MARKERS = ("meson.build", "CMakeLists.txt", "configure.ac", "configure.in", "configure",
                  "Cargo.toml", "pyproject.toml", "setup.cfg", "setup.py")


class UnsupportedBackend(ValueError):
    pass


def detect(source):
    if (source / ".cmake.conf").is_file():
        return "qt6"
    if (source / "RELEASE").is_file() and (source / "configure").is_file():
        return "ffmpeg"
    candidates = []
    if (source / "meson.build").is_file():
        candidates.append("meson")
    if (source / "CMakeLists.txt").is_file():
        candidates.append("cmake")
    if (source / "configure.ac").is_file() or (source / "configure.in").is_file() or (source / "configure").is_file():
        candidates.append("autoconf")
    if not candidates:
        if (source / 'Cargo.toml').is_file():
            candidates.append('cargo')
        if any((source / name).is_file() for name in ('pyproject.toml', 'setup.cfg', 'setup.py')):
            candidates.append('python')
    if len(candidates) != 1:
        raise ValueError(f"{source}: ambiguous or unsupported build system {candidates}; select --backend")
    return candidates[0]


def capture(source, backend=None, version=None):
    if version and not re.fullmatch(VERSION_LABEL, version):
        raise ValueError("invalid release version: " + version)
    source = Path(source).resolve()
    backend = backend or detect(source)
    if backend in {"qt6", "ffmpeg"}:
        from .legacy import capture as collect
        result = collect(source, backend)
    elif backend == "meson":
        from .meson import capture as collect
        result = collect(source)
    elif backend == "cmake":
        from .cmake import capture as collect
        result = collect(source)
    elif backend == "autoconf":
        from .autoconf import capture as collect
        result = collect(source)
    elif backend in {'cargo', 'python'}:
        from ..language_source import capture
        result = capture(source, backend)
    else:
        raise ValueError(f"unsupported backend: {backend}")
    if version:
        result["source_version"] = result["version"]
        result["version"] = version
        result["version_origin"] = f"Explicit --version {version}; source reports {result['source_version']}"
        result["notes"].append(result["version_origin"])
    return result
