# SPDX-License-Identifier: 0BSD
# Maintainer: cevdetch (https://github.com/cevdetch)

_pkgname=
pkgname=${_pkgname}-waypure
pkgver=
pkgrel=1
# epoch=1 # Match Arch when present; include it in provides too.
pkgdesc=''
arch=('x86_64')
url=''
license=() # Fill with upstream SPDX identifiers.
depends=()
makedepends=('meson')
# checkdepends=()
# optdepends=()
provides=("${_pkgname}=${pkgver}")
conflicts=("${_pkgname}")
source=() # Use an official release archive with a unique filename.
sha256sums=() # Fill with updpkgsums after setting source.

build() {
  local _meson_options=(
    # arch-meson promotes feature-auto to enabled.
    # Add only options supported by this upstream release.
  )

  arch-meson "${_pkgname}-${pkgver}" build "${_meson_options[@]}"
  meson compile -C build
}

# Keep this function only when upstream provides tests.
check() {
  meson test -C build --print-errorlogs
}

package() {
  meson install -C build --destdir "${pkgdir}" --no-rebuild
  # Install any required license files not installed by upstream.
}
