"""Copy Homebrew's tesseract + all non-system dylibs into vendor/tesseract with relocatable load paths."""
import os, re, shutil, subprocess, sys

SRC_BIN = os.path.realpath("/opt/homebrew/bin/tesseract")
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor", "tesseract")
BIN, LIB = os.path.join(OUT, "bin"), os.path.join(OUT, "lib")


def deps(path):
    out = subprocess.run(["otool", "-L", path], capture_output=True, text=True).stdout.splitlines()[1:]
    res = []
    for l in out:
        p = l.strip().split(" (")[0]
        if p.startswith(("/usr/lib", "/System")):
            continue
        res.append(p)
    return res


def resolve(ref, origin):
    if ref.startswith("@rpath/") or ref.startswith("@loader_path/"):
        name = ref.split("/", 1)[1]
        for d in (os.path.dirname(origin), "/opt/homebrew/lib"):
            cand = os.path.join(d, name)
            if os.path.exists(cand):
                return os.path.realpath(cand)
        raise SystemExit(f"cannot resolve {ref} from {origin}")
    return os.path.realpath(ref)


shutil.rmtree(OUT, ignore_errors=True)
os.makedirs(BIN); os.makedirs(LIB)
todo, seen = [SRC_BIN], {}
while todo:
    p = todo.pop()
    for ref in deps(p):
        real = resolve(ref, p)
        if real not in seen:
            seen[real] = os.path.basename(ref)
            todo.append(real)
shutil.copy2(SRC_BIN, os.path.join(BIN, "tesseract"))
for real, name in seen.items():
    shutil.copy2(real, os.path.join(LIB, name))

files = [os.path.join(BIN, "tesseract")] + [os.path.join(LIB, n) for n in seen.values()]
for f in files:
    os.chmod(f, 0o755)
    is_bin = f.endswith("/tesseract")
    if not is_bin:
        subprocess.run(["install_name_tool", "-id", "@loader_path/" + os.path.basename(f), f], check=True, capture_output=True)
    for ref in deps(f):
        name = os.path.basename(ref)
        new = ("@executable_path/../lib/" if is_bin else "@loader_path/") + name
        subprocess.run(["install_name_tool", "-change", ref, new, f], check=True, capture_output=True)
    # drop absolute rpaths
    for rp in re.findall(r"path (/opt/homebrew\S*)", subprocess.run(["otool", "-l", f], capture_output=True, text=True).stdout):
        subprocess.run(["install_name_tool", "-delete_rpath", rp, f], capture_output=True)
for f in files:
    subprocess.run(["codesign", "--force", "-s", "-", f], check=True, capture_output=True)
print(f"bundled tesseract with {len(seen)} libraries:", ", ".join(sorted(seen.values())))
