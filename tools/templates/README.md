# Starter PKGBUILDs

Choose the template that matches upstream's build system:

| Template | Build system | Native option array |
| --- | --- | --- |
| [cmake.PKGBUILD](cmake.PKGBUILD) | CMake with Ninja | `_cmake_options` |
| [meson.PKGBUILD](meson.PKGBUILD) | Meson through arch-meson | `_meson_options` |
| [autotools.PKGBUILD](autotools.PKGBUILD) | Generated configure with Make | `_configure_options` |

From the repository root, for a new Meson package:

```sh
mkdir example
cp tools/templates/meson.PKGBUILD example/PKGBUILD
cd example
```

Fill the empty metadata, official source URL, upstream SPDX license identifiers
and dependencies. Adapt the extracted source directory to the actual archive.
Use a unique download filename when upstream serves names such as `v1.0.tar.gz`:

```bash
source=("${_pkgname}-${pkgver}.tar.gz::${url}/archive/refs/tags/v${pkgver}.tar.gz")
```

That example assumes an upstream GitHub-style archive URL; use the project's
actual release location. Generate real checksums with `updpkgsums`.

## Package relations

All three starters version the official package name they replace:

```bash
provides=("${_pkgname}=${pkgver}")
conflicts=("${_pkgname}")
```

For `_pkgname=example` and `pkgver=2.4.0`, this provides `example=2.4.0`,
allowing dependencies such as `example>=2.0` to resolve to `example-waypure`.
A bare `provides=('example')` cannot satisfy versioned dependencies. This is
the supplied version, not a dependency pin. Keep dependencies on official names.

If Arch sets an epoch, copy it into the recipe and include it in that provision:

```bash
epoch=1
provides=("${_pkgname}=${epoch}:${pkgver}")
```

Use the actual official output name for ABI-named or split packages, such as
`wlroots0.20`. Version each official-name provision for the corresponding
output; do not append `${pkgver}` to every provision mechanically:

- Virtual aliases have their own meaning and version scheme, if any. They do
  not necessarily use the upstream package version.
- For a shipped library, use a bare SONAME provision such as
  `provides+=('libexample.so')`; makepkg derives its ABI version from the built
  library. The upstream `${pkgver}` is not the library's SONAME version.
- Exact same-pkgbase sibling dependencies may use
  `"sibling-waypure=${pkgver}-${pkgrel}"` (including epoch when present).
  Cross-package dependencies keep official names.

Provisions describe dependency relations; they do not create files or restore
disabled features. Document any intentional compatibility alias in its recipe.
Alternate builds use provides/conflicts; add replaces only
for an actual obsolete-package migration, as described in
[Arch's package relations guide](https://wiki.archlinux.org/title/PKGBUILD#Package_relations).

## Adapt the build

Keep policy choices in the native option array. There is no universal X11 or
Wayland switch; use the flags this release actually implements. Follow the
[flag and dependency strategy](../README.md#choosing-explicit-flags-and-dependencies)
and [comment convention](../README.md#optional-named-packaging-choices).

The CMake starter uses `None` to preserve makepkg's compiler flags. Check
upstream's required definitions before choosing it: some projects require a
different build type. Set `CMAKE_INSTALL_LIBDIR=lib` only when upstream needs
that override. See [Arch's CMake guide](https://wiki.archlinux.org/title/CMake_package_guidelines).
CTest parallelism uses `CTEST_PARALLEL_LEVEL` when set and the available CPU
count otherwise. `--no-tests=error` catches an accidentally empty test run.

The Meson starter uses arch-meson, whose wrapper promotes feature `auto` to
`enabled`. Preserve an existing alternative strategy, such as mpv's
`--auto-features=disabled`, when adapting a package. Installation uses
`--no-rebuild` so compilation stays in `build()`. See
[Arch's Meson guide](https://wiki.archlinux.org/title/Meson_package_guidelines).

The Autotools starter assumes the release archive already contains configure.
Use prepare() for required patches or regeneration; do not run autoreconf
unconditionally. The current tools also recognize existing `_make_options`
arrays; `_configure_options` describes configure arguments more precisely.
Plain Make projects need their upstream-specific variables and targets;
the Autotools starter is not a generic Makefile recipe.

Keep/adapt check() only when upstream supplies the corresponding test suite.
Declare its additional tools in checkdepends. Source inspection and tests
decide which build, license and documentation steps apply; templates do not
assume files named LICENSE or README.md exist. For required license text that
upstream does not install, adapt:

```bash
install -Dm644 "${_pkgname}-${pkgver}/COPYING" \
  -t "${pkgdir}/usr/share/licenses/${pkgname}"
```

## Review and build

After filling the recipe:

```sh
updpkgsums
waypure-options review --prepare --arch
# Review the proposed array and finish the PKGBUILD.
makepkg --printsrcinfo > .SRCINFO
namcap -i PKGBUILD
makepkg -sif
namcap -i ./*.pkg.tar.zst
waypure-audit --repo .. example --artifacts
```

Use the [clean-chroot workflow](https://wiki.archlinux.org/title/DeveloperWiki:Building_in_a_clean_chroot)
before submission. It catches host packages influencing optional detection
and undeclared build inputs. Keep generated metadata before the final build.
The root's former `cmake.sh.log`, `make.sh.log` and `meson.sh.log` starters
have moved here; per-release `options/*.options.sh.log` remain generated reports.
