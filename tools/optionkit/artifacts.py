"""Inspect archive metadata and ELF dynamic dependencies without executing code."""

from pathlib import Path, PurePosixPath
import os
import posixpath
import re
import selectors
import shutil
import subprocess
import tarfile
import tempfile
import time


def finish_decoder(process, timeout=30):
    """Discard trailing tar padding without buffering it or breaking the pipe."""
    deadline = time.monotonic() + timeout
    with selectors.DefaultSelector() as selector:
        selector.register(process.stdout, selectors.EVENT_READ)
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not selector.select(remaining):
                raise subprocess.TimeoutExpired(process.args, timeout)
            if not os.read(process.stdout.fileno(), 1024 * 1024):
                selector.unregister(process.stdout)
    return process.wait(timeout=max(0, deadline - time.monotonic()))


def python_distribution_metadata(name):
    """Recognize installed directory metadata and legacy flat egg-info files."""
    return ('site-packages' in name.parts and
            (name.name.endswith('.egg-info') or
             name.parent.name.endswith('.dist-info') and name.name == 'METADATA' or
             name.parent.name.endswith('.egg-info') and name.name == 'PKG-INFO'))


def audit_artifact(path, policy):
    result = {"path": str(path), "pkgname": None, "pkgver": None,
              "dependencies": [], "elf_files": 0, "elf_objects": {}, "findings": []}

    metadata = {}

    def error(code, message):
        result["findings"].append({"severity": "error", "code": code, "message": f"{path.name}: {message}"})

    process = decoder_errors = None
    try:
        if path.name.endswith(".zst"):
            # A file avoids stderr pipe deadlock while the tar stream is read.
            decoder_errors = tempfile.TemporaryFile()
            process = subprocess.Popen(["zstd", "--decompress", "--stdout", "--", str(path)],
                                       stdout=subprocess.PIPE, stderr=decoder_errors)
            archive = tarfile.open(fileobj=process.stdout, mode="r|")
        else:
            archive = tarfile.open(path, mode="r|*")
        with archive, tempfile.TemporaryDirectory(prefix="waypure-elf-") as directory:
            probe = Path(directory) / "object"
            for member in archive:
                name = PurePosixPath(member.name)
                if name.is_absolute() or ".." in name.parts:
                    error("archive.path", f"unsafe archive member {member.name!r}")
                    continue
                if (member.isfile() or member.issym() or member.islnk()) and (kind := policy.shipped_path(name.as_posix())):
                    error(f'artifact.{kind}', f'{member.name} ships a forbidden path')
                if (member.issym() or member.islnk()) and re.fullmatch(r'lib.+\.so(?:\.\d+)*', name.name) and (kind := policy.library(name.name)):
                    error(f'artifact.{kind}', f'{member.name} ships a forbidden library link')
                if member.issym() or member.islnk():
                    # Hardlinks are archive-root relative; symlinks are parent relative.
                    target = (posixpath.join(str(name.parent), member.linkname)
                              if member.issym() else member.linkname)
                    if kind := policy.shipped_path(target) or policy.library(member.linkname):
                        error(f'artifact.{kind}', f'{member.name} has forbidden link target {member.linkname}')
                python_metadata = python_distribution_metadata(name)
                if python_metadata and (member.issym() or member.islnk()):
                    result['findings'].append({
                        'severity': 'warning', 'code': 'artifact.python.coverage',
                        'message': f'{path.name}: {member.name}: linked Python distribution metadata was not inspected; target {member.linkname!r}'})
                if not member.isfile():
                    continue
                stream = archive.extractfile(member)
                if name.as_posix().removeprefix("./") == ".PKGINFO":
                    if member.size > 2 * 1024 * 1024:
                        error("archive.metadata", "oversized .PKGINFO")
                        continue
                    for line in stream.read().decode().splitlines():
                        key, separator, value = line.partition(" = ")
                        if separator:
                            field = {'depend': 'depends', 'optdepend': 'optdepends'}.get(key, key)
                            metadata.setdefault(field, []).append(value)
                        if separator and key in {"pkgname", "pkgver", "pkgbase"}:
                            if result.get(key) is not None:
                                error("archive.metadata", f"duplicate {key}")
                            result[key] = value
                        elif separator and key == "depend":
                            result["dependencies"].append(value)
                    continue
                if python_metadata:
                    if member.size > 2 * 1024 * 1024:
                        error('archive.metadata', f'oversized Python distribution metadata: {member.name}')
                        continue
                    from .python_source import audit_distribution
                    findings = audit_distribution(stream.read().decode(), member.name, policy)
                    for item in findings:
                        result['findings'].append({**item, 'code': 'artifact.' + item['code'],
                                                   'message': f'{path.name}: {item["message"]}'})
                    continue
                signature = stream.read(4)
                if signature != b"\x7fELF":
                    continue
                if kind := policy.library(name.name):
                    error(f'artifact.{kind}', f'{member.name} bundles a forbidden ELF library')
                if member.size > 1024 * 1024 * 1024:
                    error("artifact.limit", f"ELF file exceeds inspection limit: {member.name}")
                    continue
                with probe.open("wb") as output:
                    output.write(signature)
                    shutil.copyfileobj(stream, output)
                result["elf_files"] += 1
                native = subprocess.run(["readelf", "--dynamic", "--", str(probe)],
                                        text=True, capture_output=True, timeout=30)
                if native.returncode:
                    error("artifact.elf", f"cannot inspect {member.name}: {native.stderr.strip()}")
                    continue
                with probe.open('rb') as binary:
                    header = binary.read(20)
                dynamic = re.findall(r"\((NEEDED|SONAME)\).*?\[([^\]]+)\]", native.stdout)
                result['elf_objects'][name.as_posix()] = {
                    'abi': (header[4:6] + header[18:20]).hex(),
                    'needed': [library for entry, library in dynamic if entry == 'NEEDED'],
                    'sonames': [library for entry, library in dynamic if entry == 'SONAME']}
                for entry, library in dynamic:
                    if kind := policy.library(library):
                        error(f"artifact.{kind}", f"{member.name} {'needs' if entry == 'NEEDED' else 'has forbidden SONAME'} {library}")
        if not result["pkgname"] or not result["pkgver"]:
            error("archive.metadata", "missing package name/version in .PKGINFO")
        for severity, kind, key, value in policy.metadata(metadata):
            label = 'depends on' if key == 'depends' else key + ':'
            result['findings'].append({'severity': severity, 'code': f'artifact.{kind}',
                                       'message': f'{path.name}: {label} {value}'})
    except (OSError, ValueError, UnicodeError, tarfile.TarError, subprocess.SubprocessError) as exception:
        error("archive.read", str(exception))
    finally:
        if process:
            try:
                status = finish_decoder(process)
                if status:
                    decoder_errors.seek(0)
                    message = decoder_errors.read(65536).decode(errors='replace').strip()
                    error('archive.read', f'zstd exited {status}' + (': ' + message if message else ''))
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
                error("archive.read", "decompression did not finish")
            except OSError as exception:
                if process.poll() is None:
                    process.kill()
                    process.wait()
                error('archive.read', f'cannot finish decompression: {exception}')
            finally:
                process.stdout.close()
        if decoder_errors:
            decoder_errors.close()
    return result
