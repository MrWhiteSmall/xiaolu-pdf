"""小陆工坊 · PDF 编辑器 —— 后端服务。"""
from __future__ import annotations

import json
import os
import shutil
import time
import uuid
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import auth, engine

ROOT = Path(__file__).resolve().parent.parent
DATA = Path(os.environ.get("PDF_DATA_DIR", ROOT / "data"))
JOBS = DATA / "jobs"
JOBS.mkdir(parents=True, exist_ok=True)
STATIC = ROOT / "static"

MAX_BYTES = 40 * 1024 * 1024
MAX_PAGES = 300
TTL_SECONDS = 3 * 3600

app = FastAPI(title="小陆工坊 · PDF 编辑器", version="0.1.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


@app.middleware("http")
async def gate(request: Request, call_next):
    """所有 /api 都要求工坊签发的专属链接；静态页与健康检查放行。"""
    if request.url.path.startswith("/api/"):
        try:
            request.state.grant = auth.verify(auth.token_of(request))
        except HTTPException as exc:
            return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
    return await call_next(request)


# ---------------- 基本工具 ----------------

def job_dir(jid: str) -> Path:
    d = JOBS / jid
    if not d.is_dir():
        raise HTTPException(404, "文件不存在或已过期")
    return d


def meta_path(d: Path) -> Path:
    return d / "meta.json"


def read_meta(d: Path) -> dict:
    return json.loads(meta_path(d).read_text("utf-8"))


def write_meta(d: Path, meta: dict) -> None:
    meta_path(d).write_text(json.dumps(meta, ensure_ascii=False, indent=1), "utf-8")


def current(d: Path) -> Path:
    return d / "current.pdf"


def cleanup() -> None:
    now = time.time()
    for d in JOBS.iterdir():
        try:
            if d.is_dir() and now - d.stat().st_mtime > TTL_SECONDS:
                shutil.rmtree(d, ignore_errors=True)
        except OSError:
            pass


# ---------------- 接口 ----------------

@app.get("/manifest")
def manifest():
    """Platform registry probe: tells /admin this service is ready and has no model choice."""
    return {"id": "pdf", "title": "PDF 编辑器", "unit": "页", "page": "pdf.html",
            "available": True, "models": [], "default_model": None, "pending": {}}


@app.get("/health")
def health() -> dict:
    return {"ok": True, "service": "pdf", "fidelity_engine": engine.pe is not None}


@app.post("/api/upload")
async def upload(request: Request, file: UploadFile = File(...)) -> dict:
    cleanup()
    raw = await file.read()
    if len(raw) > MAX_BYTES:
        raise HTTPException(413, "文件超过 40 MB，请拆分后再传")
    if not raw[:5].startswith(b"%PDF"):
        raise HTTPException(400, "这不是一个 PDF 文件")
    jid = uuid.uuid4().hex[:12]
    d = JOBS / jid
    d.mkdir(parents=True)
    src = d / "original.pdf"
    src.write_bytes(raw)
    try:
        pages = engine.page_count(src)
    except Exception:
        shutil.rmtree(d, ignore_errors=True)
        raise HTTPException(400, "PDF 解析失败，可能已加密或损坏")
    if pages > MAX_PAGES:
        shutil.rmtree(d, ignore_errors=True)
        raise HTTPException(400, f"超过 {MAX_PAGES} 页，请拆分后再传")
    grant = getattr(request.state, "grant", None)
    try:
        left = auth.charge(grant["id"], pages)
    except HTTPException:
        shutil.rmtree(d, ignore_errors=True)
        raise
    shutil.copyfile(src, current(d))
    write_meta(d, {"name": file.filename or "document.pdf", "pages": pages,
                   "created": int(time.time()), "edits": 0, "grant": grant["id"]})
    return {"id": jid, "pages": pages, "name": file.filename or "document.pdf",
            "remaining": left}


@app.get("/api/me")
def me(request: Request):
    return {"ok": True, **auth.profile(request.state.grant)}


@app.get("/api/{jid}/info")
def info(jid: str) -> dict:
    d = job_dir(jid)
    m = read_meta(d)
    w, h = engine.page_size(current(d), 0)
    return {**m, "id": jid, "page_size": [round(w, 1), round(h, 1)],
            "watermark": bool(m.get("watermark"))}


@app.get("/api/{jid}/page/{index}.png")
def page_png(jid: str, index: int, dpi: int = 110) -> Response:
    d = job_dir(jid)
    pages = engine.page_count(current(d))
    if not 0 <= index < pages:
        raise HTTPException(404, "页码超出范围")
    dpi = max(72, min(200, dpi))
    return Response(engine.render_page(current(d), index, dpi), media_type="image/png",
                    headers={"Cache-Control": "no-store"})


@app.get("/api/{jid}/spans")
def get_spans(jid: str, page: int = 0) -> dict:
    d = job_dir(jid)
    pages = engine.page_count(current(d))
    if not 0 <= page < pages:
        raise HTTPException(404, "页码超出范围")
    w, h = engine.page_size(current(d), page)
    return {"page": page, "pages": pages, "size": [w, h],
            "spans": [s.as_dict() for s in engine.spans(current(d), page)]}


class EditIn(BaseModel):
    page: int
    old: str
    new: str
    bbox: list[float]
    origin: list[float]
    size: float = 12
    color: list[float] = Field(default_factory=lambda: [0.08, 0.07, 0.06])
    font: str = ""
    restyle: bool = False
    bold: bool = False


@app.post("/api/{jid}/edit")
def edit(jid: str, body: EditIn) -> dict:
    d = job_dir(jid)
    if not body.new.strip():
        raise HTTPException(400, "新内容不能为空（删除请用页面操作）")
    if len(body.new) > 400:
        raise HTTPException(400, "一次最多改 400 字，长段落请分次处理")
    src, dst = current(d), d / "next.pdf"
    size = min(400.0, max(1.0, float(body.size or 12)))
    result = engine.edit_text(src, dst, body.page, body.old, body.new,
                              body.bbox, tuple(body.origin), size,
                              tuple(body.color[:3]), body.font, body.restyle, body.bold)
    if not result.get("ok"):
        dst.unlink(missing_ok=True)
        return {"ok": False, "reason": result.get("reason", "引擎无法处理这段文字")}
    dst.replace(src)
    m = read_meta(d); m["edits"] = m.get("edits", 0) + 1; m["pages"] = engine.page_count(src)
    write_meta(d, m)
    return result


class RotateIn(BaseModel):
    page: int
    degrees: int = 90


@app.post("/api/{jid}/rotate")
def rotate(jid: str, body: RotateIn) -> dict:
    d = job_dir(jid)
    engine.rotate(current(d), body.page, 90 if body.degrees >= 0 else -90)
    return {"ok": True}


class DeleteIn(BaseModel):
    pages: list[int]


@app.post("/api/{jid}/delete")
def delete(jid: str, body: DeleteIn) -> dict:
    d = job_dir(jid)
    if not body.pages:
        raise HTTPException(400, "没有指定要删的页")
    left = engine.delete_pages(current(d), body.pages)
    m = read_meta(d); m["pages"] = left; write_meta(d, m)
    return {"ok": True, "pages": left}


class MoveIn(BaseModel):
    old: int
    new: int


@app.post("/api/{jid}/move")
def move(jid: str, body: MoveIn) -> dict:
    d = job_dir(jid)
    engine.move_page(current(d), body.old, body.new)
    return {"ok": True}


class WatermarkIn(BaseModel):
    text: str
    size: int = 42
    opacity: float = 0.16
    angle: int = 45


@app.post("/api/{jid}/watermark")
def watermark(jid: str, body: WatermarkIn) -> dict:
    d = job_dir(jid)
    if not body.text.strip():
        raise HTTPException(400, "水印文字不能为空")
    backup = d / "before-watermark.pdf"
    if not backup.exists():
        shutil.copyfile(current(d), backup)
    engine.watermark(current(d), body.text.strip(), body.size, max(0.05, min(0.6, body.opacity)), body.angle)
    m = read_meta(d); m["watermark"] = True; write_meta(d, m)
    return {"ok": True, "watermark": True}


@app.post("/api/{jid}/unwatermark")
def unwatermark(jid: str) -> dict:
    d = job_dir(jid)
    backup = d / "before-watermark.pdf"
    if not backup.exists():
        raise HTTPException(409, "这份文件没有可撤销的水印记录")
    shutil.copyfile(backup, current(d))
    m = read_meta(d); m["watermark"] = False; write_meta(d, m)
    return {"ok": True, "watermark": False}


class EraseIn(BaseModel):
    page: int
    rect: list[float]


@app.post("/api/{jid}/erase")
def erase(jid: str, body: EraseIn) -> dict:
    d = job_dir(jid)
    if len(body.rect) != 4:
        raise HTTPException(400, "需要选择一块区域")
    x0, y0, x1, y1 = [float(v) for v in body.rect]
    if x1 - x0 < 1 or y1 - y0 < 1:
        raise HTTPException(400, "框选区域太小了")
    result = engine.erase_region(current(d), body.page, (x0, y0, x1, y1))
    m = read_meta(d); m["edits"] = m.get("edits", 0) + 1; write_meta(d, m)
    return result


class NumbersIn(BaseModel):
    start: int = 1
    template: str = "{n} / {total}"
    size: int = 10


@app.post("/api/{jid}/pagenumbers")
def pagenumbers(jid: str, body: NumbersIn) -> dict:
    d = job_dir(jid)
    engine.page_numbers(current(d), body.start, body.template, body.size)
    return {"ok": True}


@app.get("/api/{jid}/download")
def download(jid: str) -> FileResponse:
    d = job_dir(jid)
    name = read_meta(d).get("name", "document.pdf")
    stem = Path(name).stem or "document"
    return FileResponse(current(d), media_type="application/pdf", filename=f"{stem}-edited.pdf")


app.mount("/", StaticFiles(directory=STATIC, html=True), name="static")
