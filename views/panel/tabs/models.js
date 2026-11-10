// 模型下载页：把全部权重列清楚（类别 / 大小 / 来源 / 许可 / 装没装），在这里下载与删除。
// 接口：GET panel/models · POST panel/models/download · GET panel/models/status · POST panel/models/delete
export default {
  id: "models",
  title: "模型下载",

  mount(root, ctx) {
    const { el, api, toast, escapeHtml, fmtBytes, EP } = ctx;

    // 沙箱里没有 allow-modals（confirm 会静默返回 false），删除用「再点一次确认」
    let pendingDelete = "";

    const state = {
      mode: "auto",
      customUrl: "",
      uploadFile: null,
      models: [],
      modelsDir: "",
      download: { state: "idle", preset: "", percent: 0, error: "" },
      poll: 0,
      busy: false,
    };

    root.innerHTML = `
      <div class="page">
        <div class="page-head">
          <div>
            <h2>模型下载</h2>
            <p class="hint">权重不随插件分发，按需下载到 <code class="mono" id="modelsDir">models/</code>。下载源按档位走各自上游，失败自动换源；配置里的「自定义下载地址」「代理」在这里同样生效。实测同一批漫画封面：二次元检测与二次元分割都检出 10/11 张，写实档 6/11，所以下面的顺序就是推荐顺序。</p>
          </div>
          <div class="row-actions">
            <span id="mRuntime" class="badge">—</span>
            <button id="mRefresh" class="ghost">刷新</button>
          </div>
        </div>

        <div class="stack">
        <div class="card" id="downloadCard" hidden>
          <h2>正在下载</h2>
          <div class="kv"><span id="downloadName">—</span><span class="mono" id="downloadPercent">0%</span></div>
          <div class="progress"><i id="downloadBar"></i></div>
          <div class="hint" id="downloadHint"></div>
        </div>

        <div class="card">
          <div class="card-head"><h2>下载线路</h2></div>
          <div class="bar">
            <div class="field">
              <label for="mMode">线路</label>
              <select id="mMode">
                <option value="auto">自动（按档位默认源）</option>
                <option value="mirror">只走镜像（国内直连不通时）</option>
                <option value="direct">只走上游源站</option>
                <option value="custom">自定义地址</option>
              </select>
              <span id="mModeHint" class="hint-inline"></span>
            </div>
          </div>
          <div class="bar" id="mCustomField" hidden>
            <div class="field grow">
              <label for="mCustomUrl">自定义地址</label>
              <input type="text" id="mCustomUrl" placeholder="https://镜像地址/{filename}，可多行" />
            </div>
          </div>
        </div>

        <div class="card">
          <div class="card-head"><h2>手动上传权重</h2></div>
          <p class="legend">连镜像都通不了、或手里已有自己导出的 ONNX 时走这里。档位决定类别顺序，装错档位会把部位打错码。</p>
          <div class="bar">
            <div class="field">
              <label for="mUploadPreset">安装为</label>
              <select id="mUploadPreset"></select>
            </div>
            <input type="file" id="mUploadFile" accept=".onnx" hidden />
            <button id="mUploadPick">选择 .onnx 文件</button>
            <button id="mUploadGo" class="primary" disabled>上传并安装</button>
            <span id="mUploadState" class="hint-inline"></span>
          </div>
        </div>

        </div>

        <div id="modelSections" class="stack"></div>
      </div>`;

    const sectionsRoot = root.querySelector("#modelSections");

    // 家族顺序 = 推荐顺序：越靠前越适合漫画封面
    const FAMILIES = [
      {
        key: "anime",
        title: "二次元检测（推荐）",
        intro:
          "纯二次元 / 同人图训练，漫画封面上召回最高、体积最小、速度最快；短板是没有 make_love 这类场景类别，性交构图会漏。",
        badge: "漫画封面首选",
      },
      {
        key: "anime_seg",
        title: "二次元分割",
        intro:
          "按部位分割，框最贴边（局部打码时周围画面保得住），带 anus / vagina 类别；female face / male face 类别不会被打码。原生 640 / 1280 输入，medium 更准但慢。",
        badge: "框最贴边",
      },
      {
        key: "erax",
        title: "写实（EraX）",
        intro:
          "真人照片向：覆盖 anus / make_love 等场景，漫画封面上召回偏低；已经把阈值降到 0.02 仍漏检时，优先换上面的二次元档。",
        badge: "真人 / 写实图",
      },
    ];

    function renderRuntime(describe) {
      const runtime = (describe && describe.runtime) || {};
      const badge = el("mRuntime");
      if (runtime.ready) {
        badge.textContent = `onnxruntime ${runtime.version || "就绪"}`;
        badge.className = "tag ok";
      } else {
        badge.textContent = runtime.error || "onnxruntime 未就绪";
        badge.className = "tag warn";
      }
    }

    function renderDownloadCard() {
      const card = el("downloadCard");
      const item = state.models.find((entry) => entry.value === state.download.preset);
      const active = state.download.state === "downloading";
      card.hidden = !active && !state.download.error;
      el("downloadName").textContent = item
        ? `${item.file || item.value} · ${item.label || ""}`
        : state.download.preset || "—";
      const percent = Math.round((state.download.percent || 0) * 100);
      el("downloadPercent").textContent = `${percent}%`;
      el("downloadBar").style.width = `${percent}%`;
      el("downloadHint").textContent = state.download.error
        ? state.download.error
        : "几十 MB 的文件，慢的话几分钟；离开这一页也会继续下。";
      el("downloadHint").className = state.download.error ? "hint warn" : "hint";
    }

    // 一个权重一条「条目」而不是表格行：文件名、类别、许可、说明全是长短不一的自由文本，
    // 塞进七列表格里每一列都会被挤到换行，条目式排布才能让长文本自然折行、按钮不被挤压。
    function rowFor(item) {
      const size = item.installed ? fmtBytes(item.bytes) : `约 ${fmtBytes(item.approx_bytes)}`;
      const kind = item.task === "segment" ? "分割" : "检测";
      const labels = item.labels.map((label) => `<span class="tag">${escapeHtml(label)}</span>`).join("");
      const status = item.installed
        ? `<span class="tag ok">已安装</span>${item.current ? '<span class="tag">当前档位</span>' : ""}`
        : item.downloading
          ? '<span class="tag warn">下载中</span>'
          : '<span class="tag warn">未安装</span>';
      // 右边只有一个按钮位：没装是蓝色的「下载」，装了是红色的「删除」。
      // 位置固定，两种状态不会让整行布局跳动。
      const actions = item.installed
        ? `<button class="danger-solid" data-act="delete" data-preset="${item.value}">${
            pendingDelete === item.value ? "确认删除" : "删除"
          }</button>`
        : `<button class="primary" data-act="download" data-preset="${item.value}"${
            state.download.state === "downloading" ? " disabled" : ""
          }>下载</button>`;
      return `<div class="model-item">
        <div class="model-main">
          <div class="model-title">
            <b class="mono">${escapeHtml(item.file)}</b>
            ${status}
            <span class="tag">${kind} · <span class="mono">${size}</span></span>
          </div>
          <div class="muted-line">${escapeHtml(item.label)}</div>
          <div class="tag-row"><span class="tag-row-label">类别</span>${labels}</div>
          <div class="muted-line">${escapeHtml(item.note || "")}</div>
          <div class="muted-line">许可：${escapeHtml(item.license || "未标注")}${
            item.upstream
              ? ` · 上游 <span class="url">${escapeHtml(item.upstream)}</span>`
              : ""
          }</div>
        </div>
        <div class="model-side"><div class="row-actions">${actions}</div></div>
      </div>`;
    }

    function renderSections() {
      if (!state.models.length) {
        sectionsRoot.innerHTML = `<div class="card"><div class="hint">没有读到模型清单</div></div>`;
        return;
      }
      const known = new Set(FAMILIES.map((family) => family.key));
      const sections = FAMILIES.map((family) => {
        const items = state.models.filter((item) => (item.family || "") === family.key);
        if (!items.length) return "";
        const installedCount = items.filter((item) => item.installed).length;
        return `<div class="card">
          <div class="card-head">
            <h2>${escapeHtml(family.title)}</h2>
            <span class="tag ok">${escapeHtml(family.badge)}</span>
            <span class="muted-line" style="margin-left:auto">${installedCount}/${items.length} 已安装</span>
          </div>
          <p class="legend">${escapeHtml(family.intro)}</p>
          <div class="model-list">${items.map(rowFor).join("")}</div>
        </div>`;
      });
      // 兜底：万一后端来了个前端还不认识的家族，也照原样列出来，别静默吞掉
      const unknown = state.models.filter((item) => !known.has(item.family || ""));
      if (unknown.length) {
        sections.push(`<div class="card"><div class="card-head"><h2>其它</h2></div>
          <div class="model-list">${unknown.map(rowFor).join("")}</div></div>`);
      }
      sectionsRoot.innerHTML = sections.join("");
    }

    function renderAll() {
      el("modelsDir").textContent = state.modelsDir ? `${state.modelsDir}` : "models/";
      renderSections();
      renderDownloadCard();
      renderUploadPresets();
      syncModeUi();
    }

    async function load() {
      const data = await api.get(EP.models);
      state.models = data.models || [];
      state.modelsDir = data.models_dir || "";
      const download = data.download || {};
      state.download = {
        state: download.state || "idle",
        preset: download.preset || "",
        percent: download.percent || 0,
        error: download.error || "",
      };
      if (download.models) state.models = download.models;
      renderAll();
      if (state.download.state === "downloading") schedulePoll();
    }

    function schedulePoll() {
      clearTimeout(state.poll);
      state.poll = setTimeout(poll, 1200);
    }

    async function poll() {
      try {
        const status = await api.get(EP.modelsStatus);
        state.download = {
          state: status.state || "idle",
          preset: status.preset || "",
          percent: status.percent || 0,
          error: status.error || "",
        };
        if (status.models) state.models = status.models;
        renderAll();
        if (state.download.state === "downloading") {
          schedulePoll();
          return;
        }
        if (state.download.state === "ready") {
          toast("权重已就绪，去「打码测试」页就能选它了");
          await load();
          return;
        }
        if (state.download.error) toast(`下载失败：${state.download.error}`, true);
      } catch (err) {
        el("downloadHint").textContent = `读进度失败：${err.message}`;
        schedulePoll();
      }
    }

    async function startDownload(preset) {
      try {
        const data = await api.post(EP.modelsDownload, {
          preset,
          mode: state.mode,
          custom_url: state.customUrl,
        });
        state.download = {
          state: data.state || "downloading",
          preset: data.preset || preset,
          percent: data.percent || 0,
          error: data.error || "",
        };
        renderAll();
        schedulePoll();
      } catch (err) {
        toast(`下载失败：${err.message}`, true);
      }
    }

    async function removeModel(preset) {
      if (pendingDelete !== preset) {
        pendingDelete = preset;
        renderSections();
        toast("再点一次「确认删除」就删掉这份权重");
        return;
      }
      pendingDelete = "";
      try {
        const data = await api.post(EP.modelsDelete, { preset });
        toast(data.removed ? "已删除" : "这个档位本来就没下载");
        await load();
      } catch (err) {
        toast(`删除失败：${err.message}`, true);
      }
    }

    function syncModeUi() {
      const hint = el("mModeHint");
      el("mCustomField").hidden = state.mode !== "custom";
      const text = {
        auto: "各档默认源：二次元档先走 HF 镜像，写实档走插件 Release + 加速镜像",
        mirror: "只用镜像域名（hf-mirror / ghfast / gh-proxy）",
        direct: "只用上游源站（huggingface.co / github.com）",
        custom: "这一行地址只对本次下载生效，不影响插件配置",
      }[state.mode] || "";
      hint.textContent = text;
      hint.className = "hint-inline";
    }

    el("mMode").addEventListener("change", () => {
      state.mode = el("mMode").value;
      if (state.mode === "custom") el("mCustomUrl").focus();
      syncModeUi();
    });
    el("mCustomUrl").addEventListener("input", () => {
      state.customUrl = el("mCustomUrl").value;
    });

    // ---- 手动上传安装 ----
    function renderUploadPresets() {
      el("mUploadPreset").innerHTML = state.models
        .map(
          (item) =>
            `<option value="${item.value}">${escapeHtml(item.label)}${
              item.installed ? "（已安装，会被覆盖）" : ""
            }</option>`,
        )
        .join("");
    }

    el("mUploadPick").addEventListener("click", () => el("mUploadFile").click());
    el("mUploadFile").addEventListener("change", (event) => {
      const file = (event.target.files || [])[0] || null;
      state.uploadFile = file;
      el("mUploadGo").disabled = !file;
      el("mUploadState").textContent = file ? `${file.name} · ${fmtBytes(file.size)}` : "";
      el("mUploadState").className = "hint-inline";
      event.target.value = "";
    });

    el("mUploadGo").addEventListener("click", async () => {
      const file = state.uploadFile;
      if (!file) return;
      el("mUploadGo").disabled = true;
      el("mUploadState").textContent = "上传中…";
      try {
        const staged = await ctx.api.upload(EP.modelsUpload, file);
        const preset = el("mUploadPreset").value;
        const done = await api.post(EP.modelsInstall, {
          upload_id: staged.upload_id,
          preset,
        });
        toast(`已安装为 ${done.file}`);
        state.uploadFile = null;
        el("mUploadState").textContent = "";
        await load();
      } catch (err) {
        el("mUploadState").textContent = `失败：${err.message}`;
        el("mUploadState").className = "hint-inline warn";
        toast(`安装失败：${err.message}`, true);
      } finally {
        el("mUploadGo").disabled = !state.uploadFile;
      }
    });

    root.querySelector("#mRefresh").addEventListener("click", () => {
      load().catch((err) => toast(`刷新失败：${err.message}`, true));
    });

    sectionsRoot.addEventListener("click", (event) => {
      const button = event.target.closest("button[data-act]");
      if (!button) return;
      const preset = button.dataset.preset;
      const act = button.dataset.act;
      if (act === "download") startDownload(preset);
      else if (act === "delete") removeModel(preset);
    });

    ctx.describe()
      .then(renderRuntime)
      .catch(() => {
        /* 顶栏已经报过错了，这里不重复打扰 */
      });
    load().catch((err) => {
      sectionsRoot.innerHTML = `<div class="card"><div class="hint warn">读不到模型清单：${escapeHtml(err.message)}</div></div>`;
    });
  },
};
