// ============ 基础工具 ============
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];

async function api(path, opts = {}) {
  const res = await fetch("/api" + path, {
    headers: { "Content-Type": "application/json" },
    ...opts,
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  if (!res.ok) {
    const e = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(e.detail || res.statusText);
  }
  if (res.status === 204) return null;
  return res.json();
}
function toast(msg) {
  const t = $("#toast");
  t.textContent = msg;
  t.classList.add("show");
  setTimeout(() => t.classList.remove("show"), 2300);
}
async function promptInput(title, def = "") {
  return new Promise((resolve) => {
    $("#promptTitle").textContent = title;
    $("#promptInput").value = def;
    $("#promptModal").classList.add("show");
    const ok = () => { cleanup(); resolve($("#promptInput").value.trim()); };
    const cancel = () => { cleanup(); resolve(null); };
    function cleanup() {
      $("#promptModal").classList.remove("show");
      $("#promptOk").onclick = null; $("#promptCancel").onclick = null;
    }
    $("#promptOk").onclick = ok;
    $("#promptCancel").onclick = cancel;
  });
}

// ============ Tab 切换 ============
$$("nav.tabs button").forEach((b) => {
  b.onclick = () => {
    $$("nav.tabs button").forEach((x) => x.classList.remove("active"));
    b.classList.add("active");
    const tab = b.dataset.tab;
    $$(".page").forEach((p) => p.classList.remove("active"));
    $("#page-" + tab).classList.add("active");
    if (tab === "dashboard") loadStats();
    if (tab === "library") { loadFields(); loadPapers(); }
    if (tab === "news") loadNews();
    if (tab === "radar") loadTemplates();
    if (tab === "settings") loadSettings();
  };
});

// ============ 看板 ============
let trendChart, fieldChart;
async function loadStats() {
  const s = await api("/stats");
  $("#statCards").innerHTML = `
    <div class="stat"><div class="n">${s.today_count}</div><div class="l">今日阅读</div><div class="sub">本周 ${s.week_count} 篇</div></div>
    <div class="stat"><div class="n">${s.streak_days}</div><div class="l">连续阅读天数</div><div class="sub">坚持就是胜利 🔥</div></div>
    <div class="stat"><div class="n">${s.read_count}</div><div class="l">已读论文</div><div class="sub">未读 ${s.unread_count} · 在读 ${s.reading_count}</div></div>
    <div class="stat"><div class="n">${s.weekly_progress}<span style="font-size:15px;color:var(--muted)">/${s.weekly_goal}</span></div><div class="l">本周目标完成</div><div class="sub">近7天新增 ${s.new_count} 篇</div></div>`;

  const dates = s.trend.map((t) => t.date);
  const counts = s.trend.map((t) => t.count);
  if (trendChart) trendChart.destroy();
  trendChart = new Chart($("#trendChart"), {
    type: "line",
    data: { labels: dates, datasets: [{ label: "每日阅读", data: counts, borderColor: "#4f6ef7", backgroundColor: "rgba(79,110,247,.12)", fill: true, tension: .3, pointRadius: 3 }] },
    options: { responsive: true, maintainAspectRatio: false, plugins: { legend: { display: false } }, scales: { y: { beginAtZero: true, ticks: { precision: 0 } } } },
  });

  const fd = s.field_distribution;
  if (fieldChart) fieldChart.destroy();
  if (fd.length) {
    fieldChart = new Chart($("#fieldChart"), {
      type: "doughnut",
      data: { labels: fd.map((f) => f.field), datasets: [{ data: fd.map((f) => f.count), backgroundColor: ["#4f6ef7", "#1aab6b", "#e0902b", "#9b6ef7", "#e0483b", "#28b6c4", "#d669c0"] }] },
      options: { responsive: true, maintainAspectRatio: false, plugins: { legend: { position: "right" } } },
    });
  } else {
    $("#fieldChart").replaceWith?.(null);
  }
}

// ============ 论文库 ============
let ALL_FIELDS = [];
async function loadFields() {
  ALL_FIELDS = await api("/fields");
  const sel = $("#libField");
  sel.innerHTML = `<option value="">全部领域</option>` +
    ALL_FIELDS.map((f) => `<option value="${f.id}">${f.name}</option>`).join("");
}
let lastRadarItems = [];
let lastRadarType = "paper";
let paperPage = 1;
let paperPageSize = 12;
async function loadPapers(page) {
  if (page) paperPage = page;
  const q = $("#libSearch").value.trim();
  const fid = $("#libField").value;
  const st = $("#libStatus").value;
  const params = new URLSearchParams();
  if (q) params.set("q", q);
  if (fid) params.set("field_id", fid);
  if (st) params.set("status", st);
  params.set("page", paperPage);
  params.set("page_size", paperPageSize);
  const raw = await api("/papers?" + params.toString());
  // 兼容两种返回形态：旧版返回数组，新版返回 {items,total,pages,...}
  const data = Array.isArray(raw) ? { items: raw, total: raw.length, page: paperPage, page_size: paperPageSize, pages: 1 } : raw;
  const papers = data.items || [];
  const box = $("#paperList");
  if (!papers.length) {
    box.innerHTML = ""; $("#libEmpty").classList.remove("hidden"); $("#paperPager").innerHTML = ""; return;
  }
  $("#libEmpty").classList.add("hidden");
  box.innerHTML = papers.map((p) => `
    <div class="paper-card" data-id="${p.id}">
      <div class="pc-top">
        <h4>${esc(p.title)}</h4>
        <span class="badge ${p.reading_status}">${statusText(p.reading_status)}</span>
      </div>
      <div class="meta">${p.field_name ? `<span class="tag">${esc(p.field_name)}</span>` : ""}
        ${p.feishu_doc_url ? "<span class='tag'>📄 飞书</span>" : ""}</div>
      <div class="meta pc-tags">${p.tags ? esc(p.tags) : "（无标签）"}</div>
    </div>`).join("");
  $$(".paper-card", box).forEach((c) => c.onclick = () => openPaper(c.dataset.id));
  renderPager($("#paperPager"), data, loadPapers, (sz) => { paperPageSize = sz; paperPage = 1; loadPapers(); });
}
function statusText(s){return {unread:"未读",reading:"在读",read:"已读"}[s]||s;}
function esc(s){return (s||"").replace(/[&<>]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;"}[c]));}

// ============ 分页器 ============
function renderPager(box, data, goFn, onSize) {
  if (!data || data.pages <= 1) { box.innerHTML = ""; return; }
  const cur = data.page, pages = data.pages, total = data.total;
  const nums = [];
  const win = 2;
  for (let i = 1; i <= pages; i++) {
    if (i === 1 || i === pages || (i >= cur - win && i <= cur + win)) nums.push(i);
    else if (nums[nums.length - 1] !== "…") nums.push("…");
  }
  const btn = (label, page, opts = {}) =>
    `<button class="pg ${opts.cls || ""} ${opts.disabled ? "pg-disabled" : ""}" ${opts.disabled ? "disabled" : ""} data-pg="${page}">${label}</button>`;
  let html = `<div class="pg-info">共 <b>${total}</b> 条 · 第 ${cur}/${pages} 页</div><div class="pg-btns">`;
  html += btn("« 上一页", cur - 1, { disabled: cur <= 1 });
  nums.forEach((n) => {
    if (n === "…") html += `<span class="pg-ellipsis">…</span>`;
    else html += btn(n, n, { cls: n === cur ? "pg-active" : "" });
  });
  html += btn("下一页 »", cur + 1, { disabled: cur >= pages });
  html += `</div><select class="pg-size" id="pgSizeSel"><option value="12"${data.page_size===12?" selected":""}>12/页</option><option value="24"${data.page_size===24?" selected":""}>24/页</option><option value="48"${data.page_size===48?" selected":""}>48/页</option></select>`;
  box.innerHTML = html;
  $$("[data-pg]", box).forEach((b) => b.onclick = () => goFn(+b.dataset.pg));
  const sizeSel = $("#pgSizeSel", box);
  if (sizeSel && onSize) sizeSel.onchange = () => onSize(+sizeSel.value);
}

// ============ Markdown 渲染（自包含，离线可用） ============
function renderMarkdown(md) {
  if (!md) return "";
  const escHtml = (s) => s.replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));
  const inline = (s) => {
    s = escHtml(s);
    s = s.replace(/`([^`]+)`/g, "<code>$1</code>");
    s = s.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
    s = s.replace(/(^|[^*])\*([^*]+)\*/g, "$1<em>$2</em>");
    s = s.replace(/\[([^\]]+)\]\(([^)]+)\)/g, (m, t, u) =>
      `<a href="${u}" target="_blank" rel="noopener">${t}</a>`);
    return s;
  };
  const lines = md.replace(/\r\n/g, "\n").split("\n");
  let html = "", i = 0;
  while (i < lines.length) {
    let line = lines[i];
    if (/^```/.test(line)) {                       // 代码块
      const buf = [];
      i++;
      while (i < lines.length && !/^```/.test(lines[i])) { buf.push(escHtml(lines[i])); i++; }
      i++;
      html += `<pre class="md-code"><code>${buf.join("\n")}</code></pre>`;
      continue;
    }
    if (/^\s*([#]{1,6})\s+(.*)$/.test(line)) {     // 标题
      const m = line.match(/^\s*(#{1,6})\s+(.*)$/);
      const lvl = m[1].length;
      html += `<h${lvl} class="md-h">${inline(m[2])}</h${lvl}>`;
      i++; continue;
    }
    if (/^\s*([-*_])\1{2,}\s*$/.test(line)) {       // 分割线
      html += "<hr class='md-hr'>"; i++; continue;
    }
    if (/^\s*>\s?/.test(line)) {                    // 引用
      const buf = [];
      while (i < lines.length && /^\s*>\s?/.test(lines[i])) { buf.push(inline(lines[i].replace(/^\s*>\s?/, ""))); i++; }
      html += `<blockquote class="md-quote">${buf.join("<br>")}</blockquote>`;
      continue;
    }
    if (/^\s*[-*+]\s+/.test(line)) {                // 无序列表
      const buf = [];
      while (i < lines.length && /^\s*[-*+]\s+/.test(lines[i])) { buf.push("<li>" + inline(lines[i].replace(/^\s*[-*+]\s+/, "")) + "</li>"); i++; }
      html += `<ul class="md-ul">${buf.join("")}</ul>`;
      continue;
    }
    if (/^\s*\d+\.\s+/.test(line)) {                // 有序列表
      const buf = [];
      while (i < lines.length && /^\s*\d+\.\s+/.test(lines[i])) { buf.push("<li>" + inline(lines[i].replace(/^\s*\d+\.\s+/, "")) + "</li>"); i++; }
      html += `<ol class="md-ol">${buf.join("")}</ol>`;
      continue;
    }
    if (line.trim() === "") { i++; continue; }      // 空行
    const buf = [line];
    i++;
    while (i < lines.length && lines[i].trim() !== "" &&
           !/^\s*([#>`\-*+\d.])/.test(lines[i]) && !/^```/.test(lines[i])) {
      buf.push(lines[i]); i++;
    }
    html += `<p class="md-p">${inline(buf.join(" "))}</p>`;
  }
  return html;
}

// ============ 论文详情（独立页面，瀑布流版式） ============
let CURRENT_PAPER_ID = null;
async function openPaper(id) { await openPaperDetail(id); }

async function openPaperDetail(id) {
  CURRENT_PAPER_ID = id;
  const p = await api("/papers/" + id);
  const notes = renderMarkdown(p.summary);
  const tagList = (p.tags ? esc(p.tags).split(",").map(t=>`<span class="tag">${esc(t.trim())}</span>`).join("") : "");
  const hasFeishu = !!p.feishu_doc_url;
  $("#paperModalBody").innerHTML = `
    <div class="detail-page">
      <header class="dp-header">
        <div>
          <h2 class="dp-title">${esc(p.title)}</h2>
          <div class="dp-tags">
            ${p.field_name ? `<span class="tag">${esc(p.field_name)}</span>` : ""}
            <span class="badge ${p.reading_status}">${statusText(p.reading_status)}</span>
            ${tagList}
          </div>
        </div>
        <div class="dp-actions">
          <button class="btn" id="d_close">关闭</button>
        </div>
      </header>

      <div class="dp-waterfall">
        <section class="dp-card">
          <h3 class="dp-card-title">📎 资源</h3>
          <ul class="dp-links">
            ${p.original_url ? `<li><a href="${esc(p.original_url)}" target="_blank">🔗 原文链接</a></li>` : `<li class="muted">暂无原文链接</li>`}
            ${p.github_url ? `<li><a href="${esc(p.github_url)}" target="_blank">💻 GitHub 代码</a></li>` : `<li class="muted">暂无 GitHub</li>`}
            ${hasFeishu ? `<li><a href="${esc(p.feishu_doc_url)}" target="_blank">📄 飞书阅读文档</a></li>` : `<li class="muted">尚未生成飞书文档</li>`}
          </ul>
        </section>

        ${p.abstract ? `
        <section class="dp-card">
          <h3 class="dp-card-title">📝 摘要</h3>
          <p class="dp-abstract">${esc(p.abstract).replace(/\n/g,"<br>")}</p>
        </section>` : ""}

        <section class="dp-card dp-card-wide">
          <h3 class="dp-card-title">📖 我的笔记 / 心得</h3>
          <div class="markdown-body">${notes || '<span class="muted">（暂无笔记，点「编辑」添加）</span>'}</div>
        </section>

        <section class="dp-card dp-card-wide">
          <h3 class="dp-card-title">⚙️ 管理</h3>
          <div class="toolbar">
            <button class="primary" id="d_edit">✏️ 编辑</button>
            <button class="btn danger" id="d_del">删除</button>
          </div>
          <div id="d_msg" class="muted" style="margin-top:8px"></div>
        </section>
      </div>
    </div>`;
  $("#paperModal").classList.add("show");

  $("#d_close").onclick = () => $("#paperModal").classList.remove("show");
  $("#d_edit").onclick = () => openPaperEdit(id);
  $("#d_del").onclick = async () => {
    if (!confirm("确认删除该论文？")) return;
    await api("/papers/" + id, { method: "DELETE" });
    toast("已删除"); $("#paperModal").classList.remove("show"); loadPapers(); loadStats();
  };
}

// ============ 论文编辑（表单模式） ============
async function openPaperEdit(id) {
  const p = await api("/papers/" + id);
  const fieldsOpt = ALL_FIELDS.map((f) => `<option value="${f.id}" ${f.id===p.field_id?"selected":""}>${f.name}</option>`).join("");
  const texPath = p.tex_repo_path || "";
  $("#paperModalBody").innerHTML = `
    <h3>编辑论文</h3>
    <div class="field-row"><label>标题</label><input id="m_title" value="${esc(p.title)}" style="width:100%"></div>
    <div class="field-row"><label>领域</label><select id="m_field"><option value="">（未分类）</option>${fieldsOpt}</select></div>
    <div class="field-row"><label>状态</label><select id="m_status">
      <option value="unread" ${p.reading_status==="unread"?"selected":""}>未读</option>
      <option value="reading" ${p.reading_status==="reading"?"selected":""}>在读</option>
      <option value="read" ${p.reading_status==="read"?"selected":""}>已读</option></select></div>
    <div class="field-row"><label>标签</label><input id="m_tags" value="${esc(p.tags)}" placeholder="逗号分隔" style="width:100%"></div>
    <div class="field-row">
      <label>原文链接</label>
      <div class="inline-input">
        <input id="m_url" value="${esc(p.original_url)}" placeholder="https://arxiv.org/abs/xxxx" style="width:100%">
        <button class="btn sm" id="m_fetch">🔄 获取元数据</button>
      </div>
    </div>
    <div class="field-row"><label>GitHub</label><input id="m_gh" value="${esc(p.github_url)}" style="width:100%"></div>
    <div class="field-row"><label>摘要</label><textarea id="m_abstract" rows="4" style="width:100%">${esc(p.abstract)}</textarea></div>
    <div class="field-row">
      <label>我的笔记/心得</label>
      <div class="md-editor">
        <div class="md-toolbar">
          <button class="btn sm" id="m_tab_edit" data-tab="edit">编辑</button>
          <button class="btn sm" id="m_tab_prev" data-tab="prev">预览</button>
          <span class="sep-line"></span>
          <button class="btn sm" id="m_upload_md">⬆️ 上传 .md</button>
          <button class="btn sm" id="m_import_feishu">🔗 从飞书导入</button>
          <input type="file" id="m_md_file" accept=".md,.markdown,.txt" style="display:none">
        </div>
        <textarea id="m_summary" rows="10" class="md-input" style="width:100%">${esc(p.summary)}</textarea>
        <div id="m_summary_preview" class="markdown-body md-preview hidden"></div>
      </div>
    </div>
    <div class="field-row">
      <label>TeX 仓库</label>
      <div class="inline-input">
        <input id="m_tex_show" value="${esc(texPath)}" placeholder="点击下方按钮选择文件夹上传" style="width:100%" readonly>
        <button class="btn sm" id="m_tex_pick">📁 选择文件夹</button>
        <input type="file" id="m_tex_folder" webkitdirectory directory multiple style="display:none">
        <button class="btn sm primary hidden" id="m_tex_convert">📄 转飞书文档</button>
      </div>
      <div id="m_tex_status" class="muted" style="margin-top:4px"></div>
      <div id="m_tex_result" style="margin-top:6px"></div>
    </div>
    <hr class="sep">
    <div class="toolbar">
      <button class="primary" id="m_save">保存</button>
      <button class="btn" id="m_back">返回详情</button>
      <button class="btn" id="m_close">关闭</button>
    </div>
    <div id="m_msg" class="muted"></div>`;
  $("#paperModal").classList.add("show");

  const summaryEl = $("#m_summary");
  const previewEl = $("#m_summary_preview");
  $("#m_tab_edit").onclick = () => {
    summaryEl.classList.remove("hidden"); previewEl.classList.add("hidden");
    $("#m_tab_edit").classList.add("primary"); $("#m_tab_prev").classList.remove("primary");
  };
  $("#m_tab_prev").onclick = () => {
    previewEl.innerHTML = renderMarkdown(summaryEl.value);
    summaryEl.classList.add("hidden"); previewEl.classList.remove("hidden");
    $("#m_tab_prev").classList.add("primary"); $("#m_tab_edit").classList.remove("primary");
  };

  // 上传 .md 文件解析
  $("#m_upload_md").onclick = () => $("#m_md_file").click();
  $("#m_md_file").onchange = async (e) => {
    const f = e.target.files[0];
    if (!f) return;
    const text = await f.text();
    summaryEl.value = text;
    previewEl.innerHTML = renderMarkdown(text);
    toast("已载入 " + f.name);
  };
  // 从飞书文档导入
  $("#m_import_feishu").onclick = async () => {
    const url = await promptInput("粘贴飞书云文档链接");
    if (!url) return;
    try {
      const r = await api("/papers/" + id + "/import_md", { method: "POST", body: { feishu_url: url } });
      summaryEl.value = r.markdown;
      previewEl.innerHTML = renderMarkdown(r.markdown);
      toast("已从飞书导入笔记");
    } catch (e) { toast("导入失败：" + e.message); }
  };

  // TeX 文件夹上传
  $("#m_tex_pick").onclick = () => $("#m_tex_folder").click();
  const showExistingFeishu = () => {
    if (p.feishu_doc_url) {
      $("#m_tex_result").innerHTML = `📄 已生成飞书文档：<a href="${esc(p.feishu_doc_url)}" target="_blank">${esc(p.feishu_doc_url)}</a>`;
    }
  };
  $("#m_tex_folder").onchange = async (e) => {
    const files = [...e.target.files];
    if (!files.length) return;
    const fd = new FormData();
    files.forEach((f) => fd.append("files", f, f.webkitRelativePath || f.name));
    $("#m_tex_status").textContent = `正在上传 ${files.length} 个文件...`;
    try {
      const r = await fetch("/api/papers/" + id + "/upload_tex", { method: "POST", body: fd });
      if (!r.ok) { const er = await r.json().catch(()=>({detail:r.statusText})); throw new Error(er.detail||r.statusText); }
      const j = await r.json();
      $("#m_tex_show").value = j.path;
      $("#m_tex_status").textContent = `✅ 已上传 ${j.files} 个文件到服务器`;
      $("#m_tex_convert").classList.remove("hidden");
      $("#m_tex_result").innerHTML = "";
      toast("TeX 仓库已上传，可点击「转飞书文档」");
    } catch (err) { $("#m_tex_status").textContent = "上传失败：" + err.message; }
  };
  // 已存在 TeX 路径时，也展示转换按钮
  if (texPath) $("#m_tex_convert").classList.remove("hidden");
  showExistingFeishu();

  // TeX 仓库 → 飞书文档转换
  $("#m_tex_convert").onclick = async () => {
    $("#m_tex_result").innerHTML = `<span class="muted">⏳ 正在转换（可能需浏览器授权飞书）...</span>`;
    try {
      const r = await api("/papers/" + id + "/generate_feishu", { method: "POST", body: {} });
      p.feishu_doc_url = r.url;
      $("#m_tex_result").innerHTML = `✅ 已生成飞书文档：<a href="${esc(r.url)}" target="_blank">${esc(r.url)}</a>`;
      toast("飞书文档已生成");
    } catch (e) { $("#m_tex_result").innerHTML = `<span class="muted">转换失败：${esc(e.message)}</span>`; }
  };

  $("#m_close").onclick = () => $("#paperModal").classList.remove("show");
  $("#m_back").onclick = () => openPaperDetail(id);
  $("#m_save").onclick = async () => {
    await api("/papers/" + id, { method: "PUT", body: {
      title: $("#m_title").value, field_id: $("#m_field").value ? +$("#m_field").value : null,
      reading_status: $("#m_status").value, tags: $("#m_tags").value,
      original_url: $("#m_url").value, github_url: $("#m_gh").value,
      abstract: $("#m_abstract").value,
      summary: $("#m_summary").value, tex_repo_path: $("#m_tex_show").value,
    }});
    toast("已保存"); openPaperDetail(id); loadPapers(); loadStats();
  };
  $("#m_fetch").onclick = async () => {
    const url = $("#m_url").value.trim();
    if (!url) return toast("请先填写原文链接");
    const m = await api("/papers/fetch_metadata?url=" + encodeURIComponent(url));
    if (m.found) {
      if (m.title) $("#m_title").value = m.title;
      if (m.abstract) $("#m_abstract").value = m.abstract;
      if (m.github) $("#m_gh").value = m.github;
      if (m.arxiv_id) toast("已获取 arXiv 元数据"); else toast("已尽力获取页面信息");
    } else toast("未能获取信息");
  };
}

$("#btnAddPaper").onclick = async () => {
  const title = await promptInput("新增论文 - 标题");
  if (!title) return;
  const p = await api("/papers", { method: "POST", body: { title, reading_status: "unread" } });
  openPaperEdit(p.id); loadPapers(); loadStats();
};
$("#btnAddField").onclick = async () => {
  const name = await promptInput("新增研究领域");
  if (!name) return;
  try { await api("/fields", { method: "POST", body: { name } }); toast("已新增领域"); loadFields(); }
  catch (e) { toast(e.message); }
};
$("#libSearch").oninput = debounce(() => { paperPage = 1; loadPapers(); }, 300);
$("#libField").onchange = () => { paperPage = 1; loadPapers(); };
$("#libStatus").onchange = () => { paperPage = 1; loadPapers(); };
function debounce(fn, ms){let t;return(...a)=>{clearTimeout(t);t=setTimeout(()=>fn(...a),ms);};}

// ============ Research Radar ============
async function loadTemplates() {
  const tpls = await api("/radar/configs");
  $("#tplList").innerHTML = tpls.length ? tpls.map((t) => `
    <div style="display:flex;align-items:center;gap:10px;padding:8px 0;border-bottom:1px solid var(--line)">
      <span class="pill">${t.type === "news" ? "资讯" : "论文"}</span>
      <strong style="min-width:120px">${esc(t.name)}</strong>
      <span class="muted">${esc(t.field || "—")} · ${esc(t.keywords || "—")}</span>
      <span class="muted" style="margin-left:auto">${t.time_range_days}天${t.type === "news" ? " · " + (t.lang === "zh" ? "中文" : t.lang === "auto" ? "自动" : "英文") + " · " + (t.channel === "cn" ? "国内媒体" : t.channel === "all" ? "全渠道" : "Google") : ""}</span>
      <button class="btn sm" data-run="${t.id}">运行</button>
      <button class="btn sm danger" data-del="${t.id}">删</button>
    </div>`).join("") : `<div class="muted">暂无模板，点击「新增模板」。</div>`;
  $$("#tplList [data-run]").forEach((b) => b.onclick = () => runTemplate(b.dataset.run));
  $$("#tplList [data-del]").forEach((b) => b.onclick = async () => {
    await api("/radar/configs/" + b.dataset.del, { method: "DELETE" }); toast("已删除"); loadTemplates();
  });
}
$("#btnAddTpl").onclick = async () => {
  const name = await promptInput("模板名称"); if (!name) return;
  const type = await promptInput("类型 (paper / news)", "paper"); if (!type) return;
  const field = await promptInput("领域（可选）", "");
  const keywords = await promptInput("搜索关键词（逗号分隔）", "");
  const days = +(await promptInput("时间范围（天）", "2")) || 2;
  let lang = "en", channel = "all";
  if (type === "news") {
    lang = (await promptInput("资讯语言 (en / zh / auto)", "en") || "en");
    channel = (await promptInput("资讯渠道 (all / google / cn)", "all") || "all");
  }
  await api("/radar/configs", { method: "POST", body: { name, type, field, keywords, time_range_days: days, lang, channel } });
  toast("已新增模板"); loadTemplates();
};
async function runTemplate(id) {
  try {
    const r = await api("/radar/run_template/" + id, { method: "POST" });
    renderRadar(r);
  } catch (e) {
    $("#radarCount").textContent = "检索失败";
    $("#radarResults").innerHTML = `<div class="empty">检索失败：${esc(e.message)}<br>请检查本机网络是否能访问 arXiv / Google News。</div>`;
  }
}
$("#btnRun").onclick = async () => {
  const type = $("#runType").value;
  const keywords = $("#runKeywords").value.trim();
  const field = $("#runField").value.trim();
  const days = +$("#runDays").value;
  const max = +$("#runMax").value || 30;
  const lang = type === "news" ? $("#runLang").value : "en";
  const channel = type === "news" ? $("#runChannel").value : "google";
  try {
    const r = await api("/radar/run", { method: "POST", body: { type, keywords, field, days, max_results: max, lang, channel } });
    renderRadar(r);
  } catch (e) {
    $("#radarCount").textContent = "检索失败";
    $("#radarResults").innerHTML = `<div class="empty">检索失败：${esc(e.message)}<br>请检查本机网络是否能访问 arXiv / Google News。</div>`;
  }
};
function renderRadar(r) {
  lastRadarItems = r.results;
  lastRadarType = r.type;
  $("#radarCount").textContent = `共 ${r.count} 条结果`;
  const box = $("#radarResults");
  // 多源诊断面板（资讯渠道可见每个源的成功/失败）
  let diag = "";
  if (r.type === "news" && r.sources && r.sources.length) {
    const rows = r.sources.map((s) => {
      const color = s.status === "ok" ? "#2e9e5b" : "#d9534f";
      const tag = s.status === "ok" ? "✓" : "✗";
      return `<span style="display:inline-block;margin:2px 10px 2px 0;color:${color}">${tag} ${esc(s.name)} <span class="muted">(${esc(s.detail || "")})</span></span>`;
    }).join("");
    diag = `<div class="src-diag">检索源状态：${rows}</div>`;
  }
  if (!r.results.length) {
    box.innerHTML = diag + `<div class="empty">该时间范围内未检索到匹配的论文/资讯。<br>可尝试：放宽「时间范围」到 14/30 天，或更换关键词。<br>若下方有 ✗ 源，说明对应渠道当前不可用（多为网络/代理限制或源失效）。</div>`;
    return;
  }
  box.innerHTML = diag;
  if (r.type === "news") {
    box.innerHTML = `<div class="news-feed">` + r.results.map((it, i) => `
      <div class="news-item">
        <h4>${esc(it.title)}</h4>
        <div class="src">${esc(it.source || "—")} · ${esc(it.published || "")} ${it.github?`· <a href="${it.github}" target="_blank">GitHub</a>`:""}</div>
        <div class="toolbar" style="margin-top:6px"><a class="btn sm" href="${esc(it.url)}" target="_blank">查看原文</a>
          <button class="btn sm" data-news="${i}">加入资讯库</button></div>
      </div>`).join("") + `</div>`;
  } else {
    box.innerHTML = `<div class="paper-list">` + r.results.map((it, i) => `
      <div class="paper-card">
        <h4>${esc(it.title)}</h4>
        <div class="meta">${it.category?`<span class="tag">${esc(it.category)}</span>`:""}
          ${it.in_library?"<span class='badge read'>已在库</span>":"<span class='badge unread'>新发现</span>"}</div>
        <div class="meta" style="margin:6px 0">${esc((it.abstract||"").slice(0,160))}...</div>
        <div class="meta">${esc(it.published||"")} ${it.github?`· <a href="${it.github}" target="_blank">GitHub</a>`:""}</div>
        <div class="toolbar" style="margin-top:6px"><a class="btn sm" href="${esc(it.url)}" target="_blank">原文</a>
          ${it.in_library?"":`<button class="btn sm primary" data-paper="${i}">加入论文库</button>`}</div>
      </div>`).join("") + `</div>`;
  }
  $$("[data-paper]", box).forEach((b) => b.onclick = () => addRadarPaper(+b.dataset.paper, b));
  $$("[data-news]", box).forEach((b) => b.onclick = () => addRadarNews(+b.dataset.news, b));
}
async function addRadarPaper(i, btn) {
  const it = lastRadarItems[i];
  const payload = [{ title: it.title, abstract: it.abstract || "", url: it.url, github: it.github || "", field: it.field || "", arxiv_id: it.arxiv_id || "" }];
  const r = await api("/papers/from_radar", { method: "POST", body: payload });
  toast(`已收录 ${r.created} 篇`);
  btn.textContent = "已加入"; btn.disabled = true;
  loadStats();
}
async function addRadarNews(i, btn) {
  const it = lastRadarItems[i];
  const r = await api("/news", { method: "POST", body: {
    title: it.title, url: it.url, source: it.source || "", published: it.published || "",
  }});
  toast("已加入资讯库");
  btn.textContent = "已加入"; btn.disabled = true;
  loadNews();
}
$("#btnAddAll").onclick = async () => {
  if (!lastRadarItems.length) return toast("请先运行检索");
  if (lastRadarType === "news") {
    const items = lastRadarItems.map((it) => ({
      title: it.title, url: it.url, source: it.source || "", published: it.published || "",
    }));
    const r = await api("/news/bulk", { method: "POST", body: { items } });
    toast(`批量加入资讯库 ${r.added} 条（共 ${r.total} 条）`); loadNews();
  } else {
    const payload = lastRadarItems.map((it) => ({
      title: it.title, abstract: it.abstract || "", url: it.url,
      github: it.github || "", field: it.field || "", arxiv_id: it.arxiv_id || "",
    }));
    const r = await api("/papers/from_radar", { method: "POST", body: payload });
    toast(`批量收录 ${r.created} 篇`); loadStats();
  }
};

// ============ 资讯库 ============
let newsPage = 1;
let newsPageSize = 12;
async function loadNews(page) {
  if (page) newsPage = page;
  const q = $("#newsSearch").value.trim();
  const src = $("#newsSource").value;
  const params = new URLSearchParams();
  if (q) params.set("q", q);
  if (src) params.set("source", src);
  params.set("page", newsPage);
  params.set("page_size", newsPageSize);
  const raw = await api("/news?" + params.toString());
  // 兼容两种返回形态：旧版返回数组，新版返回 {items,total,pages,...}
  const data = Array.isArray(raw) ? { items: raw, total: raw.length, page: newsPage, page_size: newsPageSize, pages: 1 } : raw;
  const items = data.items || [];
  const box = $("#newsList");
  $("#newsCount").textContent = `共 ${data.total || 0} 条`;
  if (!items.length) {
    box.innerHTML = ""; $("#newsEmpty").classList.remove("hidden"); $("#newsPager").innerHTML = ""; return;
  }
  $("#newsEmpty").classList.add("hidden");
  box.innerHTML = items.map((n) => `
    <div class="news-card" data-id="${n.id}">
      <div class="nc-head">
        <h4><a href="${esc(n.url)}" target="_blank" rel="noopener">${esc(n.title)}</a></h4>
        <button class="btn sm danger" data-del="${n.id}">删除</button>
      </div>
      <div class="nc-meta">
        ${n.source?`<span class="tag">${esc(n.source)}</span>`:""}
        ${n.published?`<span class="muted">${esc(n.published)}</span>`:""}
      </div>
      ${n.summary?`<div class="nc-summary">${esc(n.summary)}</div>`:""}
    </div>`).join("");
  $$("#newsList [data-del]").forEach((b) => b.onclick = async () => {
    if (!confirm("确认删除该资讯？")) return;
    await api("/news/" + b.dataset.del, { method: "DELETE" });
    toast("已删除"); loadNews();
  });
  // 来源下拉只在第一趟填充
  if (!$("#newsSource").dataset.filled) {
    const sources = [...new Set(items.map((n) => n.source).filter(Boolean))].sort();
    $("#newsSource").innerHTML = `<option value="">全部来源</option>` +
      sources.map((s) => `<option value="${esc(s)}">${esc(s)}</option>`).join("");
    $("#newsSource").dataset.filled = "1";
  }
  renderPager($("#newsPager"), data, loadNews, (sz) => { newsPageSize = sz; newsPage = 1; loadNews(); });
}
$("#newsSearch").oninput = debounce(() => { newsPage = 1; loadNews(); }, 300);
$("#newsSource").onchange = () => { newsPage = 1; loadNews(); };

// ============ 设置 ============
async function loadSettings() {
  const api2 = await api("/settings/api");
  $("#apiProvider").value = api2.provider || "OpenAI-Compatible";
  $("#apiBase").value = api2.base_url || "";
  $("#apiModel").value = api2.model_name || "";
  $("#apiOther").value = api2.other_params || "{}";
  $("#apiKey").value = "";
  const prefs = await api("/settings/prefs");
  $("#weeklyGoal").value = prefs.weekly_goal;
  const fc = await api("/feishu/config");
  $("#feishuAppId").value = fc.app_id || "";
  const px = await api("/settings/radar_proxy");
  $("#radarProxy").value = px.proxy || "";
  // arXiv 源
  const ax = await api("/settings/arxiv_source");
  $("#arxivSource").innerHTML = ax.sources.map((s) =>
    `<option value="${s.key}" ${s.key === ax.current ? "selected" : ""}>${esc(s.name)}${s.cn ? " · 国内直连" : " · 可能需代理"}</option>`
  ).join("");
  $("#arxivSourceTip").textContent = "当前：" + (ax.sources.find((s) => s.key === ax.current)?.name || ax.current);
  // 自定义 RSS 源
  try {
    const rss = await api("/settings/news_rss");
    $("#rssBuiltin").textContent = "内置源：" + (rss.builtin || []).map((b) => b.name).join("、");
    const custom = (rss.feeds || []).filter((f) => !f.builtin);
    $("#rssFeeds").value = custom.map((f) => `${f.name}|${f.url}`).join("\n");
  } catch (e) { /* RSS 可选 */ }
  // RSSHub + 微信公众号
  try {
    const rh = await api("/settings/rsshub");
    $("#rsshubBase").value = rh.base || "";
    const wx = await api("/settings/wechat");
    $("#wechatAccounts").value = (wx.accounts || []).map((a) => `${a.name}|${a.gh}`).join("\n");
  } catch (e) { /* 可选 */ }
}
$("#btnSaveApi").onclick = async () => {
  await api("/settings/api", { method: "PUT", body: {
    provider: $("#apiProvider").value, base_url: $("#apiBase").value,
    api_key: $("#apiKey").value, model_name: $("#apiModel").value, other_params: $("#apiOther").value,
  }});
  toast("API 配置已保存（Key 已加密）"); $("#apiKey").value = "";
};
$("#btnSaveProxy").onclick = async () => {
  await api("/settings/radar_proxy", { method: "PUT", body: { proxy: $("#radarProxy").value.trim() } });
  toast("代理已保存，下次检索生效");
};
$("#btnSaveArxiv").onclick = async () => {
  const r = await api("/settings/arxiv_source", { method: "PUT", body: { source: $("#arxivSource").value } });
  $("#arxivSourceTip").textContent = "当前：" + (r.sources.find((s) => s.key === r.current)?.name || r.current);
  toast("arXiv 源已保存，下次检索生效");
};
$("#btnSaveRss").onclick = async () => {
  const lines = $("#rssFeeds").value.split("\n").map((l) => l.trim()).filter(Boolean);
  const feeds = lines.map((l) => {
    const i = l.indexOf("|");
    if (i < 0) return { name: "自定义源", url: l };
    return { name: l.slice(0, i).trim(), url: l.slice(i + 1).trim() };
  });
  await api("/settings/news_rss", { method: "PUT", body: { feeds } });
  $("#rssTip").textContent = "已保存 " + feeds.length + " 个自定义源";
  toast("自定义 RSS 已保存，下次资讯检索生效");
};
$("#btnSaveRsshub").onclick = async () => {
  const base = $("#rsshubBase").value.trim();
  const r = await api("/settings/rsshub", { method: "PUT", body: { base } });
  $("#rsshubTip").textContent = "已保存：" + (r.base || "（空，使用默认公共实例）");
  toast("RSSHub 地址已保存，下次检索生效");
};
$("#btnSaveWechat").onclick = async () => {
  const lines = $("#wechatAccounts").value.split("\n").map((l) => l.trim()).filter(Boolean);
  const accounts = lines.map((l) => {
    const i = l.indexOf("|");
    if (i < 0) return { name: "公众号", gh: l };
    return { name: l.slice(0, i).trim(), gh: l.slice(i + 1).trim() };
  });
  await api("/settings/wechat", { method: "PUT", body: { accounts } });
  $("#wechatTip").textContent = "已保存 " + accounts.length + " 个公众号";
  toast("公众号列表已保存，下次检索生效");
};
$("#btnSavePrefs").onclick = async () => {
  await api("/settings/prefs", { method: "PUT", body: { weekly_goal: +$("#weeklyGoal").value || 5 } });
  toast("目标已保存"); loadStats();
};
$("#btnSaveFeishu").onclick = async () => {
  await api("/feishu/config", { method: "PUT", body: { app_id: $("#feishuAppId").value, app_secret: $("#feishuSecret").value } });
  toast("飞书凭证已保存");
};
$("#btnAuthFeishu").onclick = async () => {
  $("#feishuStatus").textContent = "请在浏览器完成授权...";
  try {
    const r = await api("/feishu/authorize", { method: "POST" });
    $("#feishuStatus").textContent = "✅ 授权成功：" + r.token_masked;
  } catch (e) { $("#feishuStatus").textContent = "授权失败：" + e.message; }
};

// ============ 启动 ============
loadStats();
