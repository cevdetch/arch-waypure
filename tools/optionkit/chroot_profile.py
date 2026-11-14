"""Prepare a personal CachyOS native profile using installed devtools defaults."""

import argparse
import json
from pathlib import Path
import re
import shlex
import subprocess


REPOSITORIES = ('cachyos-znver4', 'cachyos-core-znver4', 'cachyos-extra-znver4',
                'cachyos', 'core', 'extra')
DEFAULT_CHROOT = Path('/var/lib/archbuild-waypure-cachyos/native')


def native_flags(text, variable):
    def replace(match):
        line = match[0]
        for pattern, value in ((r'-march=[^\s"\']+', '-march=native'),
                               (r'-mtune=[^\s"\']+', '-mtune=native'),
                               (r'-O[0-3sg]\b', '-O3')):
            line, count = re.subn(pattern, value, line)
            if count != 1:
                raise ValueError(f'expected one {pattern} in devtools {variable}; review the installed defaults')
        return line
    result, count = re.subn(r'(?m)^'+variable+r'=.*$', replace, text)
    if count != 1:
        raise ValueError(f'expected one devtools {variable} assignment')
    return result


def prepare(output, chroot, *, devtools=Path('/usr/share/devtools'), repositories, repo=None):
    """Write a new reviewable profile; never create a chroot or run package builds."""
    output, chroot = output.resolve(), chroot.resolve()
    if output.exists():
        raise FileExistsError(f'profile output already exists: {output}')
    if (chroot/'root').exists():
        raise FileExistsError(f'baseline already exists: {chroot}/root')
    if tuple(repositories) not in (REPOSITORIES, (*REPOSITORIES, 'multilib')):
        raise ValueError('expected CachyOS znver4 repositories followed by core/extra and optional multilib')
    if any(not servers or any(not url.startswith('https://') or '\n' in url or '\r' in url for url in servers)
           for servers in repositories.values()):
        raise ValueError('each repository needs explicit HTTPS servers')
    defaults = devtools/'makepkg.conf.d'
    config = native_flags((defaults/'x86_64.conf').read_text(), 'CFLAGS')
    def options(match):
        values = match[1].split()
        for option in ('debug', 'lto'):
            values = [value for value in values if value not in {option, '!'+option}]
            values.append(option)
        return 'OPTIONS=('+' '.join(values)+')'
    config, count = re.subn(r'(?m)^OPTIONS=\(([^)]*)\)', options, config)
    if count != 1:
        raise ValueError('expected one devtools OPTIONS array')
    language_configs = {p.name: p.read_text() for p in sorted((defaults/'conf.d').glob('*.conf'))}
    if 'rust.conf' not in language_configs:
        raise ValueError('installed devtools Rust configuration is missing')
    language_configs['rust.conf'] += '\nRUSTFLAGS+=" -C opt-level=3 -C target-cpu=native"\n'
    if 'fortran.conf' in language_configs:
        language_configs['fortran.conf'] = native_flags(language_configs['fortran.conf'], 'FFLAGS')
    # Go exposes an ISA level rather than AMD-specific tuning.
    config += '\nexport GOAMD64=v4\n'
    pacman = ('[options]\nArchitecture = x86_64 x86_64_v2 x86_64_v3 x86_64_v4\n'
              'SigLevel = Required DatabaseOptional\nLocalFileSigLevel = Optional\n'
              'ParallelDownloads = 5\nDownloadUser = alpm\n')
    for name, servers in repositories.items():
        pacman += '\n['+name+']\n'+''.join('Server = '+url+'\n' for url in servers)
    profile = {'name': 'cachyos-native', 'target': 'native', 'repositories': list(repositories),
               'scope': 'Personal CPU-specific CachyOS build; not generic Arch compatibility evidence.'}
    repo = (repo or Path.cwd()).resolve()
    validator = Path(__file__).resolve().parents[1]/'waypure-validate'
    setup_command = ['doas', '/usr/bin/mkarchroot', '-C', str(output/'pacman.conf'),
                     '-M', str(output/'makepkg.conf'), '-f',
                     str(output/'build-profile.json')+':/etc/waypure-build-profile.json',
                     str(chroot/'root'), 'base-devel', 'archlinux-keyring', 'cachyos-keyring']
    setup = ('#!/usr/bin/env bash\nset -euo pipefail\n'
             'if [[ -e '+shlex.quote(str(chroot/'root'))+' || -L '+shlex.quote(str(chroot/'root'))+' ]]; then\n'
             '  printf "%s\\n" '+shlex.quote('Baseline already exists: '+str(chroot/'root'))+' >&2\n  exit 1\nfi\n'
             +shlex.join(['doas', '/usr/bin/mkdir', '-p', '--', str(chroot)])+'\n'
             +shlex.join(setup_command)+'\n')
    validate_command = [str(validator), '--repo', str(repo), '--chroot', str(chroot),
                        '--builder', 'makechrootpkg', '--auth', 'doas', '--update-baseline']
    validate = '#!/usr/bin/env bash\nset -euo pipefail\nexec '+shlex.join(validate_command)+' "$@"\n'
    output.mkdir(parents=True)
    (output/'makepkg.conf').write_text(config)
    (output/'pacman.conf').write_text(pacman)
    (output/'build-profile.json').write_text(json.dumps(profile, indent=2)+'\n')
    dropins = output/'makepkg.conf.d'
    dropins.mkdir()
    for name, text in language_configs.items():
        (dropins/name).write_text(text)
    (output/'setup.sh.log').write_text(setup)
    (output/'validate.sh.log').write_text(validate)
    return output


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True, help='new directory for reviewable configs and terminal scripts')
    parser.add_argument('--chroot', type=Path, default=DEFAULT_CHROOT, help='dedicated baseline parent containing root/')
    parser.add_argument('--repo', type=Path, default=Path.cwd(), help='recipe repository for the generated validation command')
    args = parser.parse_args(argv)
    try:
        enabled = subprocess.check_output(['pacman-conf', '--repo-list'], text=True).splitlines()
        if any(name not in enabled for name in REPOSITORIES):
            raise ValueError('enable the CachyOS znver4 repositories on the host before preparing this profile')
        target = subprocess.check_output(['gcc', '-march=native', '-Q', '--help=target'], text=True)
        match = re.search(r'(?m)^\s*-march=\s+(\S+)', target)
        if not match or match[1] not in {'znver4', 'znver5'}:
            raise ValueError('CachyOS znver4 repositories require a supported Zen 4/5 CPU')
        names = (*REPOSITORIES, 'multilib') if 'multilib' in enabled else REPOSITORIES
        repositories = {name: subprocess.check_output(['pacman-conf', '--repo', name, 'Server'], text=True).splitlines()
                        for name in names}
        output = prepare(args.output, args.chroot, repositories=repositories, repo=args.repo)
        print('Prepared: '+str(output)+' (native currently resolves to '+match[1]+')')
        print('Create the baseline in your normal-user terminal:')
        print(shlex.join(['bash', str(output/'setup.sh.log')]))
        print('Build and validate the same installable/debug outputs:')
        print(shlex.join(['bash', str(output/'validate.sh.log')])+' PACKAGE [PACKAGE ...]')
        return 0
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        parser.exit(2, 'waypure-chroot: '+str(error)+'\n')
