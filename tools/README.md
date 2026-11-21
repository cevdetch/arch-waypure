# Packaging maintenance tools

Developed locally in autumn 2026 and published on 14 November 2026.

`waypure-options` compares upstream releases, checks packaging choices and writes
copyable Bash option arrays. It supports Qt 6, FFmpeg, Meson, CMake and Autoconf,
plus typed Cargo and Python dependency evidence.
Keep flag values in the PKGBUILD. The command discovers the backend, release
source and official Arch pkgbase; unusual mappings can use a small TOML file.
The existing `qt6-options` and `ffmpeg-options` commands retain their interfaces.
The repository also contains `waypure-audit`, `waypure-build`,
`waypure-versions` and the Bash cleanup command `waypure-remove-x11`.

| Command | Purpose |
| --- | --- |
| `waypure-options` | Review upstream options and Arch packaging |
| `waypure-audit` | Audit recipes, options and archives |
| `waypure-build` | Build in dependency order and check freshness |
| `waypure-validate` | Validate selected releases in fresh clean-chroot copies |
| `waypure-chroot` | Prepare a personal native CachyOS clean-chroot profile |
| `waypure-versions` | Compare installed/local and cached repository versions |
| `waypure-remove-x11` | Preview or request removal of installed cleanup candidates |
| `waypure-audit-audio` | Inspect installed audio clients, servers, bridges and plugins |
| `waypure-audit-graphics` | Inspect installed OpenGL/EGL/GLX/GLES/Vulkan support |
| `waypure-audit-x11` | Trace installed Xlib/XCB/Xorg/XWayland support |

The specialized `qt6-options` and `ffmpeg-options` commands are thin entry points.
Their engines live in `optionkit/qt6.py` and `optionkit/ffmpeg.py`; the shared
option tool imports those modules directly. Both commands share CLI setup and
dispatch. Configuration files use `build-options.toml`.

Run from this checkout, or install the commands:

```sh
tools/install                         # defaults to ~/.local
tools/install --prefix /some/prefix
```

The installer copies the Python library under `PREFIX/lib/arch-waypure` and
links the commands from `PREFIX/bin`. No pip environment is required.
Before replacing a regular command file, it saves the original under
`PREFIX/lib/arch-waypure/backups/<name>.<sha256>.bak`. Reinstalling through
the generated symlinks leaves those backups intact. A directory occupying a
command path stops installation before it replaces commands.
Reinstalling removes toolkit-owned aliases and obsolete command copies from
earlier installations. The installer leaves unrelated personal commands alone.
Python 3.11 or newer is needed. Meson collection needs Meson; Arch comparisons
need Git. Native build-option types and spellings remain specific to each
adapter.

## ALPM package access

Install Arch's `pyalpm` package for direct access to pacman's libalpm. The
tools use `pacman-conf` once per handle to resolve `/etc/pacman.conf`, including
`Include`, `RootDir`, default paths and configured repository priority. The package
may already be installed as a dependency of namcap. Use the system Python so
its native extension matches the interpreter.

The shared backend provides installed-package snapshots, cached repository
versions, archive metadata and version comparison. Builder freshness, audit
comparisons, option release ordering, version reporting and cleanup selection
use it. Package data stays cached within a command; the builder discards its
snapshot after makepkg changes installed packages. Queries do not refresh sync
databases or start transactions. Official Arch comparisons use only
`core`, `extra` and `multilib`, including on systems with third-party repositories.
Native queries filter repository names before loading package caches, so those
comparisons avoid reading unrelated repository databases.
When package names are known, native queries use libalpm's indexed `get_pkg`
lookup and project only those records. Build freshness requests each output;
audits request the official names for the selected recipes; version reporting
requests names found among installed packages and local archives. Repository
priority still decides the result, and later repositories are skipped once all
requested names have been found. Enumerating installed Waypure packages and
cleanup candidates still requires the complete installed-name list. The
native backend reads names alone for that step, then version reporting loads
metadata only for Waypure packages. Cleanup never needs those full records.
These are metadata reads, not signature verification. Pacman retains its
signature checks when installing packages; archive metadata alone
does not authenticate an archive.

```sh
WAYPURE_ALPM_BACKEND=native waypure-build --repo . --check
WAYPURE_ALPM_BACKEND=native waypure-versions --diff
WAYPURE_PACMAN_CONFIG=/path/to/pacman.conf waypure-remove-x11
```

`WAYPURE_ALPM_BACKEND=auto` is the default. It uses pyalpm when available to
the active interpreter. Otherwise it uses the CLI adapter, with one bulk
installed-package query, one bulk sync query and cached version comparisons.
Named selections filter those same cached bulk queries; they do not spawn a
query for every package. An empty selection performs no package query.
Set `WAYPURE_ALPM_BACKEND=cli` to select that adapter, including when testing
with uv's separate Python. Explicit `native` mode reports an unavailable
binding. A broken native configuration or query remains an error; it does not
silently switch to another database. Both adapters honor
`WAYPURE_PACMAN_CONFIG`; cleanup passes the same configuration to pacman when
`--apply` is requested.

This uses `pyalpm`, the binding to pacman's C library. Arch's separate
`python-alpm` package exposes the newer Rust ALPM metadata libraries. It is
not required by this backend. Native option collectors, literal Bash inspection
and X11 ELF policy checks retain their existing implementations; libalpm does
not supply equivalents for those tasks.

`waypure-versions` loads local archive metadata through the backend. By default
it searches the selected repository root and package directories containing a
PKGBUILD, without walking source/build trees. Use `--recursive` to retain the
old nested search, or `--repo PATH` / `WAYPURE_DIR` to select another directory.
The command supports `--diff`, release comparison and epoch handling.

## Installed audio, graphics and X11 diagnostics

These three read-only commands inspect the **installed** system, independently
of the recipes in this checkout. They use the configured native ALPM database
and require the system Python with `pyalpm`. Full ELF scans also require
`python-pyelftools` (already used by namcap). They never execute inspected
programs, `ldd`, package transactions, or service commands.

```sh
tools/waypure-audit-audio
tools/waypure-audit-audio --family jack --details
tools/waypure-audit-graphics --family legacy-gl --details
tools/waypure-audit-x11 --details

# Fast package metadata/file-list query, without reading binaries:
tools/waypure-audit-audio --metadata-only

# One filesystem scan for all three reports:
tools/waypure-audit-audio --all --json > tools/reports/installed-stacks.json.log

# Restrict displayed packages, while retaining the complete dependency graph:
tools/waypure-audit-graphics --package '*-waypure' --details
```

`--family` and `--package` can be repeated; `--jobs` selects 1–32 file workers
(default 4). `--family` choices appear in `--help`; use `--all` for families from
more than one domain. `WAYPURE_PACMAN_CONFIG` and `WAYPURE_ALPM_BACKEND=native`
work as for the other tools. `tools/install` includes all three commands.

The report distinguishes package names and provisions, recorded file lists,
shipped symlinks, ELF SONAMEs and direct ELF `DT_NEEDED` entries. It shows one
required **package dependency path** to detected support, using the actual
installed versioned providers. Multiple compatible installed providers make a
path conditional; a definite path takes precedence when available. Optional
package relationships appear separately and never become required graph edges.
Package paths to a library/plugin owner do not prove which particular ELF file
a consumer loads. For example, a program depending on mpg123 can reach a
package containing its optional JACK output plugin without requiring JACK for
ordinary playback. `--details` shows the endpoint evidence and actual file.

Audio distinguishes JACK, ALSA, PulseAudio and PipeWire, including compatibility
bridges, clients, servers, session managers and plugins; it also inventories
OSS, sndio, PortAudio and OpenAL. A Pulse/ALSA client may route through PipeWire;
a package name cannot establish that runtime choice. Graphics separates legacy
`libGL`, GLVND OpenGL/dispatch, EGL, GLES, GLX, Vulkan, OSMesa, GBM and DRM.
`libGL` alone is not proof of GLX. X11 classification uses the shared curated
X11 policy rather than matching every `libx*` name: plain `libxkbcommon`,
`libxml2`, `libxslt` and `libxcrypt` are not X11 libraries. Vulkan ICD/layer
manifest filenames identify driver/layer components; their JSON contents and
active loader selection are not validated. Debug-symbol files and empty
manifest directories do not count as runtime interfaces.

Exit status is 0 for a completed inventory, 1 for command/backend errors, and
2 for incomplete coverage: unreadable/missing/malformed files, unresolved
required dependencies, or database changes during the scan. Protected files
can make an ordinary-user full scan incomplete; gaps stay visible in text and
JSON. A metadata-only result covers declarations/file lists only. Results are
point-in-time evidence: no `dlopen`-only or static protocol-code detection,
unowned files, Flatpak/container inventory, active backend selection or kernel
module audit is claimed. The categories are curated, not proof that every
possible implementation has been detected. These reports do not decide which
packages are safe to remove.

## Repository rebuild planning

[`arch-rebuild-order`](https://gitlab.archlinux.org/archlinux/arch-rebuild-order)
is a Rust/libalpm helper for rebuild planning from sync databases. It can help
inspect official package rebuilds:

```sh
arch-rebuild-order --no-reverse-depends --with-check-depends cairo pango gtk3
```

It does not read our unpublished PKGBUILDs or `.SRCINFO` files. Its official
package dependency graph can differ from our Wayland recipes. `waypure-build`
therefore retains the local recipe graph and explicit tiers for cycles.
The helper remains optional and is not a build dependency.

`waypure-build` checks version constraints against local output versions and
explicitly versioned `provides` entries, using ALPM comparisons. An unversioned
provision cannot satisfy a versioned dependency. Compatible concrete package
names take priority over virtual providers; split outputs from one recipe
share a build. The planner rejects incompatible local versions, ambiguous
providers and duplicate concrete output names before building. It reuses the
resolved edges for build order and dirty propagation.

The [starter package relations examples](templates/README.md#package-relations)
show versioned official-name provisions, epoch handling and the separate rules
for virtual aliases and library SONAMEs.

Use `--ignore` to select between recipe variants or leave an unfinished recipe
out. Excluded recipes become external dependencies handled by makepkg/pacman.
The current working selection excludes GStreamer, LibreOffice and empty stubs:

```sh
waypure-build --repo . --order-only \
  --ignore gstreamer,gstreamer-hugo,gstreamer-way,libreoffice,libreoffice-fresh,pyqt6,qt6-webengine
```

The same selection works with `--check`. Resolve a version failure by checking
the recipe's version and `provides`, or exclude that recipe when you intend
to use an external provider. For an ambiguous alias or duplicate output,
exclude the unused recipe directories.

For the 2026-10-25 comparison, upstream HEAD was
`733569c3d6b09bb23d2a6a3d26f2c92b016b1c2d` and Arch packaged version 0.4.5-1.
With a temporary sync database generated from 28 recipes / 31 outputs, the
helper returned the same package set and respected dependencies between cycle
groups. Its process plus database load took about 11 ms; sorting our already
parsed metadata took about 0.4 ms. These scopes differ, and serialization into
the helper's database was excluded. Neither graph operation explains the old
multi-second package-query delays. The recorded measurements live in
`tools/reports/alpm` locally; they are machine-specific evidence, not a speed
guarantee.

## Update workflow

Run a review once before the first upgrade to establish source and Arch
baselines. The first run cannot reconstruct an earlier release that was never
recorded.

From the repository root, install the commands and enter the package:

```sh
tools/install
cd cairo
waypure-options review --prepare --arch
```

`waypure-options` discovers Cairo's source, backend and package name. Review
`options/<pkgver>.review.log`, the Arch diffs and
`options/<pkgver>.options.sh.log`. Copy the selected options into the PKGBUILD
after checking their intent. The report and checks use color in a terminal;
the copyable Bash file stays plain text.

After finishing the recipe, use the version from `pkgver`:

```sh
waypure-options check 1.18.6
makepkg --printsrcinfo > .SRCINFO
namcap -i PKGBUILD
makepkg -sif
namcap -i ./*.pkg.tar.zst
waypure-audit --repo .. cairo --artifacts
```

For later releases:

1. Edit `pkgver`, retain Arch's epoch, and refresh the source checksums with
   `updpkgsums`.
2. Run `waypure-options review --prepare --arch`. Preparation runs the trusted
   PKGBUILD through `makepkg --nobuild --noprepare --nodeps`, verifying and
   extracting sources without installing dependencies or compiling. Skipping
   prepare keeps the catalogue separate from local patches. Install the native
   collector tool (such as Meson) if needed. Omit `--prepare` for extracted sources.
3. The review selects the source matching pkgver,
   compares the newest recorded older release, checks the current flags and
   policy groups, and compares official Arch packaging commits.
4. Read `options/<version>.review.log`, the Arch diffs it links to, and
   `options/<version>.options.sh.log`. Copy the proposed array after review.
5. Check edited flags with `waypure-options check <version>`, regenerate .SRCINFO
   with `makepkg --printsrcinfo > .SRCINFO`, then build with `makepkg -sif`.
   Inspect the result using `waypure-audit --repo .. cairo --artifacts`.

Collection and audit read PKGBUILDs as text. The explicit `--prepare` option
executes makepkg to obtain sources. Commands do not update pkgver/checksums,
edit recipes, remove dependencies, or compile packages. The current
`waypure-build` freshness check also considers policy files in the package
directory, so finish those edits before building.

For an Autoconf source snapshot, `--prepare` can leave generated `configure`
absent because it skips the recipe's `prepare()`. The source catalogue remains
usable but incomplete. If generated help is needed, review the package's
preparation, run `makepkg --nobuild` to execute it, then run
`waypure-options review --arch` again. This uses the prepared/patched source;
its catalogue is retained separately from earlier source evidence.

## Review several packages

From the repository root, collect fresh option evidence before auditing:

```sh
for recipe in */PKGBUILD; do
  package=${recipe%/PKGBUILD}
  waypure-options -C "$package" review
done
waypure-audit --repo . --artifacts
```

Each review writes its own report and proposal. The loop continues past a
package that needs a source hint, lacks prepared sources or has an unsupported
collector. Inspect those results along with the audit. Add `--arch` to each
review when fetching official packaging is intended; the commands above use
local sources and cached Arch evidence.

To review the current non-Qt work while including Qt Base:

```sh
for recipe in */PKGBUILD; do
  package=${recipe%/PKGBUILD}
  case "$package" in
    qt6-base) ;;
    qt6-*) continue ;;
  esac
  waypure-options -C "$package" review
done
```

## Repository audit

```sh
waypure-audit --repo .                    # all package directories, including drafts
waypure-audit --repo . cairo gtk4         # selected packages
waypure-audit --repo . cairo --artifacts  # current archives and ELF dependencies
waypure-audit --repo . cairo --artifacts --namcap # also lint trusted inputs with namcap
waypure-audit --repo . --json > /tmp/waypure-audit.json.log
waypure-audit --repo . --strict           # fail warnings as well as errors
waypure-audit --repo . --metadata-only    # skip release-option checks
waypure-audit --repo . mpv --installed-deps # also trace installed runtime providers (pyalpm)
waypure-audit --repo . gtk4 --build gtk4/src/build # inspect one configured build
```

The audit reads .SRCINFO and literal recipe fields, checks scalar and split
package identities, license/architecture/replacement fields, source and SHA256
order and counts, official dependency names, sibling pins
and replacement declarations. It traces
runtime dependencies through local packages/provides. The shared
`waypure-policy.toml` lists known X11 and incompatible audio package/library
families. Its top-level `dependencies`/`libraries` tables enforce WAYLAND;
`pipewire.dependencies`/`pipewire.libraries` enforce PIPEWIRE. Dependency
checks cover runtime, build and check inputs, output package names and provided
capabilities. Prohibited optional dependencies produce warnings and are not
traversed as required inputs. The source pkgbase is not a shipped capability:
libpulse can come from pulseaudio sources. Both policies apply by default.
An explicit `--policy` replaces the rules; a custom file can omit the audio
tables. If it includes them, both forbidden arrays must be valid.

The builder and auditor share recorded-metadata helpers in `optionkit/metadata.py`.
For each audit command, the auditor reads each `.SRCINFO` once, builds one local
provider index and reuses each selected recipe's literal bindings. The snapshots
last for that command; a later command reads edits again. Runtime traversal uses
a deque and a per-index version cache. The auditor does not import or execute
the builder command to inspect metadata.

The matcher removes version constraints, a leading `lib32-` and an exact
trailing `-waypure` from package names. It recognizes makepkg SONAME dependencies
such as `libX11.so=6-64` and `lib:libX11.so.6`. Optional descriptions use `: `,
which preserves SONAME namespaces. Checks include unqualified and x86_64
metadata fields; other architecture qualifiers stay outside this target.
ELF rules match the POSIX basename, including absolute, relative and `$ORIGIN`
DT_NEEDED/SONAME paths, while diagnostics retain the full value and case.
The audit labels audio findings `dependency.pipewire` or `artifact.pipewire`;
transitive path messages name their policy. It compiles the pattern groups
once per invocation. It does not guess build flags from option spelling.

X11 coverage includes Xlib/XCB extensions and utilities, authentication/session
libraries, legacy X toolkits, GLX vendor libraries, X-only toolkit modules and
X server/driver families. The [X.Org library guide](https://www.x.org/guide/client-ecosystem/)
documents the core families. Shared helpers such as libxkbcommon, libxcvt,
libfontenc and libxshmfence remain allowed; their names or historical users
alone do not prove an X11 runtime requirement. Generic EGL/OpenGL libraries
and shared protocol headers also remain allowed.

### Audio through PipeWire

Prefer a mature native PipeWire backend when the application supports it.
Otherwise retain its best-supported PulseAudio client or ALSA backend:

| Application interface | PipeWire integration | Keep when required |
| --- | --- | --- |
| Native PipeWire | PipeWire and a session manager | libpipewire |
| PulseAudio client | pipewire-pulse | libpulse and client bindings |
| ALSA client | pipewire-alsa and the configured PipeWire device | alsa-lib |
| JACK | Excluded by this repository | Disable the backend |

PipeWire documents both compatibility paths; it does not prescribe that
PulseAudio must always outrank ALSA. Backend maturity, required functionality
and application behavior decide that choice. See the upstream
[setup guide](https://github.com/PipeWire/pipewire/blob/master/INSTALL.md) and
[pipewire-pulse manual](https://docs.pipewire.org/page_man_pipewire-pulse_1.html).
Selecting the PipeWire server/device needs runtime verification; an allowed
client dependency alone does not establish where its audio goes.

The PIPEWIRE rules reject JACK implementations and APIs, PipeWire JACK
compatibility libraries, JACK bridges and netJACK modules. They also reject
the PulseAudio daemon and its server extensions. They permit libpulse,
pulseaudio-qt, ALSA and PipeWire clients. The `pulseaudio-alsa` configuration
can also route ALSA through libpulse to pipewire-pulse; its name alone does
not establish a PulseAudio daemon requirement. A bundled JACK plugin still
fails the archive checks. Avoid broad `libpulse*` or
`pulseaudio*` prohibitions: those would reject valid clients.

Keep supported JACK and other unwanted backend flags explicitly disabled in
recipes. For options that need stronger enforcement, use the package-specific
profiles below and inspect the configured feature summary. Global name rules
cannot detect an arbitrary static protocol implementation or a backend hidden
behind dlopen. This policy change does not edit recipes or configure services.

### Metadata and archive evidence

The shared metadata reader resolves `$name` and `${name}` references to unique,
preceding scalar literals, including chained `_pkgname` and `_pkgbase` aliases.
It preserves Bash quoting and compares the expanded values with .SRCINFO.
These ordinary substitutions do not need a warning or a separate verification
receipt. A changed alias still produces a mismatch if the metadata is stale.

Functions, conditional assignments, duplicate/indexed writes, `unset` mutations
and unsupported shell expressions remain unverified. An unknown `unset` target
withholds certainty for all fields. Commands and custom IFS settings stop literal
reference resolution; Bash's dynamic/special variables are excluded. Single-quoted
and escaped references remain literal text. Computed recipe fields still require
generated metadata review; the audit does not evaluate Bash.

The options column uses the matching recorded release catalogue. It checks
source hashes when the extracted tree exists and reports stale evidence.
Missing catalogues/sources are explicit. Official Arch versions come from
recorded Git snapshots or the local core/extra/multilib sync database; neither
proves current network state. Run `waypure-options review --arch` to fetch packaging.

Archive inspection streams regular files into a temporary ELF probe and reads
DT_NEEDED and SONAME with readelf. It checks forbidden library names and
symlink/hardlink names and targets too. Optional `paths.forbidden` and
`pipewire.paths.forbidden` arrays match installed archive paths. The default
rules include Xwayland, Xorg, pw-jack and known X11/JACK static archives.
Relative symlinks are interpreted from their installed parent, hardlinks from
the archive root, without following either. Unrelated `.a` and `.la` files
remain allowed. Named static-archive checks do not detect arbitrary static
implementations. Archive metadata checks output names, provides, hard
dependencies and optional suggestions with the same policy as recipes.
It does not execute binaries, invoke ldd, or extract
archive paths/symlinks. It validates current-version archive identities and
declared dependencies, and reports `STALE` when a package input is newer than
an archive. Archive filenames may include or omit the epoch;
the package version inside `.PKGINFO` must match the full recorded version.
Zstandard archives require zstd; ELF inspection needs
readelf. Local dependency paths and direct ELF links do not cover every external
provider or library loaded through dlopen.

`--installed-deps` adds a separate `INSTALLED` result. It follows actual
installed package dependencies and versioned provisions from one cached native
ALPM snapshot. Compatible concrete package names take priority over virtual
providers; ambiguous paths warn, and missing/incompatible providers remain
visible. An unversioned provision cannot satisfy a versioned request. These
paths describe this installation, alongside the local recipe plan. They do not
substitute an official sync package for its installed Waypure replacement.
This option requires system Python with pyalpm; the CLI backend reports the
requirement without spawning per-dependency pacman queries. It changes no
installed packages or databases.

`--build PATH` adds a separate `BUILD` result for exactly one selected package
and requires its matching release catalogue. It reuses the option tool's
project/version-checked observations. Requested-value drift warns; an observed
conflict with an optional named profile fails. Profiles remain optional and
native option names are never inferred from global policy labels.
Meson introspection supplies detected dependencies and actual target linker
arguments. Detected probes can be unused, so they warn; forbidden target link
inputs fail. CMake/Qt use an existing file-api codemodel-v2 reply, reading both
library and linker-flag fragments. Missing target evidence warns; cache values
alone cannot establish target linkage. A reply older than CMakeCache.txt is
marked stale and its link findings warn. The audit does not create a file-api
query or reconfigure a build. It recognizes direct and forwarded library
arguments without executing them. Static Cargo features, runtime loading and
source-rule guarantees still require package-specific review. One configured
build does not prove how a later build will behave with different inputs.

`--namcap` adds native recipe lint and, with `--artifacts`, native archive lint.
The `NAMCAP` column shows its result; the default is `SKIP`. This option
evaluates trusted PKGBUILDs through namcap and can invoke ldd on archived
binaries. It extends the default nonexecuting audit. Namcap can return zero
with errors, so the integration reads its `E:`, `W:` and `I:` findings.
A missing requested tool, timeout or execution error fails the check.

Run the [synthetic examples](examples/README.md) to inspect metadata mismatches,
stale archives, a renamed dummy ELF with an X11 SONAME, and a missing license
file. Each example prints the concrete recipe, metadata and archive paths.

`PASS` applies to the checks performed. `SKIP`, `MISSING`, `CACHED`, `STALE` and
`WARN` show their limits. Errors return exit status 1; `--strict` also fails
warnings/missing required evidence. Invalid arguments return 2. Use JSON for
automation and `--verbose` to include explanatory notes in text output.

A passing tool test suite does not mean the recipes pass their audits.
Resolve missing/stale `.SRCINFO`, real source checksums and package identity
mismatches before building; review unsupported flags against source evidence.
Finish recipe and metadata edits before rebuilding stale artifacts. Keep
cached Arch evidence and unsupported collector results visible during review.

`waypure-build --repo . --check` retains its build-order/freshness interface.
The maintained source is `tools/waypure-build`; the installer links that copy
into `PREFIX/bin`. The old standalone directory can remain as a backup.
The builder requires installed versions at least as new as the recipe and
current-version archives for every split output before reporting `FRESH`.
Audit and builder share the
archive discovery and source timestamp helpers; audit does not require an
installed package to detect a stale archive.

`waypure-build --namcap` lints the recipe before building and the output
archives after makepkg. Recipe errors stop the build; archive errors stop
further work. The existing `makepkg -si --noconfirm` command installs before
archive lint runs. For inspection before installation, use `makepkg -s`, lint
the resulting archives, then install them yourself.

`waypure-versions --diff` retains its installed/local version comparison.
The audit does not call their metadata-generation or build functions.

## Installed X11 cleanup

```sh
waypure-remove-x11                       # preview installed candidates
waypure-remove-x11 --policy /path/to/policy.toml
waypure-remove-x11 --apply               # request removal; pacman confirms
```

The Bash command queries the shared ALPM backend once. It selects matches from
`dependencies.forbidden` plus `[cleanup].extra` in `waypure-policy.toml`,
including matching `lib32-` variants and excluding names ending in `-waypure`.
It does not read the PIPEWIRE tables or select audio packages. The extra list
includes development tools and data; it does not change the audit's runtime
dependency rules. Shared helpers such as `libxkbcommon`, `libxcvt`, `libxml2`
and `libxcrypt` do not match the default cleanup policy.

The default prints a candidate list without invoking sudo or removal.
`--apply` runs `pacman -Rns` with pacman's dependency checks and confirmation;
it can also remove unneeded dependencies through that command's recursive
removal behavior. Check the transaction pacman presents. A query or policy
error stops the command; a failed removal returns its failure status.

This command manages installed packages. It does not edit PKGBUILDs, rebuild
dependent applications or prove that an installation has no X11 code. Its tests
use fake pacman/sudo executables, including for the removal path.

## Optional named packaging choices

Profiles add enforcement of specific values when you need it. They are optional;
normal option/deprecation checks use the PKGBUILD alone. Comments such as
`# WAYLAND`, `# SYSTEM` or `# PACKAGING` label recipe rows without a profile file.
Keep exceptional intent in `build-options.toml` beside its PKGBUILD. This example
matches GTK's native flag names and values:

```toml
[package]
backend = "meson"
arch = "gtk4"
profiles = ["WAYLAND"]

[profiles.WAYLAND]
description = "Build Wayland and exclude X11"
flags = ["-Dwayland-backend=true", "-Dx11-backend=false"]
```

Names such as `WAYLAND`, `PIPEWIRE`, `VULKAN`, `SHARED` and `PERSONAL` describe
intent; each package explicitly maps that intent to its own flags. A profile
does not guess option names from words such as `x11` or disable unrelated
settings. Backend, source or Arch-name hints can also be set without profiles.
GTK4, Cairo, Qt Base and FFmpeg use discovery with no policy file. Other current
choices, including a11y and introspection, stay in the recipe.

Use a bare uppercase tag for grouping and a preceding comment for a longer
reason. One native option per row keeps the flag, label and explanation
together when rendering:

```bash
local _meson_options=(
  -Dwayland-backend=true # WAYLAND
  -Dx11-backend=false    # WAYLAND

  # Exclude GTK's browser backend from the desktop package.
  -Dbroadway-backend=false # MINIMAL
)
```

Labels document intent. Optional profiles enforce values. The renderer supplies
the current `SELECTED`, `REDUNDANT` and `IMPLIED OFF` annotations from evidence.

Use a short vocabulary for new comments:

| Tag | Reason for the choice |
| --- | --- |
| `WAYLAND` | Select Wayland and exclude X11, XWayland or GLX paths |
| `PIPEWIRE` | Audio backend policy and required compatibility APIs |
| `VULKAN` | Select the intended Vulkan rendering path |
| `PLATFORM` | A Linux or architecture constraint that needs an explicit flag |
| `SYSTEM` | Use packaged libraries/data rather than bundled copies or downloads |
| `PACKAGING` | Install layout, shared/static outputs and package structure |
| `MINIMAL` | Omit an unwanted feature, tool, example or documentation output |
| `WORKAROUND` | An upstream defect; explain it above the flag and link the issue |

Prefer one reason tag per row. Put a longer explanation on the preceding line,
keeping the trailing tag bare so the existing parser can retain it. Use
`PLATFORM` rather than a collection of OS-specific tags such as `LINUX`.
Do not add platform flags just to label options upstream already excludes.
Existing arbitrary tags remain valid. `ARCH` and `GENTOO` identify provenance;
record the actual reason and source reference when maintaining those choices.
`AUTO`, `DEFAULT`, `UNSET` and `REDUNDANT` are generated evidence labels rather
than human policy tags. A comment does not enforce a setting.

`check` reports policy conflicts. `render` proposes the policy values and marks
changes as `POLICY CHANGE`; it preserves other selected flags and comments.
Profiles can combine when their values agree. Conflicting groups, removed
profile options and invalid values require review.

## Choosing explicit flags and dependencies

Set the package's Wayland/X11 and audio policy explicitly. For optional
functionality you intend to ship, declare its build/runtime dependencies and
request the feature explicitly when the build system provides that control.
Keep compiler checks, platform probes and implementation details at their
upstream defaults unless the package needs a specific choice. This keeps the
recipe focused on decisions you maintain across releases.

| Situation | Build option strategy |
| --- | --- |
| Forbidden backend or unwanted feature | Explicit OFF, including independent child switches when needed |
| Promised backend, output library or format | Explicit ON/provider selection plus its declared dependencies |
| Required dependency checked unconditionally by upstream | Declare it; an additional enable flag may add nothing |
| Deliberate optional fallback | AUTO with declared build inputs and review of the configured result |
| Compiler, CPU or platform capability probe | Keep upstream detection unless a supported product choice requires otherwise |
| Proven child implication | Keep the root policy flag; a generated redundant child may stay commented |

AUTO means the build system's supported detection/default behavior. Do not
pass `auto` to a Boolean option that accepts only ON/OFF or true/false.

A dependency declaration makes an input available; it does not pass an enable
option or guarantee a feature survives configuration. Conversely, enabling a
feature does not declare the packages it needs. There is no Arch-wide rule
requiring every upstream option to be repeated. For a small option list,
explicitly listing the meaningful feature/output choices is reasonable;
repeating compiler probes and every current default adds maintenance work.

Use depends for runtime requirements, makedepends for additional build-only
inputs, checkdepends for extra test inputs and optdepends for optional runtime
integration. A package in depends is also available while building, so it
normally need not be repeated in makedepends. optdepends does not install a
build dependency. A shipped optional plugin may need both makedepends to build
and optdepends for its optional runtime libraries; a hard requirement of the
main executable belongs in depends. See
[PKGBUILD(5)](https://man.archlinux.org/man/PKGBUILD.5.en).

Use automatic detection for deliberate fallback behavior, then inspect the
configured result. `arch-meson` enables automatic feature options by default;
it can make their dependencies required. Boolean options and hand-written
optional dependency checks have their own behavior. The review explains the
global setting without treating it as a substitute for explicit policy flags.
Meson's feature AUTO therefore differs from an optional `dependency(...,
required: false)` probe. CMake Boolean ON is not a universal require-dependency
switch, and some Autotools `--with-...` requests silently fall back to OFF.
Inspect the configured summary and artifact, rather than treating SELECTED as
proof that the feature was built. Keep native wrappers' defaults in view; see
[Arch's Meson guide](https://wiki.archlinux.org/title/Meson_package_guidelines).

Use a clean build environment to verify the dependency list and exclude
accidental host influence, as recommended by
[Arch's clean-chroot guide](https://wiki.archlinux.org/title/DeveloperWiki:Building_in_a_clean_chroot).
ALPM resolves package names, providers and versions; upstream's build system
determines which features those installed inputs produce.

### Clean-chroot release validation

Run this for each release after recipe and `.SRCINFO` edits. Create the baseline
separately with devtools; `--chroot` names its parent containing `root/`:

```sh
waypure-validate --repo . --chroot /var/lib/archbuild/extra-x86_64 --plan PACKAGE
waypure-validate --repo . --chroot /var/lib/archbuild/extra-x86_64 PACKAGE
# Explicit local replacements, when needed; repeat --install for each archive:
waypure-validate --repo . --chroot /var/lib/archbuild/extra-x86_64 --install /path/dependency.pkg.tar.zst PACKAGE
# Explicit prior output as a seed for a build-dependency cycle:
waypure-validate --repo . --chroot /var/lib/archbuild/extra-x86_64 --bootstrap-install /path/prior-output.pkg.tar.zst PACKAGE
# Use the standard archbuild wrapper with doas authentication:
waypure-validate --repo . --builder extra-x86_64-build --auth doas --chroot /var/lib/archbuild-waypure-smoke/extra-x86_64 --plan PACKAGE
waypure-validate --repo . --builder extra-x86_64-build --auth doas --chroot /var/lib/archbuild-waypure-smoke/extra-x86_64 PACKAGE
```

`--plan [--json]` inspects literal recipe metadata, the baseline and explicit
archives without evaluating PKGBUILDs. Execution evaluates trusted PKGBUILDs
on the host for generated metadata, source verification and namcap. It then
uses `makechrootpkg -c -u` by default with forced builds and checks enabled.
Dependencies are installed only in the working chroot. The default keeps the
pristine baseline unchanged; `--builder extra-x86_64-build` runs the native
wrapper, which updates that baseline first. Wrapper receipts retain its before
and after snapshots and check that final host stages do not change it again.
The wrapper's `--chroot` directory must be named `extra-x86_64`; its parent is
passed to native `-r`. Baseline initialization is a separate devtools step.
Incomplete or outdated roots fail before execution, preventing the wrapper's
automatic recreation of older chroots. No host packages are installed.

`--install` rejects an archive with a selected output's package name. For an
intentional build-dependency cycle, `--bootstrap-install` explicitly permits a
prior archive with that name and requires its `.BUILDINFO` recipe SHA256. The
plan and receipt label it as a bootstrap input and record its archive and recipe
hashes. It passes the same architecture, policy, capability and integrity checks
as other local inputs. The seed is installed only into the fresh working chroot;
it is never counted as a new output. A forced build with checks and fresh output
provenance is still required. Bootstrap inputs can hide missing dependencies, so
success applies to the recorded inputs and does not establish a seed-free build.
The validator does not choose seeds or resolve bootstrap cycles automatically.

Run the validator as your normal user. `--auth doas` or `--auth sudo` elevates
only the native chroot commands and explicitly preserves the normal build-user
identity and destination directories. Host metadata/source verification and
lint remain unprivileged. Root invocation fails with this guidance. Installed
devtools still uses `sudo` internally to switch users even with `--auth doas`.
Without `--auth`, native devtools handles authentication as before. Authentication
may require your interactive terminal. V3 wrapper integration is not validated.

For a custom baseline, add `--update-baseline --auth doas` with the default
`makechrootpkg` builder. The validator runs `pacman -Syuu --noconfirm` through
`arch-nspawn` under the native baseline lock and checks its exit status before
building. Each package gets a fresh `makechrootpkg -c` working copy; this mode
omits the working-copy `-u` whose failure native devtools can ignore. Receipts
record the baseline transition, update log and GCC's resolved native CPU target.
The CPU query describes native compiler targeting; the configuration still
determines the flags actually used by each recipe.

Baseline evidence includes the main pacman/makepkg configurations, makepkg
language drop-ins, pacman configuration drop-ins and mirrorlist files, plus an
optional `etc/waypure-build-profile.json`. Added, removed or changed files alter
the baseline snapshot. Arbitrary configuration includes outside those paths
are not tracked. The generated personal profile below uses explicit servers.

#### Personal CachyOS builds

On a Zen 4/5 machine already using CachyOS's znver4 repositories, prepare a
dedicated profile without installing anything or changing the host:

```sh
tools/waypure-chroot --repo . --output tools/reports/cachyos-native/profile.log
# Review the generated configurations, then authenticate in your terminal:
bash tools/reports/cachyos-native/profile.log/setup.sh.log
# Build, check and audit the same archives you will install:
bash tools/reports/cachyos-native/profile.log/validate.sh.log PACKAGE
```

The default baseline is `/var/lib/archbuild-waypure-cachyos/native/root`.
Override its parent with `waypure-chroot --chroot PATH` during preparation.
Existing profile directories and existing baselines are refused. Preparation
uses installed devtools defaults, changes C/C++ and Fortran to `-march=native
-mtune=native -O3`, and adds Rust `-C target-cpu=native -C opt-level=3`. Go uses
`GOAMD64=v4`. Hardening flags, LTO and separate debug symbols are retained;
recipe-specific exceptions still apply. `native` targets the CPU doing the
build, so these are personal packages even though `CARCH` remains `x86_64`.
There is no universal guarantee that these flags maximize application speed.

The generated pacman configuration uses explicit HTTPS servers from the host's
CachyOS znver4/core/extra repositories, then CachyOS and Arch fallbacks, plus
multilib when enabled. Package signatures remain required. Setup installs only
base-devel and keyrings into the new baseline; recipe dependencies are installed
in disposable working copies. No host package installation occurs.

A single compilation produces the normal packages and separate `-debug`
archives when the recipe permits debugging. The validator checks both and
records exact paths/hashes in `PACKAGE/.waypure-validation.json.log`. Install
the recorded primary archives with `doas pacman -U`; install matching debug
archives only when wanted. No preliminary host build is needed. This validates
the recorded CachyOS environment; use a separate generic Arch build when you
need generic Arch compatibility evidence.

Sources are verified explicitly before building because devtools' default
makepkg arguments skip integrity checks. Fresh outputs and build logs go to an
exclusive `.waypure-validation-*.log/` directory in each recipe directory.
Every declared split and generated debug archive must pass archive policy and
namcap checks. If host namcap attributes a library shipped in the same archive
to an installed package, the validator checks the actual ELF dependency,
SONAME and ABI before recording it as an internal-library note. Only regular
files and same-directory SONAME symlink chains qualify; unresolved dependencies
and unrelated errors still fail. The same applies to pkg-config ownership only
when the reported provider and consumer files ship in that archive and native
pkg-config verifies their requirements and versions in an isolated search path.
Host files and uninstalled-module substitutes cannot satisfy that check.

Bundled top-level Python modules can likewise satisfy a reported dependency:
the validator checks static imports and regular providers in this interpreter's
standard site-packages directory. Native extensions must match its ELF ABI and
export the expected Python initialization entry point. Archive Python code is
never imported. Mixed Python/library findings are split into independent checks;
missing providers and unfamiliar reason types remain errors.

A debug build-ID symlink reported missing by standalone namcap
is checked against regular files in the current build's primary archives with
matching identity/version and recipe provenance. Verified links remain visible
as notes identifying the companion archive. Missing targets and unrelated lint
errors still fail. Recipe/source hashes, `.BUILDINFO`, baseline package records,
explicit input hashes and commands are retained in
`PACKAGE/.waypure-validation.json.log`. Only a completed validation writes a
`PASS` receipt; all selected recipes and outputs are rechecked before receipts
are written. A later selected recipe cannot leave an earlier changed output
marked successful. A new execution invalidates an earlier receipt before checking.
Warnings remain visible. Existing local build archives are never accepted as
the current run's output. Receipt validity applies to the recorded inputs.

A successful build establishes dependency sufficiency in that recorded baseline
and working copy, including any `--install` or `--bootstrap-install` inputs. With default makechrootpkg,
the update is requested but its failure is not reliably propagated, so update
success is not independently verified. Native archbuild checks its baseline
update before proceeding. Its defaults also install outputs in the working
chroot and compare against repository packages; unpublished outputs may skip
that comparison. The validator's own archive policy and namcap gates remain
decisive. Inspect the build log and `.BUILDINFO` actual package versions.
Base-devel packages
are already present, and extra injected packages may provide undeclared inputs.
Success does not establish dependency necessity or minimality. Runtime loading,
optional paths and dynamically selected features still need review and suitable
tests. The validator does not prune dependencies, choose replacements, order
local dependency builds or bootstrap cycles. Prepared Cargo/Python source
checks are supporting static evidence, separately labelled from the source
actually selected by the chroot build. Existing recipe warnings remain work
for the package-maintenance pass.

makepkg's `autodeps` supplements declared dependencies by inspecting linked
libraries. It does not select build features or account for programs, data,
plugins and libraries loaded through `dlopen`. Keep those dependencies explicit
and review optional runtime fallbacks. The audit supports generated SONAME
dependency names, but neither auto-dependencies nor a clean ELF scan proves the
full runtime dependency closure is X11-free.
Each TOML flag string is one native argument; include its value directly,
including spaces or array syntax, without adding Bash quotes around the string.
The renderer adds shell quoting when needed.

```sh
waypure-options check 4.24.1
waypure-options render 4.24.1 --profile WAYLAND --prune-redundant
waypure-options render 4.24.1 --no-profiles
```

Policy intent survives pruning: when source rules guarantee a requested value,
`check` accepts that value without demanding a second explicit flag. Build
observations cannot justify this omission. An existing `# ARCH` comment remains
a maintainer's note; the recorded Arch commit provides the comparison evidence.

## Reading the proposed array

| Label | Meaning |
|---|---|
| `SELECTED` | An active flag retained from the recipe |
| `POLICY CHANGE` | A proposed addition/value change from a named profile |
| `REDUNDANT` | Another explicit setting guarantees a selected flag stays OFF |
| `IMPLIED OFF`, `IMPLIED ON` | An unset option follows another setting; reason attached |
| `DEFAULT OFF`, `DEFAULT ON`, `DEFAULT` | A known source default |
| `AUTO` | The declared value asks the build system to choose |
| `UNKNOWN` | Source logic or detection prevents a prediction |
| `UNSET` | An existing commented option retained from the recipe |
| `OBSERVED=...` | A separate observation from one configured build |

A leading `#` means the array does not pass that option. An observed OFF value
does not mean an option is redundant. The pruning pass rechecks omissions
together, retaining a root setting when removing everything would lose the
constraint.

UNSET rows also explain their effective source value, which may differ from
the commented example. String and numeric defaults use DEFAULT: an empty string,
the string OFF or a count of zero does not mean a feature is disabled.

An explicit flag that only matches today's default stays active. This preserves
packaging intent when upstream changes a default in a later release.

Use one native option per array row so its Bash quoting and attached comment
can be retained. CMake `-B`/`-G` setup rows can still contain multiple arguments.
Computed Bash values remain untouched and receive a configure-time review note.
Generated annotations keep human notes after `| NOTE:` so copying and reviewing
an array again does not accumulate old status labels.

For example, Zvbi's source chooses capture defaults by host OS. With no explicit
capture flags on Arch Linux, the proposal contains:

```bash
# --disable-bktr # [DEFAULT OFF] host_os=linux-gnu
# --enable-dvb # [DEFAULT ON] host_os=linux-gnu
# --enable-proxy # [DEFAULT ON] host_os=linux-gnu
# --enable-v4l # [DEFAULT ON] host_os=linux-gnu
```

These commented rows leave configure in control. If the recipe selects
`--disable-v4l`, source rules also make the default proxy OFF. The proposal
explains that dependency and can omit an additional explicit
`--disable-proxy`. A compiler/library probe that the reader cannot establish
stays `UNKNOWN`, with its variable or source reason attached. `feature:dvb` is
an internal catalogue key; use native `--enable-dvb` / `--disable-dvb` flags in
the recipe.

## Commands and files

```sh
waypure-options                          # same as review, without a network read
waypure-options -C gtk4 review
waypure-options diff 4.24.1 4.24.2
waypure-options snapshot src/project-1.2 --backend meson -o options/1.2.catalog.json.log
waypure-options render 4.24.2 --prune-redundant -o options/4.24.2.options.sh.log
waypure-options explain 6.12.0 FEATURE_xcb_xlib
waypure-options check 6.12.0 --build src/build
```

Source catalogues use `options/<version>.catalog.json.log`. Old Qt/FFmpeg
`<version>.json.log` files can be read without conversion or replacement.
Identical snapshots keep their modification time; differing immutable snapshots
require a different filename. Review reports and proposed arrays are generated
outputs and can be refreshed. The repository ignores the `.log` files.
Review retains a content-qualified variant when collector evidence changes and
records the current choice in `<version>.current.json.log`. An explicit
`snapshot -o PATH` still refuses to replace a differing immutable snapshot.

Use `--source` when more than one source tree matches, `--backend` when a project
supports multiple build systems, and `--array` for a different packaging array
name. `package.source` in the TOML file can provide an explicit source path/glob.
`package.backend` selects the collector. `package.array` selects the recipe
array for review, render, check and audit.
The conventional `_cmake_args` and `_meson_args` names work when their usual
`_options` arrays are absent. Explicit array overrides take precedence.
`--version` supplies a release label when metadata is computed. During a package
review, unresolved source versions use pkgver with that provenance recorded;
the collector does not execute a project version script to find out.

If the upstream version and pkgver use different labels, verify the mapping
once and record the native source version in the optional TOML file:

```toml
[package]
source_version = "1.4.363" # upstream project version; pkgver is 1.4.363.0
```

Review rejects a source that does not match `source_version` and retains both
version labels in its catalogue. Build observations must match the known native
version; the package label does not admit observations from another release.
`--version` also retains the original source version and records the explicit
override. Neither mechanism proves that an RC is the final release. Use a source
hint for Git checkouts or unusual archive directory names; it never causes the
tool to choose an arbitrary older source directory.

### Cargo and Python source checks

Cargo and Python catalogues retain native metadata rather than inventing
configure switches. `snapshot`, `review`, `diff` and `check` support both;
`render` and generic configured-build observations remain native-option tools.
Repository audit reports language findings under `SOURCE`, with `OPTIONS`
marked `SKIP`. Native build-system markers take precedence in mixed trees;
use `--backend` to select Cargo or Python explicitly where appropriate.
Virtual workspaces have no native project name; package reviews use the package
directory as the stable comparison identity and record that limitation.
For standalone virtual-workspace snapshots, pass the same `--project NAME`
across releases so release directory names do not prevent comparison.

```sh
waypure-options source-check --source src/project-1.2 --backend cargo --json
waypure-options source-check --source src/project-1.2 --backend cargo --resolve --package application --no-default-features --features wayland --cargo-home src --json
waypure-options source-check --source src/project-1.2 --backend python --extra optional-feature --json
```

Cargo capture records workspace/package declarations, renamed dependencies,
target/build/dev distinctions, feature lists and hashes of manifests, lockfiles,
configuration and build scripts. Declaration presence does not establish that
a crate or feature is active. `--resolve` explicitly runs installed Cargo's
`metadata --frozen --filter-platform ...`: locked, offline and without compiling
the project or running build scripts. It uses the installed compiler with
wrappers disabled. Configuration environment variables are rejected except
the reviewed numeric `MACOSX_DEPLOYMENT_TARGET` used by Zed. Cargo retains its
normal merge/force behavior for that variable; relative deployment paths are
rejected. Compiler-query rustflags are suppressed and inherited
`RUSTC_BOOTSTRAP` is removed to prevent custom codegen plugin execution during
introspection. Actual recipe/build flags are untouched. Query evidence records
this boundary: custom cfg activation from build flags is unobserved and
target-conditional dependencies can differ from an actual build.
Missing cached inputs, incompatible toolchains and unresolved graphs remain
diagnostics. Select the same packages, features and target as the real build;
recipe Cargo arrays are not automatically inferred. Native metadata can include
workspace, host and dev feature unions, so resolved capability findings warn
about possible activation rather than claiming exact runtime linkage. Known
crate mappings are explicit; arbitrary feature names are not policy evidence.
See [Cargo metadata](https://doc.rust-lang.org/cargo/commands/cargo-metadata.html).

Python capture reads `pyproject.toml`, `setup.cfg` and bounded literal
`setup.py` declarations without importing projects or executing backends. It
separates build requirements, runtime requirements, extras and development
groups, and reports dynamic or incomplete metadata. Requirement names are PyPI
identifiers; the tool does not guess Arch names by adding `python-`. Installed
`.dist-info/METADATA` and `.egg-info/PKG-INFO` under `site-packages` are also
checked during archive audits. The optional `packaging` library validates and
evaluates requirement markers; its absence produces an explicit coverage
warning. Backend hook requirements, arbitrary setup code, native extensions
and runtime imports cannot all be established statically. See the
[pyproject specification](https://packaging.python.org/en/latest/specifications/pyproject-toml/).

Neither source check proves that all native dependencies or policy conflicts
have been found. Use them with recipe/archive checks, clean-chroot builds and
tests for the package's intended runtime features. `--strict` makes source
warnings fail when incomplete evidence must stop automation.

## Arch packaging comparisons

```sh
waypure-options review --arch             # fetch current official packaging
waypure-options review --offline          # recorded commit; freshness unverified
waypure-options arch snapshot --pkgbase gtk4 -o /tmp/gtk4-arch.json.log
waypure-options arch snapshot --pkgbase gtk4 --local /path/to/arch/gtk4 --ref HEAD -o /tmp/gtk4-local.json.log
waypure-options arch diff /tmp/old.json.log /tmp/new.json.log
```

The official source is
`https://gitlab.archlinux.org/archlinux/packaging/packages/<pkgbase>.git`.
Snapshots record the resolved commit, metadata, and packaging file hashes/text.
Comparisons include PKGBUILD, .SRCINFO when present, patches and other packaging
files. Without .SRCINFO, only literal PKGBUILD version/epoch fields are read.
Shell expressions remain unresolved.

Review writes a commit-to-commit diff and a separate Arch-versus-local recipe
diff. Each package release anchors its comparison to the previously recorded
Arch commit, or the first fetched commit when no history exists. Repeated and
offline reviews retain that anchor; a new pkgver starts from the last recorded
commit. Diff filenames include both commit IDs, preserving earlier comparisons.
The anchor is recorded in `options/arch/<pkgbase>-<version>.baseline.json.log`.
Network failures are explicit; offline operation does not claim to show the
latest Arch state. Epoch differences require review.

## Adapter boundaries

| Adapter | Source catalogue and explanation |
|---|---|
| Qt 6 | Qt feature rules and its own evaluator; existing conservative proofs |
| FFmpeg | Configure help/components; literal `_deps` prerequisites |
| Meson | Native source introspection, option types, defaults, choices and global auto-feature settings |
| CMake | Literal declarations, dependent options, verified declaration wrappers and CTest's `BUILD_TESTING` |
| Autoconf | Literal declarations, bounded variable/OS/conditional defaults, generated help and configure variable arguments |

Qt's expression semantics differ from ordinary CMake conditions. Meson source
introspection can include subproject options that differ from a configured
build. Generic CMake macros and Autoconf M4/shell expressions may generate
options or alter defaults that a literal collector cannot establish. Those
cases need source/configure review; the tool does not claim to interpret
arbitrary build scripts. CMake's `STRINGS` property provides GUI suggestions,
whereas Meson choice lists constrain valid values.
Meson subproject names retain their `subproject:option` namespace; array choices
are checked per member. Conditional or function-scoped CMake declarations stay
unknown and cannot justify pruning a flag.

Meson `--auto-features=disabled`, `--auto-features disabled` and
`-Dauto_features=disabled` describe the same setting. The parser retains the
selected spelling and detects conflicting duplicates. Meson rejects mixing the
long spelling with `-Dauto_features`, including across a wrapper and the recipe;
use the wrapper's spelling for an override. For recipes invoking
`arch-meson`, it verifies the installed wrapper's full structure and reads its
literal `exec meson setup` defaults without running it. Arch's source validation
preamble is recognized. Argument mutations or other unfamiliar shell code leave
the wrapper defaults unknown. An unavailable or unrecognized wrapper leaves
those defaults unknown. A global setting affects `feature` options whose value
is `auto`; booleans and explicit enabled/disabled choices retain their values.
Explicit OFF flags remain present when global auto-feature disabling would also
disable them. Meson deprecations retain their subproject namespace, and value
deprecation maps apply to the selected values alone.

CMake wrapper recognition verifies bounded declaration templates in the source,
including SDL's option and subsystem wrappers. Matching names alone establish
nothing. Forwarded arguments retain their quoting; unquoted list expansions or
other uncertain argument boundaries cannot justify pruning. Bracket arguments
and escaped dollars are distinguished from expandable parameters. Helper and
temporary-variable writes invalidate proofs when they affect another option;
helper source files are retained as evidence. Changes to the wrapper body can
make it unrecognized; unresolved
defaults stay unknown. A local `CTest.cmake` prevents assuming the builtin CTest
declaration. Arbitrary functions/macros still require native configure review.

Autoconf default inference follows literal assignments, simple variable chains,
shell `case`/`if test` branches and bounded `AS_CASE`/`AS_IF` forms. It uses an
explicit Arch Linux/x86_64 context; an explicit `--host` or `--build` clears the
corresponding platform assumptions, including the host inherited from `--build`.
The reader handles literal `AS_VAR_SET`/`AS_VAR_COPY` writes and short local
`AC_DEFUN` helpers; dynamic targets and recursive helpers stay unknown.
Selected options feed the source rules, so
dependent defaults reflect the recipe's choices. Equal outcomes of an unknown
branch can establish a common value. External probes, arbitrary commands and
unsupported shell/M4 mutations keep the affected values unknown. The reader
does not run configure to establish these defaults.

Autoconf accepts literal configure arguments such as `PSDelegate=/usr/bin/gs`
as setup arguments and preserves their Bash quoting and position among flags.
Assignments to `enable_*` or `with_*` variables leave that option's effective
value unknown and cannot justify pruning. Wrapped help descriptions
still declare their options. Complete configure help allows the checker to flag
unadvertised switches; source-only discovery stays incomplete. Undocumented
inputs referenced by the generated script receive an unverified notice rather
than a claim that their behavior is supported. Unselected internal variables
stay out of the normal proposal. Audit reports selected unverified inputs as
warnings. Comparisons between source-only and generated-help catalogues flag
the coverage difference and skip additions/removals of help-only options.
Build observations are checked against the recorded project and native version
when that identity is present in the build metadata.

The X11 dependency policy recognizes makepkg SONAME dependency formats as well
as package names, including `lib:libX11.so.6` and `libX11.so=6-64`. This supplements
archive ELF inspection; runtime programs and dlopen dependencies still need
declared packaging intent.

FFmpeg and generated Autoconf collectors invoke upstream `configure --help` in
temporary working directories. Qt/CMake collection reads source files; Meson
uses its source introspection API. Only explicit `review --prepare` executes
the trusted recipe through makepkg.

## Deprecations and automation

Reviews report selected deprecated options as notes. Add `--strict` to review
or check to fail on them. Audit reports them as warnings; strict audit fails
those warnings. Removed options and invalid selected values produce errors.
Unselected upstream additions/default changes appear in the release diff.

Meson exposes structured metadata. CMake/Qt recognize explicit DEPRECATION
messages mentioning an option; Qt/FFmpeg also recognize explicit notices in
labels/help. Generated M4/macros and implicit upstream transitions can need
manual/native configure review. A feature for enabling deprecated APIs is not
itself deprecated just because of its name. No background monitor is installed.

Tool CI runs the tests on Python 3.11 and 3.13 with native Meson/CMake/ELF tools,
and the ALPM tests with pyalpm in an Arch container. It also audits metadata for
changed recipes and patches. GitHub container jobs lack the systemd-nspawn
support that devtools needs, so clean-chroot release validation runs locally
with `waypure-validate`; the unit tests cover its plans and receipts.
Source-option reviews use locally prepared sources and retained catalogues.
CI does not compile the package collection or publish packages.

## Tests

```sh
python -B -m unittest discover -s tools/tests -v
```

Use [uv's Python selection](https://docs.astral.sh/uv/concepts/python-versions/)
to test the same versions as CI without replacing Arch's system Python:

```sh
uv run --no-project --python 3.11 python -B -m unittest discover -s tools/tests -v
uv run --no-project --python 3.13 python -B -m unittest discover -s tools/tests -v
```

uv can download a missing interpreter. Add `--offline --no-python-downloads`
to use local interpreters alone. Keep native tools such as Meson, CMake and
readelf installed; uv manages Python here. These commands need no project
environment or lockfile because the toolkit uses the standard library.
uv is optional and does not speed up audit execution or native package builds.

The suite includes the original standalone builder's 32 tests, migrated to
unittest without a pytest dependency, plus regressions for split archives,
version matching and native lint errors. Tests isolate the builder's cache
and fake package installation commands; they do not install packages.
Native backend tests use temporary local/sync databases and archives, including
alternate roots and configuration includes. The Arch-container CI job exercises
pyalpm; portable CI and uv also cover the bulk CLI adapter.
The optional native namcap test uses the host PATH so Arch's wrapper selects
the system Python that owns its namcap module, including during a uv run.

Tests exercise native source introspection, preserved shell arguments, policy
validation, immutable baselines, Qt's evaluator, and real local Git history for
Arch comparisons. Installation tests use a temporary prefix.

## Terminal colors and machine output

`waypure-options` and `waypure-audit` use colors when stdout is a terminal.
Passes and additions appear green, warnings and changes yellow, failures and
removals red, and notes/cached evidence cyan. Skipped checks appear dim.
Labels remain readable in redirected reports.

```sh
waypure-audit --repo . --color=always | less -R
waypure-audit --repo . --json > /tmp/waypure-audit.json.log
waypure-options -C cairo review --color=never
NO_COLOR=1 waypure-audit --repo .
```

`--color=auto` is the default. Pipes, redirected output, `TERM=dumb` and a
nonempty `NO_COLOR` disable automatic colors. Explicit `--color=always` or
`--color=never` overrides those settings. For `waypure-options`, the flag works
before or after its subcommand. Saved reports, catalogues, JSON and copyable
Bash arrays stay plain text, including `render` output with forced colors.
Argument help stays plain across Python versions.

