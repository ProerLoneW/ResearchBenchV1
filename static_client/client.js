/*
 * ResearchBench 本地客户端（static_client/client.js）
 *
 * 设计要点：
 * 1. 普通脚本（非 ES module）—— file:// 双击打开时 type="module" 会被 CORS 拦截，
 *    也不会用 fetch() 去读本地文件（同样被拦）。导入 JSON 走 <input type="file"> + FileReader。
 * 2. 检索能力全部来自远程服务（/api/services/*），这些接口无状态、不落库、不需要密码。
 * 3. 论文库保存在浏览器 localStorage（键名 researchbench.papers），与 example_demo 约定一致；
 *    「是否已在库」由浏览器自己判断，服务器看不到你的论文库。
 * 4. 需要本地文件系统 / 用户 OAuth 的能力（TeX→飞书、遍历目录）一律不做，页面上有占位说明。
 */
(function () {
  "use strict";

  var URL_KEY = "researchbench.serviceUrl";
  var LIB_KEY = "researchbench.papers";

  var $ = function (s) { return document.querySelector(s); };

  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#039;");
  }

  // ---------------------------------------------------------------------------
  // 服务地址
  // ---------------------------------------------------------------------------
  function getBase() {
    return (localStorage.getItem(URL_KEY) || "").replace(/\/+$/, "");
  }

  function ensureBase() {
    var base = getBase();
    if (!base) {
      base = window.prompt(
        "首次使用，请输入 ResearchBench 服务地址（含端口）：",
        "http://127.0.0.1:8765"
      );
      if (!base) return "";
      base = base.trim().replace(/\/+$/, "");
      if (!/^https?:\/\//i.test(base)) base = "http://" + base;
      localStorage.setItem(URL_KEY, base);
    }
    $("#serviceUrl").value = base;
    $("#urlStatus").textContent = "当前服务地址：" + base;
    return base;
  }

  async function svcGet(path) {
    var res = await fetch(getBase() + path, { method: "GET" });
    if (!res.ok) throw new Error(res.status + " " + res.statusText);
    return res.json();
  }

  async function svcPost(path, body) {
    // 服务端接口一律用 JSON body + Pydantic model 接收（不要用 query 参数）
    var res = await fetch(getBase() + path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {}),
    });
    var data = await res.json().catch(function () { return null; });
    if (!res.ok) {
      var detail = (data && data.detail) || res.statusText;
      throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
    }
    return data;
  }

  // ---------------------------------------------------------------------------
  // 本地论文库
  // ---------------------------------------------------------------------------
  function loadLibrary() {
    try {
      var arr = JSON.parse(localStorage.getItem(LIB_KEY) || "[]");
      return Array.isArray(arr) ? arr : [];
    } catch (e) {
      console.warn("读取本地论文库失败：", e);
      return [];
    }
  }

  function saveLibrary(list) {
    localStorage.setItem(LIB_KEY, JSON.stringify(list));
  }

  function libKeys() {
    var set = new Set();
    loadLibrary().forEach(function (p) {
      if (p && p.arxiv_id) set.add(p.arxiv_id);
      if (p && p.original_url) set.add(p.original_url);
      if (p && p.url) set.add(p.url);
    });
    return set;
  }

  function itemKey(it) {
    return it.arxiv_id || it.url || it.original_url || "";
  }

  function addToLibrary(it) {
    var list = loadLibrary();
    var key = itemKey(it);
    if (key && list.some(function (p) { return itemKey(p) === key; })) {
      return false;
    }
    list.unshift({
      arxiv_id: it.arxiv_id || "",
      title: it.title || "",
      abstract: it.abstract || it.summary || "",
      url: it.url || "",
      original_url: it.original_url || it.url || "",
      github: it.github || "",
      category: it.category || "",
      field: it.field || "",
      published: it.published || "",
      source: it.source || "",
      type: it.type || "paper",
      added_at: new Date().toISOString(),
    });
    saveLibrary(list);
    return true;
  }

  function removeFromLibrary(key) {
    var list = loadLibrary().filter(function (p) { return itemKey(p) !== key; });
    saveLibrary(list);
  }

  // ---------------------------------------------------------------------------
  // 渲染
  // ---------------------------------------------------------------------------
  function renderLibrary() {
    var list = loadLibrary();
    var box = $("#library");
    if (!list.length) {
      box.innerHTML = '<div class="empty">论文库还是空的。在检索结果里点「加入我的论文库」即可收藏。</div>';
      return;
    }
    box.innerHTML =
      '<table><thead><tr><th style="width:38%">标题</th><th>领域 / 来源</th><th style="width:110px">加入时间</th><th style="width:80px"></th></tr></thead><tbody>' +
      list.map(function (p) {
        var key = itemKey(p);
        var title = p.url
          ? '<a href="' + esc(p.url) + '" target="_blank" rel="noopener">' + esc(p.title || "(无标题)") + "</a>"
          : esc(p.title || "(无标题)");
        return (
          "<tr>" +
          "<td>" + title + "</td>" +
          "<td>" + (p.field ? '<span class="tag">' + esc(p.field) + "</span>" : "") + esc(p.source || p.category || "—") + "</td>" +
          "<td>" + esc((p.added_at || "").slice(0, 10)) + "</td>" +
          '<td><button class="ghost tiny" data-del="' + esc(key) + '">移除</button></td>' +
          "</tr>"
        );
      }).join("") +
      "</tbody></table>" +
      '<p class="stat" style="margin:10px 0 0">共 ' + list.length + ' 条</p>';

    Array.prototype.forEach.call(box.querySelectorAll("[data-del]"), function (btn) {
      btn.onclick = function () {
        removeFromLibrary(btn.getAttribute("data-del"));
        renderLibrary();
        if (lastResults) renderResults(lastResults);
      };
    });
  }

  var lastResults = null;

  function renderResults(r) {
    lastResults = r;
    var existing = libKeys();
    var box = $("#results");

    var diag = "";
    if (r.type === "news" && r.sources && r.sources.length) {
      diag = '<div class="src-diag">检索源状态：' + r.sources.map(function (s) {
        var color = s.status === "ok" ? "#2e9e5b" : "#d9534f";
        var tag = s.status === "ok" ? "✓" : "✗";
        return '<span style="display:inline-block;margin:2px 10px 2px 0;color:' + color + '">' +
          tag + " " + esc(s.name) + ' <span style="color:#858b96">(' + esc(s.detail || "") + ")</span></span>";
      }).join("") + "</div>";
    }

    if (!r.results || !r.results.length) {
      box.innerHTML = diag + '<div class="empty">该时间范围内未检索到匹配结果。<br>' +
        "可尝试放宽「时间范围」到 14/30 天，或更换关键词。<br>" +
        "若上方有 ✗ 源，说明服务器当前访问不到该渠道（多为网络/代理限制）。</div>";
      return;
    }

    var items = r.results.map(function (it, i) {
      var key = itemKey(it);
      var inLib = Boolean(key && existing.has(key));
      var head = '<div class="item">' +
        "<h3>" + esc(it.title) + "</h3>" +
        '<div class="meta">' +
        (it.category ? '<span class="tag">' + esc(it.category) + "</span>" : "") +
        (it.field ? '<span class="tag">' + esc(it.field) + "</span>" : "") +
        '<span class="' + (inLib ? "read" : "unread") + '">' + (inLib ? "已在库" : "新发现") + "</span>" +
        (it.published ? " · " + esc(it.published) : "") +
        (it.source ? " · " + esc(it.source) : "") +
        "</div>";

      var bodyText = it.abstract || it.summary || "";
      var body = bodyText
        ? "<p>" + esc(bodyText.slice(0, 400)) + (bodyText.length > 400 ? "…" : "") + "</p>"
        : "";

      var links =
        '<a class="link" href="' + esc(it.url || it.original_url || "#") + '" target="_blank" rel="noopener">原文 →</a>' +
        (it.github ? ' <a class="link" href="' + esc(it.github) + '" target="_blank" rel="noopener">GitHub →</a>' : "");

      var action = inLib
        ? '<button class="ghost tiny" disabled>已在库</button>'
        : '<button class="tiny" data-add="' + i + '">加入我的论文库</button>';

      return head + body + '<div class="row" style="margin-top:10px">' + links + action + "</div></div>";
    }).join("");

    box.innerHTML = diag + '<div class="feed">' + items + "</div>";

    Array.prototype.forEach.call(box.querySelectorAll("[data-add]"), function (btn) {
      btn.onclick = function () {
        var it = r.results[Number(btn.getAttribute("data-add"))];
        it.type = r.type;
        if (addToLibrary(it)) {
          renderLibrary();
          renderResults(r);
          $("#radarStatus").textContent = "已加入本地论文库（记得定期导出 JSON 备份）";
        }
      };
    });
  }

  // ---------------------------------------------------------------------------
  // 交互
  // ---------------------------------------------------------------------------
  async function testConnection() {
    var base = getBase();
    if (!base) { ensureBase(); return; }
    $("#urlStatus").textContent = "测试中…";
    try {
      var h = await svcGet("/api/services/health");
      $("#urlStatus").textContent = "连接正常：" + base + "（" + (h.status || "ok") + "）";
    } catch (e) {
      $("#urlStatus").textContent = "连接失败：" + e.message +
        "　请确认服务已启动、端口已放行，且服务端 ALLOW_ORIGINS 允许本页来源（默认 *）。";
    }
  }

  async function runRadar() {
    var btn = $("#runBtn");
    var status = $("#radarStatus");
    if (!getBase()) { ensureBase(); return; }

    btn.disabled = true;
    btn.textContent = "检索中…";
    status.textContent = "正在调用远程检索服务…";
    $("#results").innerHTML = '<div class="empty">检索中，请稍候…</div>';

    var payload = {
      type: $("#fType").value,
      keywords: $("#fKeywords").value.trim(),
      field: $("#fField").value.trim(),
      days: Number($("#fDays").value) || 7,
      max_results: Number($("#fMax").value) || 30,
      lang: $("#fLang").value,
      channel: $("#fChannel").value,
    };

    try {
      var r = await svcPost("/api/services/radar/run", payload);
      status.textContent = "共 " + r.count + " 条结果";
      renderResults(r);
    } catch (e) {
      status.textContent = "检索失败";
      $("#results").innerHTML = '<div class="empty">检索失败：' + esc(e.message) + "<br><br>" +
        "若提示网络相关错误，请到服务器上确认能否访问 arXiv / Google News，<br>" +
        "或在服务端的 <code>.env</code> 里配置 <code>RADAR_PROXY</code>。</div>";
    } finally {
      btn.disabled = false;
      btn.textContent = "▶ 运行 Radar";
    }
  }

  async function addByUrl() {
    if (!getBase()) { ensureBase(); return; }
    var url = window.prompt("输入论文链接（arXiv 或普通网页），抓取标题/摘要后加入本地论文库：");
    if (!url || !url.trim()) return;
    $("#radarStatus").textContent = "正在抓取元数据…";
    try {
      var meta = await svcPost("/api/services/metadata", { url: url.trim() });
      if (!meta.found) { $("#radarStatus").textContent = "未能从该链接解析出元信息"; return; }
      meta.type = "paper";
      if (addToLibrary(meta)) {
        $("#radarStatus").textContent = "已加入本地论文库：" + (meta.title || url);
        renderLibrary();
      } else {
        $("#radarStatus").textContent = "该论文已在本地论文库中";
      }
    } catch (e) {
      $("#radarStatus").textContent = "抓取失败：" + e.message;
    }
  }

  function exportJson() {
    var list = loadLibrary();
    if (!list.length) { window.alert("论文库是空的，没有可导出的内容。"); return; }
    var blob = new Blob([JSON.stringify(list, null, 2)], { type: "application/json" });
    var a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = "researchbench-papers-" + new Date().toISOString().slice(0, 10) + ".json";
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    setTimeout(function () { URL.revokeObjectURL(a.href); }, 1000);
    $("#radarStatus").textContent = "已导出 " + list.length + " 条（重要：浏览器清缓存会清空本地论文库，请妥善保存此文件）";
  }

  function importJson(file) {
    var reader = new FileReader();
    reader.onload = function () {
      try {
        var data = JSON.parse(String(reader.result));
        if (!Array.isArray(data)) throw new Error("文件内容不是数组");
        var list = loadLibrary();
        var keys = libKeys();
        var added = 0;
        data.forEach(function (p) {
          if (!p || typeof p !== "object") return;
          var k = itemKey(p);
          if (k && keys.has(k)) return;
          if (k) keys.add(k);
          list.push(p);
          added += 1;
        });
        saveLibrary(list);
        renderLibrary();
        $("#radarStatus").textContent = "导入完成：新增 " + added + " 条，当前共 " + list.length + " 条";
      } catch (e) {
        window.alert("导入失败：" + e.message);
      }
    };
    reader.readAsText(file);
  }

  function syncTypeUI() {
    var isNews = $("#fType").value === "news";
    $("#langWrap").classList.toggle("hidden", !isNews);
    $("#channelWrap").classList.toggle("hidden", !isNews);
  }

  // ---------------------------------------------------------------------------
  // 绑定
  // ---------------------------------------------------------------------------
  document.addEventListener("DOMContentLoaded", function () {
    ensureBase();
    syncTypeUI();
    renderLibrary();

    $("#saveUrlBtn").onclick = function () {
      var v = $("#serviceUrl").value.trim().replace(/\/+$/, "");
      if (!v) { window.alert("服务地址不能为空"); return; }
      if (!/^https?:\/\//i.test(v)) v = "http://" + v;
      localStorage.setItem(URL_KEY, v);
      $("#urlStatus").textContent = "已保存：" + v;
    };
    $("#testUrlBtn").onclick = testConnection;
    $("#runBtn").onclick = runRadar;
    $("#metaBtn").onclick = addByUrl;
    $("#exportBtn").onclick = exportJson;
    $("#importBtn").onclick = function () { $("#importFile").click(); };
    $("#importFile").onchange = function (e) {
      var f = e.target.files && e.target.files[0];
      if (f) importJson(f);
      e.target.value = "";
    };
    $("#fType").onchange = syncTypeUI;
  });
})();
