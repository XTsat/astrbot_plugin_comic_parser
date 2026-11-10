// 打码测试页：把一张图（上传、粘贴或从渠道拉来的封面）丢进插件自己的检测器，
// 看露骨指数、检出框和糊出来的样子。通信只走 bridge（ctx 里带），禁止裸 fetch。
export default {
  id: "test",
  title: "打码测试",
  mount(root, ctx) {
  "use strict";

  const el = (id) => root.querySelector("#" + id);
  const self = this;
  self._ctx = ctx;
  const bridge = ctx.bridge;
  const api = ctx.api;
  const EP = ctx.EP;
  const toast = ctx.toast;
  const escapeHtml = ctx.escapeHtml;
  const fmtBytes = ctx.fmtBytes;

  /** 这一页自己的兜底提示：顶栏状态由 app.js 管，这里只弹提示。 */
  function fatal(message) {
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
    try {
      state.config = await ctx.describe();
    } catch (err) {
      fatal(`读不到配置：${err.message}`);
      return;
    }

    const cfg = state.config;
    renderPresets(cfg.installed || [], cfg.defaults.preset);
    renderCoverChannels(cfg.channels || []);

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
    el("btnCover").addEventListener("click", pullCover);
    el("coverText").addEventListener("keydown", (event) => {
      if (event.key === "Enter") pullCover();
    });
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

  /** 档位下拉只列**已安装**的权重：没装的在「模型下载」页里下，免得选了跑不起来。 */
  function renderPresets(presets, preferred) {
    state.presets = (presets || []).filter((item) => item.installed);
    // 下拉 = 权重名（去掉 .onnx）+ 段位 + 体积：文件名是用户认得的，段位和大小才是区分点
    const stem = (item) => String(item.file || item.value).replace(/\.onnx$/i, "");
    const shortLabel = (item) => String(item.label || "").split("（")[0].trim();
    // 体积按整 MB 报，跟设置页下拉里的写法一致（11.5 MB → 12MB）
    const sizeOf = (item) => {
      const mb = Math.round((item.bytes || item.approx_bytes || 0) / 1048576);
      return mb ? `${mb}MB` : "";
    };
    el("preset").innerHTML = state.presets
      .map((item) => {
        const parts = [stem(item), shortLabel(item), sizeOf(item)].filter(Boolean);
        return `<option value="${item.value}">${escapeHtml(parts.join(" · "))}</option>`;
      })
      .join("");

    if (!state.presets.length) {
      state.preset = "";
      syncModelUi();
      return;
    }
    const wanted = state.presets.some((item) => item.value === preferred)
      ? preferred
      : state.presets.some((item) => item.value === state.preset)
        ? state.preset
        : state.presets[0].value;
    el("preset").value = wanted;
    state.preset = el("preset").value;
    syncModelUi();
  }

  /** 把别处送来的图（渠道页的封面、拉封面接口返回的封面）放进列表并立刻评分。 */
  function pushExternalImage(payload) {
    if (!payload || !payload.image_id) return;
    state.items.set(payload.image_id, {
      id: payload.image_id,
      name: payload.name || "渠道封面",
      bytes: payload.bytes || 0,
      width: payload.width,
      height: payload.height,
      url: payload.data_url,
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
    state.currentId = payload.image_id;
    renderList();
    render();
    requestScore(state.currentId, true);
  }
  self.pushExternalImage = pushExternalImage;

  /** 渠道下拉：有封面预览能力的渠道优先排前面。 */
  function renderCoverChannels(channels) {
    const usable = (channels || [])
      .filter((item) => item.enabled)
      .sort((a, b) => Number(Boolean(b.supports_cover)) - Number(Boolean(a.supports_cover)));
    el("coverChannel").innerHTML = usable.length
      ? usable
          .map(
            (item) =>
              `<option value="${item.command}"${item.supports_cover ? "" : " disabled"}>${
                item.display_command || item.command
              }${item.supports_cover ? "" : "（不支持封面预览）"}</option>`,
          )
          .join("")
      : '<option value="">没有启用中的渠道</option>';
    const first = usable.find((item) => item.supports_cover);
    if (first) el("coverChannel").value = first.command;
    el("btnCover").disabled = !first;
    if (!first) el("coverText").placeholder = "当前没有支持封面预览的渠道，可以直接上传图片";
  }

  /** 从漫画渠道按 ID / 链接把封面拉进来，省得先去下一本。 */
  async function pullCover() {
    const channel = el("coverChannel").value;
    const text = el("coverText").value.trim();
    if (!channel) {
      toast("先在左边选一个渠道；一个可用的都没有就先去设置里启用渠道", true);
      return;
    }
    if (!text) {
      toast("再填一下作品 ID 或作品链接", true);
      return;
    }
    const button = el("btnCover");
    button.disabled = true;
    button.textContent = "拉取中…";
    try {
      const data = await api.post(EP.channelsFetch, { channel, text });
      if (!data || !data.image) {
        toast(
          (data && data.cover_error) || `${channel} 没取到封面：可能被站点风控或网络不通，详情看插件日志`,
          true,
        );
        return;
      }
      pushExternalImage({
        image_id: data.image.image_id,
        data_url: data.image.data_url,
        width: data.image.width,
        height: data.image.height,
        bytes: data.image.bytes,
        name: `${data.comic.title || data.comic.comic_id} · 封面`,
      });
      toast(`已拉取「${data.comic.title || data.comic.comic_id}」的封面`);
    } catch (err) {
      toast(`拉取失败：${err.message}`, true);
    } finally {
      button.disabled = false;
      button.textContent = "拉取";
    }
  }

  function selectedPreset() {
    return state.presets.find((item) => item.value === el("preset").value) || null;
  }

  /** 一个权重都没装时明确指向「模型下载」页，而不是让下拉空着。 */
  function syncModelUi() {
    const item = selectedPreset();
    const hint = el("modelHint");
    if (item) {
      hint.className = "hint-inline";
      hint.textContent = `${item.task === "segment" ? "分割" : "检测"} · ${item.labels.length} 类`;
      return;
    }
    hint.className = "hint-inline warn";
    hint.textContent = "还没有已安装的模型，先去「模型下载」页装一个";
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

  /** 删掉一张：页面里移除，后端那份暂存也顺手释放（释放失败无所谓，超出上限会自己挤掉）。 */
  function removeItem(id) {
    const item = state.items.get(id);
    if (!item) return;
    if (item.url && item.url.startsWith("blob:")) URL.revokeObjectURL(item.url);
    state.items.delete(id);
    api.post(EP.imageDelete, { image_id: id }).catch(() => {});
    if (state.currentId === id) {
      const next = state.items.keys().next();
      state.currentId = next.done ? null : next.value;
    }
    render();
    const active = current();
    if (active && active.score == null && !active.failed) requestScore(active.id);
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
    if (!state.items.size) {
      el("thumbs").innerHTML = '<li class="list-empty">还没有图片<br />拖一张进来，或用上面的渠道拉一张封面</li>';
      el("thumbCount").textContent = "还没有图片";
      return;
    }
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
          <button class="del" type="button" title="从列表移除这张">×</button>
        </li>`;
      })
      .join("");

    // 列头计数跟列表一起刷：图多了以后「现在有几张、已评几张」比列表本身更常被看
    const total = state.items.size;
    const scored = [...state.items.values()].filter((item) => item.score != null && !item.failed).length;
    el("thumbCount").textContent = total ? `${total} 张 · 已评 ${scored} 张` : "还没有图片";

    [...el("thumbs").children].forEach((node) => {
      node.addEventListener("click", () => select(node.dataset.id));
      const del = node.querySelector(".del");
      if (del) {
        del.addEventListener("click", (event) => {
          // 删除按钮在列表项里面，不拦住就会顺带把这张图选中
          event.stopPropagation();
          removeItem(node.dataset.id);
        });
      }
    });
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
      el("btnPeek").hidden = true;
      el("fileName").textContent = "未选择图片";
      el("fileDims").textContent = "";
      return;
    }

    el("viewer").hidden = false;
    el("placeholder").hidden = true;
    el("btnPeek").hidden = false;
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
  },

  /** 每次切到本页都会被调用：把「渠道页送来的封面」接住并评分。 */
  activate() {
    const ctx = this._ctx;
    if (!ctx || !ctx.shared.pendingImage || typeof this.pushExternalImage !== "function") return;
    const pending = ctx.shared.pendingImage;
    ctx.shared.pendingImage = null;
    this.pushExternalImage(pending);
  },
};
