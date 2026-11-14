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
makedepends=()
# checkdepends=()
# optdepends=()
provides=("${_pkgname}=${pkgver}")
conflicts=("${_pkgname}")
source=() # Use an official release archive with a unique filename.
sha256sums=() # Fill with updpkgsums after setting source.

build() {
  local _configure_options=(
    --prefix=/usr # PACKAGING
    --libdir=/usr/lib # PACKAGING
    --libexecdir=/usr/lib # PACKAGING
    --sysconfdir=/etc # PACKAGING
    --localstatedir=/var # PACKAGING
    --runstatedir=/run # PACKAGING
    # Add only options supported by this upstream release.
  )

  cd "${_pkgname}-${pkgver}" || return
  ./configure "${_configure_options[@]}"
  make
}

# Keep this function only when upstream provides a check target.
check() {
  make -C "${_pkgname}-${pkgver}" check
}

package() {
  make -C "${_pkgname}-${pkgver}" DESTDIR="${pkgdir}" install
  # Install any required license files not installed by upstream.
}
