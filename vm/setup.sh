#!/usr/bin/env bash
# Fresh Ubuntu 22.04 x86_64 -> everything pipeline_v3 needs. No step needs a person. Re-running is safe.
#   bash vm/setup.sh          (repository checked out anywhere; installs under $DEID_HOME, default ~/deid)
set -euo pipefail
R=$(cd "$(dirname "$0")/.." && pwd); P=${DEID_HOME:-$HOME/deid}; J=$(nproc)
mkdir -p $P/opt/bin $P/private $P/tmp $P/out $P/logs $P/src
SUDO=""; [ "$(id -u)" = 0 ] || SUDO="sudo -n"
$SUDO apt-get update -y
DEBIAN_FRONTEND=noninteractive $SUDO apt-get install -y --no-install-recommends build-essential curl ca-certificates \
  xz-utils aria2 pkg-config autoconf automake libtool zlib1g-dev libbz2-dev liblzma-dev libzstd-dev liblz4-dev \
  libarchive13 libarchive-dev wamerican wbritish libgl1 libglib2.0-0 git
# Python 3.12.3 exactly, as on the Spark (the letter map uses Python's Unicode tables)
UV=$(ls $P/opt/uv/bin/uv $P/opt/uv/uv 2>/dev/null | head -1 || true)
if [ -z "$UV" ]; then curl -LsSf https://astral.sh/uv/0.4.30/install.sh | env UV_INSTALL_DIR=$P/opt/uv UV_NO_MODIFY_PATH=1 sh; UV=$(ls $P/opt/uv/bin/uv $P/opt/uv/uv 2>/dev/null | head -1); fi
$UV python install 3.12.3
[ -x $P/venv/bin/python ] || $UV venv --python 3.12.3 $P/venv
$UV pip install --python $P/venv/bin/python -r $R/code/requirements.txt
$P/venv/bin/python -c "import sys, unicodedata; assert sys.version.startswith('3.12.3'), sys.version; print('python', sys.version.split()[0], 'unicode', unicodedata.unidata_version)"
# (the word list used by code/tools/filter_ids.py ships in code/data/words_en.txt: no host dictionary needed)
# libarchive: system libarchive.so.13; else build 3.7.7 in user space
if ! ldconfig -p | grep -q libarchive.so.13; then
  cd $P/src && curl -fsSLO https://www.libarchive.org/downloads/libarchive-3.7.7.tar.xz && tar xf libarchive-3.7.7.tar.xz
  cd libarchive-3.7.7 && ./configure --prefix=$P/opt/libarchive --without-xml2 --without-openssl --disable-bsdtar \
    --disable-bsdcpio --disable-bsdcat -q && make -j$J -s && make install -s
fi
# unrar (extraction only), from rarlab's source
if [ ! -x $P/opt/bin/unrar ]; then
  cd $P/src && curl -fsSL -o unrarsrc.tar.gz https://www.rarlab.com/rar/unrarsrc-7.0.9.tar.gz && tar xzf unrarsrc.tar.gz
  make -C $P/src/unrar -j$J -s && cp $P/src/unrar/unrar $P/opt/bin/
fi
# LibreDWG 0.13.3 (dwgread: the DWG gate). Without it the DWG handler leaves DWG content unchanged.
if [ ! -x $P/opt/libredwg/bin/dwgread ]; then
  cd $P/src && curl -fsSLO https://ftp.gnu.org/gnu/libredwg/libredwg-0.13.3.tar.xz && tar xJf libredwg-0.13.3.tar.xz
  cd libredwg-0.13.3 && ./configure --prefix=$P/opt/libredwg --disable-bindings --disable-docs --disable-shared -q \
    && make -j$J -s && make install -s
fi
# 7-Zip 7zz (second 7z gate)
[ -x $P/opt/bin/7zz ] || { cd $P/src && curl -fsSLO https://www.7-zip.org/a/7z2501-linux-x64.tar.xz && tar xJf 7z2501-linux-x64.tar.xz 7zz && cp 7zz $P/opt/bin/; }
# OCR font (SIL OFL). Not shipped (its licence file names its authors). The exact file the Spark used comes from
# Ubuntu 24.04 fonts-liberation 1:2.1.5-3; its sha256 is checked, so painted stand-ins are identical on both machines.
F=$R/code/deid/vision/fonts; mkdir -p $F
FSHA=4659bc0c58c5028dd488ec928d41d9265db43d9b669fc14ca8b0832daca7b144
if [ "$(sha256sum $F/LiberationSans-Regular.ttf 2>/dev/null | cut -d' ' -f1)" != "$FSHA" ]; then
  cd $P/src && curl -fsSL -o fonts-liberation.deb http://archive.ubuntu.com/ubuntu/pool/main/f/fonts-liberation/fonts-liberation_2.1.5-3_all.deb
  rm -rf fl && dpkg-deb -x fonts-liberation.deb fl && cp fl/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf $F/
  [ "$(sha256sum $F/LiberationSans-Regular.ttf | cut -d' ' -f1)" = "$FSHA" ] || { echo "OCR font checksum mismatch"; exit 1; }
fi
# RapidOCR model files come inside the rapidocr-onnxruntime wheel (Apache-2.0): nothing to fetch
cat > $P/env.sh <<ENV
export DEID_HOME=$P DEID_REPO=$R PYTHONDONTWRITEBYTECODE=1 DEID_TMP=$P/tmp
export DEID_PSEUDO_KEY_FILE=$P/private/pseudo.key DEID_UNRAR=$P/opt/bin/unrar DEID_7ZZ=$P/opt/bin/7zz
export DEID_DWGREAD=$P/opt/libredwg/bin/dwgread DEID_LIBARCHIVE=$P/opt/libarchive/lib DEID_PRIVATE_DATA=$P/private
export DEID_OCR_CACHE=$P/private/ocr_cache DEID_PDF_OCR=1 DEID_OCR_FRESH=0 OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1
ENV
. $P/env.sh
for x in "$DEID_UNRAR" "$DEID_7ZZ" "$DEID_DWGREAD"; do [ -x "$x" ] || { echo "MISSING $x"; exit 1; }; done
echo "setup OK under $P. Next: copy the private files (vm/PRIVATE_FILES.txt), bash vm/fetch.sh, bash vm/parity_check.sh"
