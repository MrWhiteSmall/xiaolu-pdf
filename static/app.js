const $ = (s) => document.querySelector(s);
const DEBUG = new URLSearchParams(location.search).get("debug") === "1";
if (DEBUG) document.body.classList.add("debug");
const BASE = location.pathname.replace(/\/+$/, "") || "";
const TOKEN = (() => {
  const h = new URLSearchParams(location.hash.replace(/^#/, ""));
  return h.get("token") || new URLSearchParams(location.search).get("token") || "";
})();
const authQ = TOKEN ? "token=" + encodeURIComponent(TOKEN) : "";
const authHeaders = TOKEN ? { authorization: "Bearer " + TOKEN } : {};
if (!TOKEN) {
  document.querySelector("#empty").hidden = true;
  document.querySelector("#gate").hidden = false;
}
const state = { id: null, page: 0, pages: 0, spans: [], selected: null, scale: 1, dpi: Math.round(110 * Math.min(2, window.devicePixelRatio || 1)) };

const toast = (msg, err) => {
  const t = $("#toast");
  t.textContent = msg;
  t.className = "toast" + (err ? " err" : "");
  t.hidden = false;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => (t.hidden = true), 3200);
};

async function api(path, opts = {}) {
  opts.headers = { ...authHeaders, ...(opts.headers || {}) };
  const res = await fetch(path, opts);
  const type = res.headers.get("content-type") || "";
  if (!res.ok) {
    const body = type.includes("json") ? await res.json() : { detail: res.statusText };
    throw new Error(body.detail || "请求失败");
  }
  return type.includes("json") ? res.json() : res;
}

// ---------- 上传 ----------
$("#file").addEventListener("change", (e) => e.target.files[0] && upload(e.target.files[0]));
$("#drop").addEventListener("dragover", (e) => { e.preventDefault(); $("#drop").style.background = "#faf5ea"; });
$("#drop").addEventListener("dragleave", () => { $("#drop").style.background = ""; });
$("#drop").addEventListener("drop", (e) => {
  e.preventDefault(); $("#drop").style.background = "";
  const f = e.dataTransfer.files[0]; if (f) upload(f);
});

async function upload(file) {
  if (!file.name.toLowerCase().endsWith(".pdf")) return toast("请选择 PDF 文件", true);
  toast("上传中…");
  const fd = new FormData(); fd.append("file", file);
  try {
    const info = await api(`${BASE}/api/upload`, { method: "POST", body: fd });
    state.id = info.id; state.pages = info.pages; state.page = 0;
    state.name = info.name;
    $("#empty").hidden = true; $("#workspace").hidden = false;
    $("#btn-download").disabled = false; $("#btn-reset").hidden = false;
    $("#status").textContent = `${info.pages} 页 · ${info.name}` + (info.remaining != null ? ` · 还剩 ${info.remaining} 页` : "");
    $("#unwatermark").hidden = true;
    toast("已上传，点页面上任一段文字开始改");
    await loadPage(0);
    buildThumbs();
  } catch (err) { toast(err.message, true); }
}

async function openJob(id) {
  const info = await api(`${BASE}/api/${id}/info`);
  state.id = info.id; state.pages = info.pages; state.page = info.page || 0; state.name = info.name;
  $("#empty").hidden = true; $("#workspace").hidden = false;
  $("#btn-download").disabled = false; $("#btn-reset").hidden = false;
  $("#status").textContent = `${info.pages} 页 · ${info.name}`;
  $("#unwatermark").hidden = !info.watermark;
  await loadPage(state.page); buildThumbs();
}

async function buildThumbs() {
  const box = $("#thumbs"); box.innerHTML = "";
  $("#page-count").textContent = state.pages;
  for (let i = 0; i < state.pages; i++) {
    const b = document.createElement("button");
    b.className = "thumb" + (i === state.page ? " on" : "");
    b.innerHTML = `<img loading="lazy" src="${BASE}/api/${state.id}/page/${i}.png?dpi=60&${authQ}" alt="第 ${i + 1} 页"><span>${i + 1}</span>`;
    b.onclick = () => loadPage(i);
    box.appendChild(b);
  }
}

// ---------- 页面渲染 ----------
async function loadPage(n) {
  if (!state.id) return;
  state.page = n; state.selected = null; clearSelection();
  const img = $("#pageimg");
  img.src = `${BASE}/api/${state.id}/page/${n}.png?dpi=${state.dpi}&${authQ}&t=${Date.now()}`;
  $("#pageno").textContent = `${n + 1} / ${state.pages}`;
  await new Promise((r) => (img.onload = r));
  const data = await api(`${BASE}/api/${state.id}/spans?page=${n}`);
  state.spans = data.spans;
  state.pw = data.size[0]; state.scale = img.clientWidth / data.size[0];
  $("#sheet").style.width = img.clientWidth + "px";
  $("#sheet").style.height = img.clientHeight + "px";
  drawSpans();
  document.querySelectorAll(".thumb").forEach((t, i) => t.classList.toggle("on", i === n));
}

function drawSpans() {
  const box = $("#overlay");
  box.innerHTML = "";
  const s = state.scale;
  for (const sp of state.spans) {
    const [x0, y0, x1, y1] = sp.bbox;
    const el = document.createElement("div");
    el.className = "span";
    el.style.left = x0 * s + "px";
    el.style.top = y0 * s + "px";
    el.style.width = Math.max(4, (x1 - x0) * s) + "px";
    el.style.height = Math.max(6, (y1 - y0) * s) + "px";
    el.title = sp.text;
    el.onclick = (e) => { e.stopPropagation(); select(sp, el); };
    box.appendChild(el);
  }
}

function select(sp, el) {
  document.querySelectorAll(".span.on").forEach((n) => n.classList.remove("on"));
  el.classList.add("on");
  state.selected = sp;
  $("#hint").hidden = true;
  $("#editform").hidden = false;
  $("#clear").hidden = false;
  $("#old").value = sp.raw ?? sp.text;
  $("#new").value = sp.raw ?? sp.text;
  $("#size").value = Number(sp.size.toFixed(2));
  $("#color").value = rgb2hex(sp.color);
  $("#bold").checked = !!sp.bold;
  $("#meta").textContent = `${sp.font || "未知字体"}${sp.bold ? " · 粗体" : ""} · ${sp.size}pt · 第 ${state.page + 1} 页`;
  $("#new").focus();
  $("#new").select();
}

function clearSelection() {
  state.selected = null;
  document.querySelectorAll(".span.on").forEach((n) => n.classList.remove("on"));
  $("#editform").hidden = true;
  $("#hint").hidden = false;
  $("#clear").hidden = true;
}
$("#clear").onclick = clearSelection;

const rgb2hex = (c) => "#" + c.map((v) => Math.round(Math.min(1, Math.max(0, v)) * 255).toString(16).padStart(2, "0")).join("");
const hex2rgb = (h) => [1, 3, 5].map((i) => parseInt(h.slice(i, i + 2), 16) / 255);

$("#editform").onsubmit = async (e) => {
  e.preventDefault();
  const sp = state.selected; if (!sp) return;
  const body = {
    page: state.page, old: sp.raw ?? sp.text, new: $("#new").value,
    bbox: sp.bbox, origin: sp.origin, size: Number($("#size").value) || sp.size,
    color: hex2rgb($("#color").value), font: sp.font, bold: $("#bold").checked,
  };
  if (!body.new.trim()) return toast("新内容不能为空", true);
  const sameText = body.new === (sp.raw ?? sp.text);
  const sizeMoved = Math.abs(body.size - sp.size) > 0.05;
  const colorMoved = body.color.some((v, i) => Math.abs(v - (sp.color[i] ?? v)) > 0.012);
  body.restyle = sameText && (sizeMoved || colorMoved);
  if (!sameText && (sizeMoved || colorMoved)) body.restyle = true;
  $("#apply").disabled = true;
  try {
    const r = await api(`${BASE}/api/${state.id}/edit`, {
      method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(body),
    });
    if (!r.ok) { toast(r.reason || "这段改不了", true); return; }
    const label = r.mode === "fidelity" ? "已原地修改，字体未变" :
      (r.mode === "noop" ? "内容没变" :
       (r.restyled ? "已按新字号/颜色重写（字体已替换）" : "已擦除重写（字体已替换）"));
    const extra = (r.degradations || []).filter((k) => k !== "font_substituted").slice(0, 2).join("、");
    toast(label + (extra ? ` · ${extra}` : "") + (r.overflow ? " · 文字比原来长，注意别压到右边" : ""));
    await loadPage(state.page);
    refreshThumb(state.page);
  } catch (err) { toast(err.message, true); }
  finally { $("#apply").disabled = false; }
};

function refreshThumb(i) {
  const t = document.querySelectorAll(".thumb")[i];
  if (t) t.querySelector("img").src = `${BASE}/api/${state.id}/page/${i}.png?dpi=60&${authQ}&t=${Date.now()}`;
}

// ---------- 页面操作 ----------
$("#prev").onclick = () => state.page > 0 && loadPage(state.page - 1);
$("#next").onclick = () => state.page < state.pages - 1 && loadPage(state.page + 1);

$("#rotate").onclick = async () => {
  await api(`${BASE}/api/${state.id}/rotate`, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ page: state.page, degrees: 90 }) });
  await loadPage(state.page); refreshThumb(state.page); toast("已旋转");
};

$("#delete").onclick = async () => {
  if (state.pages <= 1) return toast("只剩一页，不能删", true);
  if (!confirm(`删除第 ${state.page + 1} 页？`)) return;
  const r = await api(`${BASE}/api/${state.id}/delete`, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ pages: [state.page] }) });
  state.pages = r.pages;
  state.page = Math.min(state.page, state.pages - 1);
  $("#status").textContent = `${state.pages} 页 · ${state.name}`;
  await loadPage(state.page); buildThumbs(); toast("已删除");
};

$("#watermark").onclick = async () => {
  const text = prompt("水印文字（斜着铺满每页）", "仅供办理 · 他用无效");
  if (!text) return;
  await api(`${BASE}/api/${state.id}/watermark`, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ text, size: 40, opacity: 0.16 }) });
  $("#unwatermark").hidden = false;
  await loadPage(state.page); toast("水印已加，可用「撤销水印」一键还原");
};

$("#numbers").onclick = async () => {
  const tpl = prompt("页码格式（{n} 是当前页，{total} 是总页数）", "{n} / {total}");
  if (!tpl) return;
  await api(`${BASE}/api/${state.id}/pagenumbers`, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ start: 1, template: tpl, size: 10 }) });
  await loadPage(state.page); toast("页码已加");
};

$("#btn-download").onclick = async () => {
  if (!state.id) return;
  try {
    const res = await api(`${BASE}/api/${state.id}/download`);
    const blob = await res.blob();
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = (state.name || "document").replace(/\.pdf$/i, "") + "-edited.pdf";
    a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 15000);
  } catch (err) { toast(err.message, true); }
};
$("#btn-reset").onclick = () => { state.id = null; location.reload(); };

const bootId = new URLSearchParams(location.search).get("id");
if (bootId) openJob(bootId).catch((e) => toast(e.message, true));

window.addEventListener("keydown", (e) => {
  if (e.target.matches("input,textarea")) return;
  if (e.key === "ArrowLeft") $("#prev").click();
  if (e.key === "ArrowRight") $("#next").click();
});

window.addEventListener("resize", () => {
  const img = $("#pageimg");
  if (!state.id || !state.pw || !img.clientWidth) return;
  state.scale = img.clientWidth / state.pw;
  $("#sheet").style.width = img.clientWidth + "px";
  $("#sheet").style.height = img.clientHeight + "px";
  drawSpans();
});

// ---------- 框选删除 ----------
let eraseMode = false, dragFrom = null;
const sheet = $("#sheet"), marquee = $("#marquee");

function setErase(on) {
  eraseMode = on;
  $("#erase").classList.toggle("on", on);
  sheet.classList.toggle("erase-mode", on);
  if (!on) marquee.hidden = true;
}

$("#erase").onclick = () => {
  if (!state.id) return;
  setErase(!eraseMode);
  toast(eraseMode ? "在页面上拖一个框，松手后确认删除" : "已退出框选删除");
};

sheet.addEventListener("mousedown", (e) => {
  if (!eraseMode || !state.id) return;
  e.preventDefault();
  const r = sheet.getBoundingClientRect();
  dragFrom = { x: e.clientX - r.left, y: e.clientY - r.top };
  marquee.hidden = false;
  marquee.style.left = dragFrom.x + "px";
  marquee.style.top = dragFrom.y + "px";
  marquee.style.width = "0px";
  marquee.style.height = "0px";
});

window.addEventListener("mousemove", (e) => {
  if (!dragFrom) return;
  const r = sheet.getBoundingClientRect();
  const x = Math.max(0, Math.min(r.width, e.clientX - r.left));
  const y = Math.max(0, Math.min(r.height, e.clientY - r.top));
  marquee.style.left = Math.min(x, dragFrom.x) + "px";
  marquee.style.top = Math.min(y, dragFrom.y) + "px";
  marquee.style.width = Math.abs(x - dragFrom.x) + "px";
  marquee.style.height = Math.abs(y - dragFrom.y) + "px";
});

window.addEventListener("mouseup", async (e) => {
  if (!dragFrom) return;
  const r = sheet.getBoundingClientRect();
  const x = Math.max(0, Math.min(r.width, e.clientX - r.left));
  const y = Math.max(0, Math.min(r.height, e.clientY - r.top));
  const left = Math.min(x, dragFrom.x), top = Math.min(y, dragFrom.y);
  const w = Math.abs(x - dragFrom.x), h = Math.abs(y - dragFrom.y);
  dragFrom = null; marquee.hidden = true;
  if (w < 6 || h < 6 || !state.id) return;
  const rect = [left / state.scale, top / state.scale, (left + w) / state.scale, (top + h) / state.scale].map((v) => Math.round(v * 10) / 10);
  if (!confirm("删除框选区域里的内容？（水印、印章、多余文字都可以这样去掉）")) return;
  try {
    await api(`${BASE}/api/${state.id}/erase`, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ page: state.page, rect }) });
    toast("已删除框选区域");
    await loadPage(state.page);
  } catch (err) { toast(err.message, true); }
});

// ---------- 撤销水印 ----------
$("#unwatermark").onclick = async () => {
  if (!state.id) return;
  if (!confirm("撤销水印会回到加水印之前的状态，之后的其他修改会丢失。继续？")) return;
  try {
    await api(`${BASE}/api/${state.id}/unwatermark`, { method: "POST" });
    $("#unwatermark").hidden = true;
    toast("水印已撤销，回到加水印之前");
    await loadPage(state.page);
  } catch (err) { toast(err.message, true); }
};

async function loadMe() {
  if (!TOKEN) return;
  try {
    const me = await api(`${BASE}/api/me`);
    if (!me || me.ok === false) return;
    const el = $("#quota");
    if (el) { el.hidden = false; el.textContent = `本单剩 ${me.remaining} / ${me.total} 页`; }
    state.me = me;
  } catch (e) {}
}
document.addEventListener("DOMContentLoaded", loadMe);
