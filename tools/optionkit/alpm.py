"""Shared read-only package snapshots and Arch version semantics."""

from dataclasses import dataclass
from functools import cached_property, lru_cache
import importlib
import os
from pathlib import Path
import re
import shutil
import subprocess


OFFICIAL_REPOSITORIES = ('core', 'extra', 'multilib')


class BackendError(ValueError):
    pass


class BackendUnavailable(BackendError):
    pass


@dataclass(frozen=True)
class Package:
    name: str
    version: str
    base: str = ''
    depends: tuple = ()
    provides: tuple = ()


class NativeBackend:
    name = 'native'

    def __init__(self, module, config):
        self.module = module
        self.config = config or '/etc/pacman.conf'

    @cached_property
    def handle(self):
        try:
            # Let pacman resolve Includes, default paths and repository order.
            # This output is normalized configuration, not a second conf parser.
            result = subprocess.run(['pacman-conf', '--config', self.config], capture_output=True,
                                    text=True, timeout=30, env={**os.environ, 'LC_ALL': 'C'})
            if result.returncode:
                raise ValueError(f'pacman-conf exited {result.returncode}: {result.stderr.strip()}')
            options, repositories, section = {}, [], None
            for line in result.stdout.splitlines():
                if line.startswith('[') and line.endswith(']'):
                    section = line[1:-1]
                    if section != 'options':
                        repositories.append(section)
                elif section == 'options':
                    key, separator, value = line.partition(' = ')
                    if separator:
                        options.setdefault(key, []).append(value)
            if not options.get('RootDir') or not options.get('DBPath'):
                raise ValueError('pacman-conf did not report RootDir and DBPath')
            handle = self.module.Handle(options['RootDir'][0], options['DBPath'][0])
            if options.get('Architecture'):
                handle.arch = options['Architecture']
            for repository in dict.fromkeys(repositories):
                # Metadata queries only; authentication remains pacman's job.
                handle.register_syncdb(repository, 0)
            return handle
        except (OSError, ValueError, subprocess.SubprocessError, self.module.error) as error:
            raise BackendError(f'Cannot open ALPM configuration {self.config}: {error}') from error

    @staticmethod
    def project(package):
        return Package(package.name, package.version, package.base or package.name)

    @cached_property
    def local_packages(self):
        try:
            return {package.name: self.project(package) for package in self.handle.get_localdb().pkgcache}
        except self.module.error as error:
            raise BackendError(f'Cannot read installed ALPM packages: {error}') from error

    def installed_names(self):
        try:
            return [package.name for package in self.handle.get_localdb().pkgcache]
        except self.module.error as error:
            raise BackendError(f'Cannot read installed ALPM packages: {error}') from error

    def installed(self, names=None):
        if names is None:
            return self.local_packages
        names = set(names)
        if not names:
            return {}
        try:
            database = self.handle.get_localdb()
            return {name: self.project(package) for name in names
                    if (package := database.get_pkg(name)) is not None}
        except self.module.error as error:
            raise BackendError(f'Cannot read installed ALPM packages: {error}') from error

    @cached_property
    def runtime_packages(self):
        try:
            return {package.name: Package(package.name, package.version, package.base or package.name,
                                          tuple(package.depends), tuple(package.provides))
                    for package in self.handle.get_localdb().pkgcache}
        except self.module.error as error:
            raise BackendError(f'Cannot read installed runtime dependencies: {error}') from error

    def installed_runtime(self):
        return self.runtime_packages

    def sync_versions(self, repositories=None, names=None):
        names = None if names is None else set(names)
        if names is not None and not names:
            return {}
        try:
            versions = {}
            for database in self.handle.get_syncdbs():
                if repositories is None or database.name in repositories:
                    packages = (database.pkgcache if names is None else
                                (database.get_pkg(name) for name in names.difference(versions)))
                    for package in packages:
                        if package is not None:
                            versions.setdefault(package.name, package.version)
                    if names is not None and len(versions) == len(names):
                        break
            return versions
        except self.module.error as error:
            raise BackendError(f'Cannot read cached ALPM repositories: {error}') from error

    def compare(self, left, right):
        return self.module.vercmp(left, right)

    def archive(self, path):
        try:
            return self.project(self.handle.load_pkg(str(Path(path).resolve()), 0))
        except self.module.error as error:
            raise BackendError(f'Cannot read package archive {path}: {error}') from error


class CliBackend:
    name = 'cli'

    def __init__(self, config):
        self.config = config
        self.comparisons = {}

    def query(self, arguments):
        if not shutil.which('pacman'):
            raise BackendUnavailable('Package queries require pyalpm or pacman')
        command = ['pacman', *arguments]
        if self.config:
            command += ['--config', self.config]
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=30,
                                    env={**os.environ, 'LC_ALL': 'C'})
        except (OSError, subprocess.SubprocessError) as error:
            raise BackendError(f'Package query failed: {error}') from error
        if result.returncode:
            raise BackendError(f'Package query exited {result.returncode}: {result.stderr.strip()}')
        return result.stdout

    @cached_property
    def local_packages(self):
        packages = {}
        for line in self.query(['-Q']).splitlines():
            fields = line.split()
            if len(fields) != 2:
                raise BackendError(f'Invalid installed package record: {line!r}')
            packages[fields[0]] = Package(fields[0], fields[1])
        return packages

    def installed(self, names=None):
        if names is None:
            return self.local_packages
        return {name: self.local_packages[name] for name in set(names) if name in self.local_packages}

    def installed_names(self):
        return self.local_packages.keys()

    def installed_runtime(self):
        raise BackendUnavailable('Installed runtime traversal requires system Python with pyalpm and the native ALPM backend')

    @cached_property
    def sync_packages(self):
        records = []
        for line in self.query(['-Sl']).splitlines():
            fields = line.split()
            if len(fields) < 3:
                raise BackendError(f'Invalid repository package record: {line!r}')
            records.append(tuple(fields[:3]))
        return records

    def sync_versions(self, repositories=None, names=None):
        names = None if names is None else set(names)
        if names is not None and not names:
            return {}
        versions = {}
        for repository, name, version in self.sync_packages:
            if (repositories is None or repository in repositories) and (names is None or name in names):
                versions.setdefault(name, version)
        return versions

    def compare(self, left, right):
        key = (left, right)
        if key in self.comparisons:
            return self.comparisons[key]
        if not shutil.which('vercmp'):
            raise BackendUnavailable('Arch version comparison requires pyalpm or vercmp')
        try:
            result = subprocess.run(['vercmp', left, right], capture_output=True, text=True, timeout=30)
            if result.returncode:
                raise BackendError(f'vercmp exited {result.returncode}: {result.stderr.strip()}')
            comparison = int(result.stdout.strip())
            self.comparisons[key] = comparison
            return comparison
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            raise BackendError(f'Arch version comparison failed: {error}') from error

    def archive(self, path):
        fields = self.query(['-Qp', str(Path(path).resolve())]).split()
        if len(fields) != 2:
            raise BackendError(f'Invalid package archive metadata: {path}')
        return Package(*fields)


@lru_cache(maxsize=4)
def _backend(mode, config):
    if mode not in {'auto', 'native', 'cli'}:
        raise BackendError('WAYPURE_ALPM_BACKEND must be auto, native or cli')
    if mode != 'cli':
        try:
            module = importlib.import_module('pyalpm')
        except ImportError as error:
            if mode == 'native':
                raise BackendError('pyalpm is unavailable for this interpreter; use system Python with pyalpm, or WAYPURE_ALPM_BACKEND=cli') from error
        else:
            return NativeBackend(module, config)
    return CliBackend(config)


def get_backend(mode=None, config=None):
    mode = mode or os.environ.get('WAYPURE_ALPM_BACKEND', 'auto')
    config = str(config) if config is not None else os.environ.get('WAYPURE_PACMAN_CONFIG')
    return _backend(mode, config)


def reset_backend():
    _backend.cache_clear()


def compare_versions(left, right, *, natural=False):
    try:
        return get_backend().compare(left, right)
    except BackendUnavailable:
        if not natural:
            raise
        key = lambda value: [(0, int(part)) if part.isdigit() else (1, part)
                             for part in re.findall(r'\d+|\D+', value)]
        a, b = key(left), key(right)
        return (a > b) - (a < b)


def main():
    import argparse
    parser = argparse.ArgumentParser(description='Internal read-only package backend bridge.')
    parser.add_argument('command', choices=['installed-names'])
    parser.parse_args()
    try:
        for name in get_backend().installed_names():
            print(name)
    except BackendError as error:
        parser.exit(2, 'waypure-alpm: ' + str(error) + '\n')


if __name__ == '__main__':
    main()
