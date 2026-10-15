"""Command line: python -m inkforge.cli [--device x3] [--pdf-mode auto] FILES_OR_FOLDERS..."""
import argparse
import sys
import time

from .options import Options, apply_preset
from .qtrender import ensure_qt
from .runner import collect, process_job, make_output_root, zip_folder


def main(argv=None):
    ap = argparse.ArgumentParser(prog="inkforge", description="Optimize books for Xteink e-ink readers (offline).")
    ap.add_argument("paths", nargs="+")
    ap.add_argument("--device", default="x4", choices=["x4", "x3"])
    ap.add_argument("--mode", default="full", choices=["quick", "full"])
    ap.add_argument("--pdf-mode", default="auto", choices=["auto", "reflow", "imgflow", "pages"])
    ap.add_argument("--kannada", default="text", choices=["text", "image"])
    ap.add_argument("--quality", type=int, default=70)
    ap.add_argument("--naming", default="author_title")
    ap.add_argument("--out", default="")
    ap.add_argument("--light-novel", action="store_true")
    ap.add_argument("--no-zip", action="store_true")
    a = ap.parse_args(argv)
    ensure_qt()
    o = apply_preset(Options(), a.mode)
    o.device, o.pdf_mode, o.kannada_mode, o.jpeg_quality = a.device, a.pdf_mode, a.kannada, a.quality
    o.naming, o.output_dir, o.light_novel, o.make_zip = a.naming, a.out, a.light_novel, not a.no_zip
    jobs = [j for j in collect(a.paths) if j.status == "queued"]
    root = make_output_root(o)
    for k, j in enumerate(jobs, 1):
        t = time.time()
        last = [0.0]

        def prog(f, label):
            if f - last[0] > 0.2:
                last[0] = f
                print(f"   {f * 100:3.0f}% {label}", file=sys.stderr)
        process_job(j, o, root, prog, log=lambda m: print("   ·", m, file=sys.stderr))
        print(f"[{k}/{len(jobs)}] {j.status.upper():6} {j.name[:70]} -> {j.before / 1e6:.1f}MB→{j.after / 1e6:.2f}MB "
              f"{time.time() - t:.0f}s | {j.message}" + (f" | ⚠ {'; '.join(j.warnings[:3])}" if j.warnings else ""))
    if o.make_zip and any(j.status == "done" for j in jobs):
        print("ZIP:", zip_folder(root))
    print("OUT:", root)


if __name__ == "__main__":
    main()
