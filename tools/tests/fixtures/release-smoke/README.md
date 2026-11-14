# Native release-validation fixture

This synthetic package installs a text file and its MIT license. It exercises
real makepkg/devtools, doas authentication and validator receipts in the Arch
CI job without building any repository package or installing host packages.

The fixture has real source checksums and generated `.SRCINFO`. Build outputs
and validation receipts are ignored. The native CI job retains those outputs
and logs for inspection; unit tests separately cover failed builds, stale
outputs and changing inputs.

The package requires a marker produced by `check()`, so skipping the check
stage makes the native smoke build fail. A local native makepkg test exercises
both paths; the CI job exercises that contract through clean-chroot validation.
