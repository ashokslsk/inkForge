# PyInstaller spec: builds dist/InkForge.app
block_cipher = None
a = Analysis(["app_main.py"], pathex=["."], datas=[("inkforge/assets/icon.png", "inkforge/assets"), ("vendor/tessdata", "vendor/tessdata"), ("vendor/tesseract", "vendor/tesseract")],
             hiddenimports=["PySide6.QtSvg", "mobi", "lxml.html", "lxml.etree", "pymupdf"],
             excludes=["tkinter", "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.Qt3DCore",
                       "PySide6.QtQuick", "PySide6.QtQml", "PySide6.QtMultimedia", "PySide6.QtCharts", "PySide6.QtPdf",
                       "matplotlib", "scipy", "pandas", "IPython"],
             noarchive=False)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="InkForge", console=False, argv_emulation=False,
          target_arch=None, icon="build_tmp/InkForge.icns")
coll = COLLECT(exe, a.binaries, a.zipfiles, a.datas, strip=False, upx=False, name="InkForge")
doc = lambda name, exts, uti: {"CFBundleTypeName": name, "CFBundleTypeRole": "Editor", "LSHandlerRank": "Alternate",
                               "CFBundleTypeExtensions": exts, "LSItemContentTypes": uti}
app = BUNDLE(coll, name="InkForge.app", icon="build_tmp/InkForge.icns", bundle_identifier="local.ashok.inkforge",
             info_plist={"CFBundleShortVersionString": "1.0.0", "NSHighResolutionCapable": True,
                         "LSMinimumSystemVersion": "12.0", "NSRequiresAquaSystemAppearance": True,
                         "CFBundleDocumentTypes": [
                             doc("EPUB book", ["epub"], ["org.idpf.epub-container"]),
                             doc("PDF document", ["pdf"], ["com.adobe.pdf"]),
                             doc("Kindle book", ["mobi", "azw", "azw3", "prc"], []),
                             doc("Text", ["txt"], ["public.plain-text"]),
                             doc("Folder", [], ["public.folder"])]})
