import multiprocessing
import sys

if __name__ == "__main__":
    multiprocessing.freeze_support()
    if len(sys.argv) > 1 and sys.argv[1] == "--diag":
        from inkforge.pdfconv import tesseract_path, tesseract_langs, tesseract_env, _vendor_dir
        import subprocess
        print("frozen:", getattr(sys, "frozen", False), "meipass:", getattr(sys, "_MEIPASS", None))
        print("vendor:", _vendor_dir()); print("tesseract:", tesseract_path())
        print("TESSDATA_PREFIX:", tesseract_env().get("TESSDATA_PREFIX")); print("langs:", tesseract_langs())
        r = subprocess.run([tesseract_path(), "--version"], capture_output=True, text=True, env=tesseract_env())
        print(r.returncode, (r.stdout + r.stderr)[:300])
        if len(sys.argv) > 2:   # --diag file.pdf: OCR one page and show tesseract's own messages
            import tempfile, os
            from inkforge.pdfconv import PdfConverter
            from inkforge.options import Options
            c = PdfConverter(sys.argv[2], Options())
            d = c.open()
            im = c.render(d[min(10, d.page_count - 1)], 1200)
            td = tempfile.mkdtemp()
            im.save(os.path.join(td, "p.png"))
            r = subprocess.run([tesseract_path(), os.path.join(td, "p.png"), os.path.join(td, "out"), "-l", "kan+eng",
                                "txt", "tsv"], capture_output=True, env=tesseract_env())
            print("ocr rc", r.returncode, r.stderr[-600:].decode("utf-8", "replace"), os.listdir(td))
        sys.exit(0)
    if len(sys.argv) > 1 and sys.argv[1] == "--cli":   # InkForge.app/Contents/MacOS/InkForge --cli [options] files...
        from inkforge.cli import main as cli_main
        cli_main(sys.argv[2:])
    else:
        from inkforge.ui import main
        main()
