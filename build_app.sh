#!/bin/zsh
# Rebuild dist/InkForge.app (needs internet only the first time, to create .venv)
set -e
cd "$(dirname "$0")"
[ -d .venv ] || { /opt/homebrew/bin/python3.12 -m venv .venv && .venv/bin/pip install PySide6-Essentials pymupdf pillow lxml mobi numpy pyinstaller; }
[ -x vendor/tesseract/bin/tesseract ] || .venv/bin/python vendor_tesseract.py   # bundled OCR engine
[ -f vendor/tessdata/kan.traineddata ] || { echo "vendor/tessdata/kan.traineddata missing"; exit 1; }
.venv/bin/pyinstaller --noconfirm --log-level WARN InkForge.spec
rm -rf build_tmp/dmg && mkdir -p build_tmp/dmg && cp -R dist/InkForge.app build_tmp/dmg/ && ln -s /Applications build_tmp/dmg/Applications && cp README.md "build_tmp/dmg/Read Me.md"
hdiutil create -volname InkForge -srcfolder build_tmp/dmg -ov -format UDZO -imagekey zlib-level=9 dist/InkForge-1.0.0.dmg
echo "Built: $(pwd)/dist/InkForge.app and dist/InkForge-1.0.0.dmg"
