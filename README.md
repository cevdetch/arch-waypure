# arch-waypure

Arch Linux PKGBUILDs for a Wayland desktop, with X11/XWayland disabled and
PipeWire for audio. Prefer Vulkan/EGL and retain the compatible client APIs
that applications need. Use official release archives or pinned upstream Git
sources, recorded checksums and explicit packaging choices.

Each package lives directly under the repository root:

```text
cairo/
├── PKGBUILD
└── .SRCINFO
sway/
├── PKGBUILD
├── .SRCINFO
└── sway.install
tools/
├── waypure-options
├── waypure-audit
├── waypure-build
├── waypure-versions
├── waypure-remove-x11
├── templates/
└── README.md
```

Replacement packages provide and conflict with the official Arch package name.
Keep dependencies on official names, such as `cairo`; pacman can satisfy those
through an installed `cairo-waypure`. See [CONTRIBUTING.md](CONTRIBUTING.md) for
package conventions and submission requirements.

## Build an existing package

From the repository root:

```sh
tools/install
cd cairo
namcap -i PKGBUILD
makepkg -sif
namcap -i ./*.pkg.tar.zst
waypure-audit --repo .. cairo --artifacts
```

`makepkg -sif` installs missing build dependencies, builds the package and
installs the result. Review the PKGBUILD before running it. Build outputs,
downloaded sources and generated maintenance reports stay out of Git.

Arch's `pyalpm` package enables direct libalpm queries across the maintenance
tools. They share cached package data and honor pacman.conf. A bulk CLI adapter
supports interpreters without pyalpm; see the [ALPM guide](tools/README.md#alpm-package-access).

## Maintain a package

For a release update, edit `pkgver`, retain Arch's epoch and reset `pkgrel` to
1. For a packaging change to a published version, increment `pkgrel`.
Refresh the checksums, then review the upstream options and Arch packaging:

```sh
cd cairo
updpkgsums
waypure-options review --prepare --arch
```

Read `options/<pkgver>.review.log` and its Arch diffs. Review the proposed
`options/<pkgver>.options.sh.log`, then copy the choices into the PKGBUILD's
option array. Finish the recipe before generating metadata and building:

```sh
waypure-options check 1.18.6 # use the pkgver you are maintaining
makepkg --printsrcinfo > .SRCINFO
namcap -i PKGBUILD
makepkg -sif
namcap -i ./*.pkg.tar.zst
waypure-audit --repo .. cairo --artifacts
```

Generate `.SRCINFO` before the build; changing it afterward makes the artifact
stale. Use `# WAYLAND`, `# PIPEWIRE` or another short label beside policy flags,
and explain unusual choices on the preceding line. The tools preserve these
comments and report removed/deprecated options without a separate policy file.

## Start a new package

Use the [starter PKGBUILDs](tools/templates/README.md) for CMake, Meson or
Autotools. For a new Meson package, from the repository root:

```sh
mkdir example
cp tools/templates/meson.PKGBUILD example/PKGBUILD
```

Fill the upstream metadata, source, checksum and dependencies before building.
Keep backend policy and promised features explicit; use automatic detection
for ordinary probes and intentional fallbacks. Declaring a dependency does
not enable a feature by itself. The [flag strategy and comment guide](tools/README.md#choosing-explicit-flags-and-dependencies)
explains those choices and dependency classes.

## Review the collection

From the repository root:

```sh
waypure-audit --repo .
waypure-audit --repo . cairo gtk3 sway --artifacts
waypure-audit --repo . cairo --artifacts --namcap # native lint for trusted inputs
waypure-build --repo . --check
```

Audit checks metadata, selected options, dependency policy and, with
`--artifacts`, the current package archives, timestamps and ELF links/library
identities. Add `--namcap` to evaluate trusted recipes and run native archive
lint; it can invoke ldd. `CACHED`, `UNKNOWN`,
`MISSING` and `SKIP` describe limits in the available evidence. The audit does
not build packages or change feature choices.

The [tools guide](tools/README.md) covers the workflow from inside a package,
repository-wide option reviews, inferred defaults, optional profiles, source
hints and Arch comparisons. Terminal reports use color automatically.
The [runnable audit examples](tools/examples/README.md) show concrete inputs
and expected findings, including a synthetic library with an X11 SONAME.

Use `waypure-versions --diff` to compare installed/local packages with cached
official Arch repository versions. Archive discovery skips nested source/build
trees by default; `--recursive` restores that search. Use `waypure-remove-x11` to preview installed cleanup
candidates; `--apply` requests removal with pacman's confirmation and dependency
checks. The installer retires toolkit-owned aliases from earlier installations
and preserves original scripts in its [backup directory](tools/README.md).

## License

The packaging files use [0BSD](LICENSE), unless a file states otherwise.
Packaged software retains its upstream license.
