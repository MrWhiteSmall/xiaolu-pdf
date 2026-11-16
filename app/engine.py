"""PDF 编辑内核：保真改字（pdf-edit-engine）+ 擦除兜底（PyMuPDF）+ 页面操作。"""
from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import pymupdf

try:  # 可选依赖：装了就用保真引擎
    import pdf_edit_engine as pe
except Exception:  # pragma: no cover
    pe = None

FONT_DIR = Path(os.environ.get("PDF_FONT_DIR", Path(__file__).resolve().parent.parent / "assets" / "fonts"))
FONT_SANS = FONT_DIR / "NotoSansSC-Regular.ttf"
FONT_SANS_B = FONT_DIR / "NotoSansSC-Bold.ttf"
SERIF_HINTS = ("song", "sun", "ming", "serif", "宋")
KAI_HINTS = ("kai", "楷")
HEI_HINTS = ("hei", "yahei", "hei", "黑", "ping", "gothic", "sans")


def normalise(text: str) -> str:
    return unicodedata.normalize("NFKC", text or "")


_SUBSET_DIR = Path(os.environ.get("PDF_SUBSET_DIR", "/tmp/pdf-subsets"))
_subset_cache: dict[tuple[str, str], Path] = {}


def font_name(fp: Path | None) -> str:
    """每个子集字体用独立资源名，避免 MuPDF 按 fontname 复用到别的子集。"""
    if not fp:
        return "helv"
    return "zh" + hashlib.md5(str(fp).encode()).hexdigest()[:6]


def subset_font(text: str, base: Path | None = None) -> Path | None:
    """只为本次用到的字生成子集字体，避免把 16MB 全字库塞进 PDF。"""
    base = base or FONT_SANS
    if not base.exists():
        return None
    key = ("".join(sorted(set(text))), base.name)
    if key in _subset_cache and _subset_cache[key].exists():
        return _subset_cache[key]
    try:
        from fontTools import subset as ftsubset
        _SUBSET_DIR.mkdir(parents=True, exist_ok=True)
        out = _SUBSET_DIR / f"{abs(hash(key)):x}-{base.stem}.otf"
        opts = ftsubset.Options()
        opts.layout_features = ["*"]
        opts.notdef_outline = True
        opts.recalc_bounds = True
        font = ftsubset.load_font(str(base), opts)
        sub = ftsubset.Subsetter(options=opts)
        sub.populate(text=text)
        sub.subset(font)
        font.save(str(out))
        font.close()
        _subset_cache[key] = out
        return out
    except Exception:
        return base


def _font_for(original: str) -> Path:
    """按原字体名挑一个可用的中文字体（兜底模式用）。"""
    low = (original or "").lower()
    # 目前只随包内置 Sans；宋/楷也先落到 Sans，页面上会提示“字体已替换”
    return FONT_SANS_B if "bold" in low or "heavy" in low else FONT_SANS


@dataclass
class Span:
    id: str
    text: str
    bbox: tuple[float, float, float, float]
    origin: tuple[float, float]
    font: str
    size: float
    color: tuple[float, float, float]

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "text": normalise(self.text),
            "raw": self.text,
            "bbox": [round(v, 2) for v in self.bbox],
            "origin": [round(v, 2) for v in self.origin],
            "font": self.font,
            "size": round(self.size, 2),
            "color": [round(c, 3) for c in self.color],
        }


def page_count(path: str | Path) -> int:
    with pymupdf.open(path) as doc:
        return doc.page_count


def page_size(path: str | Path, index: int) -> tuple[float, float]:
    with pymupdf.open(path) as doc:
        r = doc[index].rect
        return r.width, r.height


def spans(path: str | Path, index: int) -> list[Span]:
    out: list[Span] = []
    with pymupdf.open(path) as doc:
        page = doc[index]
        d = page.get_text("dict")
        n = 0
        for block in d["blocks"]:
            for line in block.get("lines", []):
                for s in line["spans"]:
                    t = s["text"]
                    if not t.strip():
                        continue
                    n += 1
                    colour = s.get("color", 0)
                    rgb = ((colour >> 16) & 255) / 255, ((colour >> 8) & 255) / 255, (colour & 255) / 255
                    out.append(Span(
                        id=f"s{n}",
                        text=t,
                        bbox=tuple(s["bbox"]),
                        origin=tuple(s.get("origin", (s["bbox"][0], s["bbox"][3]))),
                        font=s.get("font", ""),
                        size=float(s.get("size", 10)),
                        color=rgb,
                    ))
    return out


def render_page(path: str | Path, index: int, dpi: int = 110) -> bytes:
    with pymupdf.open(path) as doc:
        pix = doc[index].get_pixmap(dpi=dpi)
        return pix.tobytes("png")


def _replace_fidelity(src: Path, page_index: int, old: str, new: str, dst: Path) -> dict | None:
    """原地改字。返回 None 表示这个文件/这段文字引擎办不了。"""
    if pe is None:
        return None
    try:
        matches = [m for m in pe.find(str(src), old) if m.page_number == page_index]
    except Exception:
        return None
    if not matches:
        return None
    try:
        res = pe.replace(str(src), matches[0], new, output_path=str(dst), reflow=True)
    except Exception as exc:  # 引擎在不可寻址字体上会抛错
        return {"ok": False, "mode": "fidelity", "reason": f"{type(exc).__name__}: {exc}"[:200]}
    rep = res.fidelity_report
    kinds = [getattr(d.kind, "name", str(d.kind)) for d in rep.degradations]
    ok = bool(res.success)
    if not ok:
        return {"ok": False, "mode": "fidelity", "reason": "引擎放弃：" + ",".join(kinds)[:160]}
    return {
        "ok": True,
        "mode": "fidelity",
        "font": None if rep.font_preserved else rep.font_substituted,
        "overflow": rep.overflow_detected,
        "reflow": rep.reflow_applied,
        "degradations": kinds,
    }


def _replace_fallback(src: Path, page_index: int, span_bbox: Iterable[float], origin: tuple[float, float],
                      new: str, size: float, colour: tuple[float, float, float], font_name: str, dst: Path) -> dict:
    """擦除原字形（不填底色）后按原基线重写。"""
    with pymupdf.open(src) as doc:
        page = doc[page_index]
        rect = pymupdf.Rect(*span_bbox)
        page.add_redact_annot(rect, fill=None)
        page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_NONE)
        fpath = subset_font(new, _font_for(font_name))
        kwargs = {"fontsize": size, "color": colour}
        if fpath is not None:
            kwargs.update(fontfile=str(fpath), fontname="zh")
        page.insert_text(origin, new, **kwargs)
        doc.save(dst, garbage=3, deflate=True)
    return {"ok": True, "mode": "fallback", "font": fpath.stem, "overflow": False,
            "reflow": False, "degradations": ["font_substituted"]}


def edit_text(src: Path, dst: Path, page_index: int, old: str, new: str, span_bbox, origin,
              size: float, colour, font_name: str) -> dict:
    """先试保真引擎，失败或不可寻址则走擦除重写。"""
    old_n, new_n = normalise(old), normalise(new)
    if old_n == new_n:
        shutil.copyfile(src, dst)
        return {"ok": True, "mode": "noop", "degradations": []}
    # 先按原文匹配（PDF 里往往是全角标点），再退回 NFKC 归一化后的写法
    fid = None
    for o, n in ((old, new), (old_n, new_n)):
        if o == n:
            continue
        fid = _replace_fidelity(src, page_index, o, n, dst)
        if fid and fid.get("ok"):
            return fid
    fb = _replace_fallback(src, page_index, span_bbox, origin, new_n, size, colour, font_name, dst)
    if fid and not fid.get("ok"):
        fb["reason"] = fid.get("reason")
    return fb


# ---------------- 页面操作 ----------------

def _save(doc, path: Path, incremental: bool = False) -> None:
    """统一出口：子集化新嵌入的字体，避免整份字体（16MB）被塞进 PDF。"""
    if incremental:
        doc.saveIncr()
        return
    tmp = path.with_suffix(".tmp.pdf")
    doc.save(tmp, garbage=3, deflate=True)
    tmp.replace(path)


def rotate(path: Path, index: int, degrees: int) -> None:
    with pymupdf.open(path) as doc:
        page = doc[index]
        page.set_rotation((page.rotation + degrees) % 360)
        _save(doc, path)


def delete_pages(path: Path, indices: list[int]) -> int:
    with pymupdf.open(path) as doc:
        for i in sorted(set(indices), reverse=True):
            if 0 <= i < doc.page_count and doc.page_count > 1:
                doc.delete_page(i)
        _save(doc, path)
        return doc.page_count


def move_page(path: Path, old: int, new: int) -> None:
    with pymupdf.open(path) as doc:
        doc.move_page(old, new)
        _save(doc, path)


def merge(paths_and_ranges: list[tuple[Path, list[int] | None]], dst: Path) -> None:
    out = pymupdf.open()
    for p, pages in paths_and_ranges:
        with pymupdf.open(p) as src:
            if pages is None:
                out.insert_pdf(src)
            else:
                out.insert_pdf(src, from_page=min(pages), to_page=max(pages))
    out.save(dst, garbage=3, deflate=True)


def watermark(path: Path, text: str, size: int = 42, opacity: float = 0.18, angle: int = 45) -> None:
    fp = subset_font(text)
    with pymupdf.open(path) as doc:
        for page in doc:
            r = page.rect
            fs = min(size, r.width / max(4, len(text)) * 1.4)
            pivot = pymupdf.Point(r.width / 2, r.height / 2)
            mat = pymupdf.Matrix(1, 0, 0, 1, 0, 0).prerotate(angle)
            page.insert_text((pivot.x - len(text) * fs * 0.30, pivot.y), text, fontsize=fs,
                             fontfile=str(fp) if fp else None,
                             fontname=font_name(fp),
                             color=(0.45, 0.45, 0.45), fill_opacity=opacity, overlay=True,
                             morph=(pivot, mat))
        _save(doc, path)


def page_numbers(path: Path, start: int = 1, template: str = "{n} / {total}", size: int = 10) -> None:
    with pymupdf.open(path) as doc:
        total = doc.page_count
        labels = [template.format(n=start + i, total=total) for i in range(total)]
        fp = subset_font("".join(labels))
        for i, page in enumerate(doc):
            label = labels[i]
            r = page.rect
            page.insert_text((r.width / 2 - len(label) * size * 0.28, r.height - 26), label,
                             fontsize=size, fontfile=str(fp) if fp else None,
                             fontname=font_name(fp), color=(0.25, 0.25, 0.25))
        _save(doc, path)
