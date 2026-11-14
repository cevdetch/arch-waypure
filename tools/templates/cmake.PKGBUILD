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
makedepends=('cmake' 'ninja')
# checkdepends=()
# optdepends=()
provides=("${_pkgname}=${pkgver}")
conflicts=("${_pkgname}")
source=() # Use an official release archive with a unique filename.
sha256sums=() # Fill with updpkgsums after setting source.

build() {
  local _cmake_options=(
    -B build -G Ninja
    -D CMAKE_BUILD_TYPE=None # PACKAGING
    -D CMAKE_INSTALL_PREFIX=/usr # PACKAGING
    # Add only options supported by this upstream release.
  )

  cmake -S "${_pkgname}-${pkgver}" "${_cmake_options[@]}"
  cmake --build build
}

# Keep this function only when upstream defines CTest tests.
check() {
  ctest --test-dir build --output-on-failure \
    --parallel "${CTEST_PARALLEL_LEVEL:-$(nproc)}" --no-tests=error
}

package() {
  DESTDIR="${pkgdir}" cmake --install build
  # Install any required license files not installed by upstream.
}
