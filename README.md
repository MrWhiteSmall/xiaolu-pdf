# 小陆工坊 · PDF 工作室

在浏览器里改 PDF：**点一段文字，原地改字**；顺带做旋转、删页、水印、页码、合并、下载。
米白纸感界面，单进程 FastAPI，按页计费的实现参考。

线上：<https://47.117.247.11/pdf/>

## 它能做什么

| 能力 | 说明 |
| --- | --- |
| 改字 | 点选页面上的文字段，改内容/字号/颜色。优先「保真」——直接改内容流里的字形与字宽；做不到时退回「擦除重写」 |
| 旋转 / 删页 | 单页 90°，删除多余页 |
| 水印 | 斜向铺满每页，支持透明度与字号 |
| 页码 | `{n} / {total}` 模板，居底 |
| 下载 | 处理完直接下载，文件保留 3 小时 |

## 两种改字模式

1. **保真（fidelity）**：调用 [`pdf-edit-engine`](https://pypi.org/project/pdf-edit-engine/)，在原内容流里替换字形、按需扩展字体子集，字体与排版基本不变。要求 PDF 的文字有可用的 ToUnicode CMap。
2. **兜底（fallback）**：先用 PyMuPDF 的 redaction 只抹掉字形（`fill=None`，不动背景），再用思源黑体（Noto Sans SC）子集重写。字体不可避免会被替换，接口会返回 `degradations: ["font_substituted"]`。

**已知边界**（实测 10 份真实 PDF：7 份可保真、3 份只能兜底）：

- PowerPoint 导出的 PDF、部分扫描件没有 ToUnicode，无法定位文字 → 走兜底
- 扫描件本身没有文字层 → 需要 OCR（本仓库不含）
- 中文用兼容部首字符（如 `⼀` U+2F00）时，先做 NFKC 归一化再匹配
- 不承诺「整段自动重排」：改长句可能溢出，接口返回 `overflow` 标记，交给用户决定

## 结构

```text
app/engine.py   # PDF 读写：定位文字、保真替换、兜底重写、页面操作
app/main.py     # FastAPI 路由与任务目录
static/         # 三栏界面：缩略图 / 画布 / 属性面板
assets/fonts/   # Noto Sans SC（不进仓库，部署时放进去）
```

接口：`POST /api/upload` · `GET /api/{id}/info` · `GET /api/{id}/page/{n}.png?dpi=110` · `GET /api/{id}/spans?page=n` · `POST /api/{id}/edit` · `rotate` / `delete` / `merge` / `watermark` / `pagenumbers` · `GET /api/{id}/download`

## 本地跑

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
# 字体：放两份 TTF 到 assets/fonts/
#   NotoSansSC-Regular.ttf / NotoSansSC-Bold.ttf（OFL，可用 fontTools 从可变字体实例化）
.venv/bin/uvicorn app.main:app --reload --port 8123
```

## 部署（nginx 前缀 /pdf/）

```nginx
location = /pdf { return 301 /pdf/; }
location ^~ /pdf/ {
  proxy_pass http://127.0.0.1:8130/;
  client_max_body_size 50m;
  proxy_read_timeout 300s;
}
```

systemd：`User=image-portal`、`Environment=PDF_DATA_DIR=/var/lib/xiaolu-pdf/jobs`、`ExecStart=/opt/xiaolu-pdf/.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8130`。

## 许可与依赖

- 本仓库代码 MIT
- `pdf-edit-engine` MIT、`PyMuPDF` **AGPL-3.0**（商用需评估，或换成 pypdf + 自研内容流解析）
- 字体：Noto Sans SC，SIL OFL 1.1
