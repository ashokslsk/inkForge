"""Noise metrics for a converted EPUB: python tests/noise_report.py book.epub"""
import re, sys, zipfile
sys.path.insert(0, __file__.rsplit("/tests/", 1)[0])
from inkforge.textclean import kannada_quality

z = zipfile.ZipFile(sys.argv[1])
xs = sorted(n for n in z.namelist() if n.endswith(".xhtml") and "/Text/" in n)
paras = []
for x in xs:
    h = re.sub(r"<head>.*</head>", "", z.read(x).decode(), flags=re.S)
    paras += [re.sub(r"<[^>]+>", " ", m).strip() for m in re.findall(r"<(?:p|h\d)[^>]*>(.*?)</(?:p|h\d)>", h, re.S)]
paras = [re.sub(r"\s+", " ", p) for p in paras if p.strip()]
text = " ".join(paras)
rep = {
    "paragraphs": len(paras),
    "running heads left": sum(1 for p in paras if re.fullmatch(r"(?i)(the constitution of india|ಭಾರತದ ಸಂವಿಧಾನ)\s*[\d೦-೯]*", p)),
    "number-only paras": sum(1 for p in paras if re.fullmatch(r"[\d೦-೯ivxlcIVXLC\-–,.\s\[\]]+", p)),
    "dot leaders": len(re.findall(r"(?:\.\s?){5,}", text)),
    "garbled-English-as-Kannada": len(re.findall(r"[೦-೯][0-9][೦-೯0-9]{2,}", text)),
    "1-2 word fragments": sum(1 for p in paras if len(p.split()) <= 2 and not re.match(r"(?i)(part|chapter|ಭಾಗ|ಅಧ್ಯಾಯ)", p)),
    "Kannada quality": round(kannada_quality(text), 4),
}
for k, v in rep.items():
    print(f"{k:28} {v}")
