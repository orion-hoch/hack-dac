#!/usr/bin/env bash
# Fresh Ubuntu 22.04 x86_64 -> everything pipeline_v3 needs. No step needs a person. Re-running is safe.
#   bash setup.sh            (installs under $DEID_HOME, default ~/deid)
set -euo pipefail
B=$(cd "$(dirname "$0")" && pwd); P=${DEID_HOME:-$HOME/deid}; J=$(nproc)
mkdir -p $P/opt/bin $P/private $P/tmp $P/out $P/logs $P/src
SUDO=""; [ "$(id -u)" = 0 ] || SUDO="sudo -n"
# 1. system build tools (the only step that needs root; on a rented VM the default user has passwordless sudo)
$SUDO apt-get update -y
DEBIAN_FRONTEND=noninteractive $SUDO apt-get install -y --no-install-recommends build-essential curl ca-certificates \
  xz-utils aria2 pkg-config autoconf automake libtool zlib1g-dev libbz2-dev liblzma-dev libzstd-dev liblz4-dev \
  libarchive13 libarchive-dev git
# 2. Python 3.12.3 exactly, as on the Spark (the letter map uses Python's Unicode tables): uv-managed CPython
if [ ! -x $P/opt/uv/uv ]; then curl -LsSf https://astral.sh/uv/0.4.30/install.sh | env UV_INSTALL_DIR=$P/opt/uv UV_NO_MODIFY_PATH=1 sh; fi
$P/opt/uv/uv python install 3.12.3
[ -x $P/venv/bin/python ] || $P/opt/uv/uv venv --python 3.12.3 $P/venv
$P/opt/uv/uv pip install --python $P/venv/bin/python -r $B/code/requirements.txt
$P/venv/bin/python -c "import sys, unicodedata; assert sys.version.startswith('3.12.3'), sys.version; print('python', sys.version.split()[0], 'unicode', unicodedata.unidata_version)"
# 3. libarchive: the system libarchive.so.13 (step 1); if it is missing, build 3.7.7 in user space
if ! ldconfig -p | grep -q libarchive.so.13; then
  cd $P/src && curl -fsSLO https://www.libarchive.org/downloads/libarchive-3.7.7.tar.xz && tar xf libarchive-3.7.7.tar.xz
  cd libarchive-3.7.7 && ./configure --prefix=$P/opt/libarchive --without-xml2 --without-openssl --disable-bsdtar \
    --disable-bsdcpio --disable-bsdcat -q && make -j$J -s && make install -s
fi
# 4. unrar (RAR reading; licence allows extraction) from the bundled source
if [ ! -x $P/opt/bin/unrar ]; then
  rm -rf $P/src/unrar && tar xzf $B/src/unrar.tar.gz -C $P/src && make -C $P/src/unrar -j$J -s && cp $P/src/unrar/unrar $P/opt/bin/
fi
# 5. LibreDWG 0.13.3 (dwgread, the DWG gate) from the bundled source
if [ ! -x $P/opt/libredwg/bin/dwgread ]; then
  rm -rf $P/src/libredwg-0.13.3 && tar xJf $B/src/libredwg.tar.xz -C $P/src
  cd $P/src/libredwg-0.13.3 && ./configure --prefix=$P/opt/libredwg --disable-bindings --disable-docs --disable-shared -q \
    && make -j$J -s && make install -s
fi
# 6. 7-Zip 7zz (second 7z gate), x86_64 build
if [ ! -x $P/opt/bin/7zz ]; then
  cd $P/src && curl -fsSLO https://www.7-zip.org/a/7z2408-linux-x64.tar.xz && tar xJf 7z2408-linux-x64.tar.xz 7zz && cp 7zz $P/opt/bin/
fi
# 7. C helper of the independent verifier (deep leak scan)
gcc -O2 -shared -fPIC -o $B/verifier/src/libscan.so $B/verifier/src/scanlib.c
# 8. environment for every later script
cat > $P/env.sh <<ENV
export DEID_HOME=$P DEID_BUNDLE=$B PYTHONDONTWRITEBYTECODE=1 DEID_TMP=$P/tmp
export DEID_PSEUDO_KEY_FILE=$P/private/pseudo.key DEID_UNRAR=$P/opt/bin/unrar DEID_7ZZ=$P/opt/bin/7zz
export DEID_DWGREAD=$P/opt/libredwg/bin/dwgread DEID_LIBARCHIVE=$P/opt/libarchive/lib
export OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1
ENV
. $P/env.sh
for x in "$DEID_UNRAR" "$DEID_7ZZ" "$DEID_DWGREAD"; do [ -x "$x" ] || { echo "MISSING $x"; exit 1; }; done
echo "setup OK under $P. Next: copy the private files (PRIVATE_FILES.txt), then bash fetch.sh, bash parity_check.sh."
