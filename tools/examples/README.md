# Audit examples

Run these examples from the repository root. The script creates synthetic
`demo-waypure` recipes and archives in a new temporary directory, then prints
their paths and the audit findings. It leaves the files there for inspection.
It does not build or install a package.

```sh
example_root=$(mktemp -d /tmp/waypure-examples.XXXXXX)
python -B tools/examples/audit-gaps.py --directory "$example_root/fixtures" --namcap
```

Omit `--namcap` if namcap is unavailable. The bundled-library example needs
`cc` and `readelf`; the script skips that example if either is missing.
The script returns zero when the checks match their expected results,
including the examples that should fail an audit.

| Example | Concrete input | Expected finding |
| --- | --- | --- |
| `baseline` | Matching recipe and metadata | `RECIPE PASS` |
| `license-mismatch` | `license=('MIT')` in PKGBUILD, `license = BSD-2-Clause` in .SRCINFO | `srcinfo.fields` error |
| `checksum-count` | Two `source` entries, one SHA256 checksum | `source.checksums` error |
| `stale-archive` | Correct package version, archive older than PKGBUILD and .SRCINFO | `artifact.stale` warning |
| `bundled-x11` | Dummy ELF at `usr/lib/renamed.so` with SONAME `libX11.so.6`, without DT_NEEDED entries | `artifact.x11` error |
| `license-files` | MIT archive with its license under `usr/share/licenses/wrong-name/` | Native namcap license error; requires `--namcap` |

These are demonstrations of checks, not findings in the repository's real
packages. The script compiles one test function into the dummy shared object.
It does not copy the system's X11 library. The invented package names lack
official Arch evidence, so a CLI audit also reports that limitation.

Inspect the renamed-library case:

```sh
tools/waypure-audit --repo "$example_root/fixtures" bundled-x11 --metadata-only --artifacts
tar -xOf "$example_root/fixtures/bundled-x11/demo-waypure-1.0-1-x86_64.pkg.tar" usr/lib/renamed.so > "$example_root/renamed.so"
readelf --dynamic "$example_root/renamed.so"
```

The audit returns 1 for this intentional policy violation. In readelf's output,
look for `(SONAME) ... [libX11.so.6]`. The file's name and its lack of linked
dependencies do not establish that the archive is free of X11 libraries.

Inspect a recipe mismatch or the native license result:

```sh
cat "$example_root/fixtures/license-mismatch/PKGBUILD" "$example_root/fixtures/license-mismatch/.SRCINFO"
tools/waypure-audit --repo "$example_root/fixtures" checksum-count --metadata-only
namcap "$example_root/fixtures/license-files/demo-waypure-1.0-1-any.pkg.tar"
```

Native namcap can print `E:` findings and still return zero. Both the audit's
explicit `--namcap` option and the builder's `--namcap` option parse those
findings instead of relying on its exit status alone.

For a real package, finish the recipe, generate `.SRCINFO`, then build. Run
`waypure-audit --repo . <package> --artifacts` afterward. Add `--namcap` for
trusted inputs when you want native packaging lint too. See the
[maintenance guide](../README.md) for the full workflow and check limits.
