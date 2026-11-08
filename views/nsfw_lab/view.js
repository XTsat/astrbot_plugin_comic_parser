// 封面打码 / 评分实验台的页面逻辑。通信只走 bridge，禁止裸 fetch。
(() => {
  "use strict";

  const el = (id) => document.getElementById(id);
  // 视图跑在沙箱 iframe 里，拿不到 Dashboard 的会话与 token：只能用 bridge，禁止裸 fetch
  const bridge = window.AstrBotPluginView || window.AstrBotPluginPage;

  // bridge 把 endpoint 拼成 /api/v1/plugins/extensions/<插件名>/<endpoint>，而后端注册的路由是
  // /<插件名>/nsfw_lab/<action>——所以这里必须带上 nsfw_lab 这一段，少一层就是「未找到该路由」
  const EP = {
    config: "nsfw_lab/config",
    upload: "nsfw_lab/upload",
    score: "nsfw_lab/score",
    censor: "nsfw_lab/censor",
    download: "nsfw_lab/download",
    prepare: "nsfw_lab/model/prepare",
    status: "nsfw_lab/model/status",
  };

  const api = {
    get: (endpoint, params) => bridge.apiGet(endpoint, params),
    post: (endpoint, body) => bridge.apiPost(endpoint, body),
  };

  /** 页面没在对的入口打开、或后端不可用：直接说清楚，别让用户对着一堆空控件猜。 */
  function fatal(message) {
    el("rtBadge").textContent = message;
    el("rtBadge").className = "badge bad";
    el("modelsBadge").textContent = "";
    toast(message, true);
  }

  const state = {
    config: null,
    items: new Map(),
    currentId: null,
    style: "mosaic",
    presets: [],
    preset: "",
    modelPoll: 0,
    imgsz: 960,
    showBoxes: true,
    peeking: false,
  };

  // ---- 小工具 ----

  function toast(message, isError) {
    const node = document.createElement("div");
    node.textContent = message;
    if (isError) node.className = "err";
    el("toast").appendChild(node);
    setTimeout(() => node.remove(), isError ? 5200 : 2600);
  }

  function debounce(fn, wait) {
    let timer = 0;
    return (...args) => {
      clearTimeout(timer);
      timer = setTimeout(() => fn(...args), wait);
    };
  }

  function scoreClass(score) {
    if (score == null) return "";
    if (score < 5) return "ok";
    if (score < 15) return "warn";
    return "bad";
  }

  function escapeHtml(text) {
    return String(text).replace(/[&<>"']/g, (ch) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    })[ch]);
  }

  function suffixOf(name, type) {
    const dot = name.lastIndexOf(".");
    if (dot > 0 && name.length - dot <= 6) return name.slice(dot).toLowerCase();
    if ((type || "").includes("png")) return ".png";
    if ((type || "").includes("webp")) return ".webp";
    return ".jpg";
  }

  const current = () => (state.currentId ? state.items.get(state.currentId) : null);

  // ---- 初始化 ----

  async function boot() {
    if (!bridge) {
      fatal("请从 AstrBot 插件详情页打开本视图（直接打开文件拿不到 bridge）");
      return;
    }

    // ready 只等首帧 context：等不到也继续（上下文没有 isDark 时按系统偏好切主题）
    try {
      await bridge.ready();
    } catch {
      /* 忽略：主题与语言都有兜底 */
    }
    applyContext(bridge.getContext());
    const unsubscribe = bridge.onContext(applyContext);
    window.addEventListener("beforeunload", () => {
      if (typeof unsubscribe === "function") unsubscribe();
    });

    try {
      state.config = await api.get(EP.config);
    } catch (err) {
      fatal(`读不到配置：${err.message}`);
      return;
    }

    const cfg = state.config;
    el("rtBadge").textContent = cfg.runtime_ready ? "onnxruntime 就绪" : "onnxruntime 缺失";
    el("rtBadge").className = "badge " + (cfg.runtime_ready ? "ok" : "bad");
    el("modelsBadge").textContent = `${cfg.presets.length} 个权重可用`;

    renderPresets(cfg.presets, cfg.defaults.preset);

    el("threshold").value = cfg.defaults.threshold;
    el("thresholdNum").value = cfg.defaults.threshold;
    // 输入尺寸始终可改；权重是固定尺寸导出件时，后端会按权重的边长跑，并在评分结果里回
    // imgsz_ignored，页面据此提示「你选的值没被采用」
    const choices = cfg.imgsz_choices || [640, 960, 1280];
    el("imgsz").innerHTML = choices
      .map((value) => '<option value="' + value + '">' + value + "</option>")
      .join("");
    el("imgsz").value = String(cfg.imgsz_fixed || cfg.defaults.imgsz);
    state.imgsz = Number(el("imgsz").value);
    el("block").value = cfg.defaults.block;
    el("padding").value = cfg.defaults.padding;
    el("fullArea").value = cfg.defaults.full_area;
    state.style = cfg.defaults.style;

    el("styleSeg").innerHTML = cfg.styles
      .map((s) => `<button data-style="${s.value}"${s.value === state.style ? ' class="on"' : ""}>${s.label}</button>`)
      .join("");

    syncSliderLabels();
    bindEvents();
    render();
  }

  /** 主题与文案跟着 Dashboard 走：切暗色/换语言都会再走一遍。 */
  function applyContext(context) {
    const dark = context && typeof context.isDark === "boolean"
      ? context.isDark
      : window.matchMedia("(prefers-color-scheme: dark)").matches;
    document.documentElement.setAttribute("data-theme", dark ? "dark" : "light");
    el("title").textContent = bridge.t("views.nsfw_lab.title", "封面打码 / 评分实验台");
  }

  function bindEvents() {
    el("drop").addEventListener("click", () => el("file").click());
    el("placeholder").addEventListener("click", () => el("file").click());
    el("file").addEventListener("change", (event) => {
      addFiles(event.target.files);
      event.target.value = "";
    });

    ["dragenter", "dragover"].forEach((name) =>
      document.addEventListener(name, (event) => {
        event.preventDefault();
        el("drop").classList.add("hot");
      })
    );
    ["dragleave", "drop"].forEach((name) =>
      document.addEventListener(name, (event) => {
        event.preventDefault();
        el("drop").classList.remove("hot");
        if (name === "drop" && event.dataTransfer) addFiles(event.dataTransfer.files);
      })
    );

    document.addEventListener("paste", (event) => {
      const files = Array.from((event.clipboardData && event.clipboardData.files) || []);
      if (files.length) addFiles(files);
    });

    el("preset").addEventListener("change", () => {
      state.preset = el("preset").value;
      syncModelUi();
      const item = selectedPreset();
      if (!item || !item.available) return;  // 没下载的档位先点「下载」
      invalidateScores();
      requestScore(state.currentId, true);
    });
    el("btnModel").addEventListener("click", downloadModel);
    if (el("imgsz")) {
      el("imgsz").addEventListener("change", () => {
        state.imgsz = Number(el("imgsz").value);
        invalidateScores();
        requestScore(state.currentId, true);
      });
    }

    const onThreshold = debounce((value) => {
      el("threshold").value = value;
      el("thresholdNum").value = value;
      invalidateScores();
      requestScore(state.currentId, true);
    }, 350);
    el("threshold").addEventListener("input", (e) => onThreshold(e.target.value));
    el("thresholdNum").addEventListener("change", (e) => {
      const value = Math.min(0.5, Math.max(0.005, Number(e.target.value) || 0.03));
      onThreshold(value);
    });

    el("styleSeg").addEventListener("click", (event) => {
      const button = event.target.closest("button[data-style]");
      if (!button) return;
      state.style = button.dataset.style;
      [...el("styleSeg").children].forEach((node) => node.classList.toggle("on", node === button));
      recensor();
    });

    const onCensorParam = debounce(() => { syncSliderLabels(); recensor(); }, 200);
    el("block").addEventListener("input", onCensorParam);
    el("padding").addEventListener("input", onCensorParam);
    el("fullArea").addEventListener("input", onCensorParam);

    el("showBoxes").addEventListener("change", (event) => {
      state.showBoxes = event.target.checked;
      paint();
    });

    const peek = el("btnPeek");
    const startPeek = () => { state.peeking = true; paint(); };
    const stopPeek = () => { state.peeking = false; paint(); };
    peek.addEventListener("pointerdown", startPeek);
    peek.addEventListener("pointerup", stopPeek);
    peek.addEventListener("pointerleave", stopPeek);

    el("btnCensor").addEventListener("click", () => recensor(true));
    el("btnDownload").addEventListener("click", download);
    el("btnBatch").addEventListener("click", batchScore);
    el("btnClear").addEventListener("click", clearAll);
  }

  /** 档位下拉：把「有 nano / small / int8 可换」摆出来，未下载的标一下并给下载入口。 */
  function renderPresets(presets, preferred) {
    state.presets = presets || [];
    el("preset").innerHTML = state.presets
      .map((item) => {
        const suffix = item.available ? "" : " · 未下载";
        return `<option value="${item.value}">${item.label}${suffix}</option>`;
      })
      .join("");

    const fallback = (state.presets.find((item) => item.available) || state.presets[0] || {}).value;
    const wanted = state.presets.some((item) => item.value === preferred && item.available)
      ? preferred
      : state.presets.some((item) => item.value === state.preset && item.available)
        ? state.preset
        : fallback;
    if (wanted) el("preset").value = wanted;
    state.preset = el("preset").value;
    syncModelUi();
  }

  function selectedPreset() {
    return state.presets.find((item) => item.value === el("preset").value) || null;
  }

  /** 未下载的档位显示「下载」按钮；已就位的清掉提示。 */
  function syncModelUi() {
    const item = selectedPreset();
    const btn = el("btnModel");
    if (!item) {
      btn.hidden = true;
      el("modelHint").textContent = "";
      return;
    }
    if (item.available) {
      btn.hidden = true;
      el("modelHint").textContent = "";
      return;
    }
    btn.hidden = false;
    btn.disabled = false;
    btn.textContent = "下载";
    el("modelHint").className = "hint-inline warn";
    el("modelHint").textContent = "这个档位还没下载";
  }

  /** 下载选中的档位：起后台任务后按秒轮询进度。 */
  async function downloadModel() {
    const item = selectedPreset();
    if (!item) return;
    try {
      await api.post(EP.prepare, { preset: item.value });
    } catch (err) {
      toast(`下载失败：${err.message}`, true);
      el("modelHint").className = "hint-inline warn";
      el("modelHint").textContent = err.message;
      return;
    }

    el("btnModel").disabled = true;
    const preset = item.value;
    const tick = async () => {
      let status = null;
      try {
        status = await api.get(EP.status);
      } catch (err) {
        el("modelHint").textContent = `读进度失败：${err.message}`;
        return;
      }
      if (status.state === "downloading") {
        const percent = Math.round((status.percent || 0) * 100);
        el("btnModel").textContent = `下载中 ${percent}%`;
        el("modelHint").className = "hint-inline";
        el("modelHint").textContent = status.preset === preset ? "" : `正在下载 ${status.preset}`;
        state.modelPoll = setTimeout(tick, 1200);
        return;
      }

      el("btnModel").disabled = false;
      if (status.state === "ready") {
        toast(`档位 ${preset} 已就绪`);
        await reloadConfig(preset);
        return;
      }
      el("modelHint").className = "hint-inline warn";
      el("modelHint").textContent = status.error || "下载失败";
      el("btnModel").textContent = "重试";
    };
    state.modelPoll = setTimeout(tick, 600);
  }

  /** 下载完成后重读配置并把新档位选上、直接开跑。 */
  async function reloadConfig(preset) {
    try {
      state.config = await api.get(EP.config);
    } catch (err) {
      toast(`重读配置失败：${err.message}`, true);
      return;
    }
    renderPresets(state.config.presets, preset);
    if (state.currentId) {
      invalidateScores();
      requestScore(state.currentId, true);
    }
  }

  function syncSliderLabels() {
    el("blockVal").textContent = el("block").value;
    el("paddingVal").textContent = el("padding").value;
    el("fullAreaVal").textContent = Number(el("fullArea").value).toFixed(2);
  }

  // ---- 图片 ----

  async function addFiles(fileList) {
    const files = Array.from(fileList || []).filter((file) => (file.type || "").startsWith("image/"));
    if (!files.length) return;

    for (const file of files) {
      try {
        const suffix = suffixOf(file.name, file.type);
        const info = await bridge.upload(EP.upload, file);
        state.items.set(info.image_id, {
          id: info.image_id,
          name: file.name || `粘贴图片${suffix}`,
          bytes: info.bytes,
          width: info.width,
          height: info.height,
          url: URL.createObjectURL(file),
          score: null,
          coverage: null,
          detections: [],
          failed: false,
          stale: false,
          ms: null,
          threshold: null,
          censored: null,
          censoredChanged: false,
          censoredCoverage: 0,
          censoredFull: false,
          scoring: null,
          needsRescore: false,
          scoreToken: 0,
          censorToken: 0,
        });
        state.currentId = info.image_id;
        render();
        // 上传后立刻评分；select 里不会再重复触发（requestScore 自己去重）
        await requestScore(info.image_id, true);
      } catch (err) {
        toast(`${file.name || "图片"} 上传失败：${err.message}`, true);
      }
    }
  }

  function select(id) {
    state.currentId = id;
    const item = current();
    render();
    if (item && item.score == null && !item.failed) requestScore(id);
  }

  function invalidateScores() {
    state.items.forEach((item) => { if (item.score != null) item.stale = true; });
  }

  function clearAll() {
    state.items.forEach((item) => URL.revokeObjectURL(item.url));
    state.items.clear();
    state.currentId = null;
    render();
  }

  // ---- 评分 ----

  /** 同一张图的评分请求串行化：正在跑就只记一笔「回头再算」，避免重复请求与结果错序。 */
  function requestScore(id, force) {
    const item = id ? state.items.get(id) : null;
    if (!item) return Promise.resolve();
    if (item.scoring) {
      item.needsRescore = item.needsRescore || Boolean(force);
      return item.scoring;
    }
    if (!force && (item.score != null && !item.stale)) return Promise.resolve();

    item.scoring = runScore(item).finally(() => {
      item.scoring = null;
      if (item.needsRescore) {
        item.needsRescore = false;
        requestScore(item.id, true);
      }
    });
    return item.scoring;
  }

  async function runScore(item) {
    const presets = state.presets.find((entry) => entry.value === el("preset").value);
    if (presets && !presets.available) {
      el("modelHint").className = "hint-inline warn";
      el("modelHint").textContent = "这个档位还没下载，点右边的「下载」";
      item.failed = true;
      item.score = null;
      renderList();
      return;
    }

    const token = ++item.scoreToken;
    try {
      const data = await api.post(EP.score, {
        image_id: item.id,
        preset: el("preset").value,
        threshold: Number(el("thresholdNum").value),
        imgsz: state.imgsz,
      });
      if (token !== item.scoreToken) return; // 已有更新的请求在跑，丢掉这次结果

      item.score = data.score;
      item.coverage = data.coverage;
      item.detections = data.detections;
      item.labels = data.labels;
      item.ms = data.cost_ms;
      item.width = data.width;
      item.height = data.height;
      item.threshold = data.threshold;
      item.preset = data.preset;
      item.imgszFixed = data.imgsz_fixed;
      item.imgszIgnored = Boolean(data.imgsz_ignored);
      item.failed = false;
      item.stale = false;
      item.censored = null;
    } catch (err) {
      if (token !== item.scoreToken) return;
      item.score = null;
      item.detections = [];
      item.failed = true;
      toast(`评分失败：${err.message}`, true);
    }

    if (item.id === state.currentId) {
      render();
      recensor();
    } else {
      renderList();
    }
    renderSummary();
  }

  async function batchScore() {
    const pending = [...state.items.values()].filter((item) => item.score == null || item.stale);
    if (!pending.length) {
      toast("没有待评分的图片");
      return;
    }
    for (const item of pending) await requestScore(item.id, true);
    renderSummary();
    toast(`已评分 ${pending.length} 张`);
  }

  // ---- 打码 ----

  async function recensor(manual) {
    const item = current();
    if (!item) return;
    if (item.score == null || item.failed) {
      renderCensorState();
      return;
    }

    const token = ++item.censorToken;
    try {
      const data = await api.post(EP.censor, {
        image_id: item.id,
        boxes: item.detections.map((det) => det.box),
        style: state.style,
        block: Number(el("block").value),
        padding: Number(el("padding").value),
        full_area: Number(el("fullArea").value),
      });
      if (token !== item.censorToken) return; // 参数又变了，这次结果已经过期

      // 没有检出框时后端原样返回，别把「没改动」的图当打码结果，否则预览会白白切走
      item.censored = data.changed ? data.image : null;
      item.censoredChanged = data.changed;
      item.censoredCoverage = data.coverage;
      item.censoredFull = data.full;
      paint();
      renderCensorState();
      if (manual) toast(data.changed ? "已重新打码" : "没有检出框，图未改动");
    } catch (err) {
      if (token !== item.censorToken) return;
      toast(`打码失败：${err.message}`, true);
    }
  }

  async function download() {
    const item = current();
    if (!item || !item.censored) {
      toast("还没有打码结果", true);
      return;
    }
    const base = (item.name || "cover").replace(/\.[^.]+$/, "");
    const ext = item.censored.startsWith("data:image/png") ? ".png" : ".jpg";
    try {
      await bridge.download(EP.download, { image_id: item.id }, `${base}_censored${ext}`);
    } catch (err) {
      toast(`下载失败：${err.message}`, true);
    }
  }

  // ---- 渲染 ----

  function render() {
    renderList();
    paint();
    renderScore();
    renderDetections();
    renderCensorState();
    renderSummary();
  }

  function renderList() {
    el("thumbs").innerHTML = [...state.items.values()]
      .map((item) => {
        let chip = '<span class="chip">待评分</span>';
        if (item.failed) chip = '<span class="chip bad">失败</span>';
        else if (item.score != null) {
          chip = `<span class="chip ${scoreClass(item.score)}">${item.score.toFixed(1)}${item.stale ? "?" : ""}</span>`;
        } else if (item.scoring) chip = '<span class="chip">评分中…</span>';
        const active = item.id === state.currentId ? " active" : "";
        return `<li class="thumb${active}" data-id="${item.id}">
          <img src="${item.url}" alt="" />
          <div class="meta">
            <div class="name" title="${escapeHtml(item.name)}">${escapeHtml(item.name)}</div>
            <div class="sub">${item.width}×${item.height}</div>
          </div>
          ${chip}
        </li>`;
      })
      .join("");

    [...el("thumbs").children].forEach((node) =>
      node.addEventListener("click", () => select(node.dataset.id))
    );
  }

  function renderSummary() {
    const scored = [...state.items.values()].filter((item) => item.score != null && !item.failed);
    if (!scored.length) {
      el("summaryBadge").textContent = "";
      return;
    }
    const avg = scored.reduce((sum, item) => sum + item.score, 0) / scored.length;
    el("summaryBadge").textContent =
      `已评 ${scored.length} 张 · 平均 ${avg.toFixed(1)} · ≥15 分 ${scored.filter((i) => i.score >= 15).length} 张`;
  }

  function paint() {
    const item = current();
    if (!item) {
      el("viewer").hidden = true;
      el("placeholder").hidden = false;
      el("resultHint").hidden = false;
      el("fileName").textContent = "未选择图片";
      el("fileDims").textContent = "";
      return;
    }

    el("viewer").hidden = false;
    el("placeholder").hidden = true;
    el("resultHint").hidden = item.score != null || item.failed;
    el("fileName").textContent = item.name;
    el("fileDims").textContent = `${item.width}×${item.height} · ${(item.bytes / 1024).toFixed(0)}KB`;

    const showCensored = !state.peeking && Boolean(item.censored);
    el("preview").src = showCensored ? item.censored : item.url;
    drawBoxes();
  }

  function drawBoxes() {
    const container = el("boxes");
    container.innerHTML = "";
    const item = current();
    if (!item || !state.showBoxes || !item.width || !item.height) return;

    item.detections.forEach((det) => {
      const [x1, y1, x2, y2] = det.box;
      const box = document.createElement("div");
      box.className = "box";
      box.style.left = `${(x1 / item.width) * 100}%`;
      box.style.top = `${(y1 / item.height) * 100}%`;
      box.style.width = `${((x2 - x1) / item.width) * 100}%`;
      box.style.height = `${((y2 - y1) / item.height) * 100}%`;
      const label = document.createElement("span");
      label.textContent = `${det.label} ${det.confidence.toFixed(2)}`;
      box.appendChild(label);
      container.appendChild(box);
    });
  }

  function renderScore() {
    const item = current();
    const has = item && item.score != null;
    el("scoreValue").innerHTML = has ? `${item.score.toFixed(1)}<small>/100</small>` : "—<small>/100</small>";
    el("scoreValue").style.color = has ? `var(--${scoreClass(item.score) || "text"})` : "var(--text)";
    el("coverage").textContent = has && item.coverage != null ? `${(item.coverage * 100).toFixed(1)}%` : "—";
    el("detCount").textContent = has ? String(item.detections.length) : "—";
    el("costMs").textContent = has && item.ms != null ? `${item.ms.toFixed(0)} ms` : "—";
    el("usedThreshold").textContent = has && item.threshold != null ? String(item.threshold) : "—";
    // 选了别的输入尺寸但权重是固定尺寸时，说清「实际用的是多少」
    const ignored = Boolean(has && item.imgszIgnored);
    if (ignored) {
      el("modelHint").className = "hint-inline warn";
      el("modelHint").textContent = `这份权重固定 ${item.imgszFixed}，你选的输入尺寸没生效`;
    } else if (el("modelHint").classList.contains("warn") && el("modelHint").textContent.includes("输入尺寸")) {
      el("modelHint").className = "hint-inline";
      el("modelHint").textContent = "";
    }
  }

  function renderDetections() {
    const item = current();
    const rows = item && item.detections.length
      ? item.detections
          .slice()
          .sort((a, b) => b.confidence - a.confidence)
          .map((det) => {
            const [x1, y1, x2, y2] = det.box;
            return `<tr><td>${escapeHtml(det.label)}</td><td class="mono">${det.confidence.toFixed(3)}</td>
              <td class="mono">${Math.round(x2 - x1)}×${Math.round(y2 - y1)}</td></tr>`;
          })
          .join("")
      : '<tr><td colspan="3" class="muted">—</td></tr>';
    el("detBody").innerHTML = rows;
  }

  function renderCensorState() {
    const item = current();
    if (!item || !item.censored) {
      el("censorState").textContent = item && item.failed ? "评分失败，无法打码" : "—";
      el("censorCoverage").textContent = "—";
      el("censorHint").textContent = "";
      return;
    }
    el("censorState").textContent = item.censoredFull
      ? "整张打码"
      : item.censoredChanged
        ? `局部打码 ${item.detections.length} 处`
        : "未改动";
    el("censorCoverage").textContent = `${(item.censoredCoverage * 100).toFixed(1)}%`;
    el("censorHint").textContent = item.censoredFull
      ? "检出并集超过了「整张阈值」，于是整张糊掉——阈值调高可以只糊局部。"
      : "预览已换成打码结果；按住「按住看原图」可对比。";
  }

  boot();
})();
