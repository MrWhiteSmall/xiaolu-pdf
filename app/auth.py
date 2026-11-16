"""专属链接校验：复用工坊的 grants 表与 HMAC 签名，不引入第二套账号体系。"""
from __future__ import annotations

import hmac
import json
import os
import sqlite3
from pathlib import Path

from fastapi import HTTPException, Request

DB_PATH = Path(os.environ.get("STUDIO_DB", "/var/lib/image-studio-portal/portal.sqlite3"))
SERVICE = "pdf"


def _secret() -> dict:
    raw = os.environ.get("STUDIO_PORTAL_SECRET", "")
    if raw:
        return json.loads(raw)
    cred = Path(os.environ.get("CREDENTIALS_DIRECTORY", "")) / "STUDIO_PORTAL_SECRET"
    if cred.is_file():
        return json.loads(cred.read_text("utf-8"))
    raise HTTPException(503, "服务端缺少签名凭据，请联系管理员")


def _connect() -> sqlite3.Connection:
    if not DB_PATH.is_file():
        raise HTTPException(503, "工作站数据库不可用，请联系管理员")
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def token_of(request: Request) -> str:
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    if request.headers.get("x-token"):
        return request.headers["x-token"].strip()
    return (request.query_params.get("token") or "").strip()


def verify(token: str) -> dict:
    if not token or "." not in token:
        raise HTTPException(401, "请用专属链接打开，链接可以在工坊里生成")
    gid = token.split(".", 1)[0]
    want = gid + "." + hmac.new(_secret()["signing_key"].encode(), gid.encode(), "sha256").hexdigest()
    if not hmac.compare_digest(token, want):
        raise HTTPException(401, "链接无效，请核对后重试")
    with _connect() as c:
        row = c.execute("SELECT * FROM grants WHERE id=?", (gid,)).fetchone()
    if not row:
        raise HTTPException(404, "链接不存在")
    g = dict(row)
    if not g["active"]:
        raise HTTPException(403, "这条链接已暂停，请联系管理员")
    if g["service"] != SERVICE:
        raise HTTPException(403, "这条链接不是 PDF 编辑器的额度")
    g["models"] = json.loads(g["models"] or "[]")
    return g


def charge(grant_id: str, pages: int) -> int:
    """按上传页数扣额度，返回扣完后的剩余页数。"""
    with sqlite3.connect(DB_PATH, timeout=15) as c:
        cur = c.execute("UPDATE grants SET remaining=remaining-? WHERE id=? AND remaining>=?",
                        (pages, grant_id, pages))
        if cur.rowcount != 1:
            row = c.execute("SELECT remaining FROM grants WHERE id=?", (grant_id,)).fetchone()
            left = row[0] if row else 0
            raise HTTPException(402, f"本次需要 {pages} 页，这条链接还剩 {left} 页，请联系管理员续充")
        c.execute("INSERT INTO audit(grant_id,action,amount,created) VALUES(?,?,?,strftime('%s','now'))",
                  (grant_id, "pdf_upload", pages))
        row = c.execute("SELECT remaining FROM grants WHERE id=?", (grant_id,)).fetchone()
    return int(row[0])


def profile(grant):
    """Remaining/total for the header badge."""
    with _connect() as c:
        row = c.execute("SELECT remaining,total,used,label FROM grants WHERE id=?", (grant["id"],)).fetchone()
    if not row:
        return {"remaining": 0, "total": 0, "used": 0, "label": ""}
    return {"remaining": row["remaining"], "total": row["total"], "used": row["used"], "label": row["label"]}
