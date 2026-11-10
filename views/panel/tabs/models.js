// 模型下载页：把全部权重列清楚（类别 / 大小 / 来源 / 许可 / 装没装），在这里下载与删除。
// 接口：GET panel/models · POST panel/models/download · GET panel/models/status · POST panel/models/delete
export default {
  id: "models",
  title: "模型下载",

  mount(root, ctx) {
    const { el, api, toast, escapeHtml, fmtBytes, EP, go } = ctx;

    // 沙箱里没有 allow-modals（confirm 会静默返回 false），删除用「再点一次确认」
    let pendingDelete = "";

    const state = {
      models: [],
      modelsDir: "",
      download: { state: "idle", preset: "", percent: 0, error: "" },
      poll: 0,
      busy: false,
    };

    root.innerHTML = `
      <div class="pane-grid">
        <div class="card">
          <h2>模型下载</h2>
          <div class="hint">
            权重不随插件分发，按需下载到插件的 <code class="mono" id="modelsDir">models/</code> 目录。
            下载源按档位走各自上游（写实档走插件 Release，二次元档走 HuggingFace 镜像），失败会自动换源；
            插件配置里的「自定义下载地址」「代理」在这里同样生效。
          </div>
          <div class="row-actions" style="margin-top:10px">
            <button id="mRefresh">刷新</button>
            <span id="mRuntime" class="badge">—</span>
          </div>
        </div>

        <div class="card" id="downloadCard" hidden>
          <h2>正在下载</h2>
          <div class="kv"><span id="downloadName">—</span><span class="mono" id="downloadPercent">0%</span></div>
          <div class="progress"><i id="downloadBar"></i></div>
          <div class="hint" id="downloadHint"></div>
        </div>

        <div class="card">
          <h2>全部模型</h2>
          <div class="hint">「打码测试」页的模型下拉只列已安装的；要换掉插件当前使用的档位，去「设置」页改「模型档位」。</div>
          <table class="data-table" id="modelTable">
            <thead>
              <tr>
                <th>模型</th><th>类型</th><th>类别</th><th>大小</th>
                <th>来源 / 许可</th><th>状态</th><th>操作</th>
              </tr>
            </thead>
            <tbody></tbody>
          </table>
        </div>
      </div>`;

    const tableBody = root.querySelector("#modelTable tbody");

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
      el("downloadName").textContent = item ? item.label : state.download.preset || "—";
      const percent = Math.round((state.download.percent || 0) * 100);
      el("downloadPercent").textContent = `${percent}%`;
      el("downloadBar").style.width = `${percent}%`;
      el("downloadHint").textContent = state.download.error
        ? state.download.error
        : "几十 MB 的文件，慢的话几分钟；离开这一页也会继续下。";
      el("downloadHint").className = state.download.error ? "hint warn" : "hint";
    }

    function renderRows() {
      if (!state.models.length) {
        tableBody.innerHTML = `<tr><td colspan="7" class="muted">没有读到模型清单</td></tr>`;
        return;
      }
      const families = [
        ["erax", "写实（EraX）"],
        ["anime", "二次元检测（deepghs）"],
        ["anime_seg", "二次元分割（01miku）"],
      ];
      const rows = [];
      for (const [family, title] of families) {
        const group = state.models.filter((item) => (item.family || "") === family);
        if (!group.length) continue;
        rows.push(
          `<tr><td colspan="7" style="background:var(--panel-2);font-weight:600">${escapeHtml(title)}</td></tr>`,
        );
        for (const item of group) {
          const size = item.installed ? fmtBytes(item.bytes) : `约 ${fmtBytes(item.approx_bytes)}`;
          const kind = item.task === "segment" ? "分割" : "检测";
          const labels = item.labels.map((label) => `<span class="tag">${escapeHtml(label)}</span>`).join("");
          const status = item.installed
            ? `<span class="tag ok">已安装</span>${item.current ? '<span class="tag">当前档位</span>' : ""}`
            : item.downloading
              ? '<span class="tag warn">下载中</span>'
              : '<span class="tag warn">未安装</span>';
          const actions = item.installed
            ? `<button data-act="delete" data-preset="${item.value}"${
                pendingDelete === item.value ? ' class="danger"' : ""
              }>${pendingDelete === item.value ? "确认删除" : "删除"}</button>
               <button data-act="test" data-preset="${item.value}">去测试</button>`
            : `<button class="primary" data-act="download" data-preset="${item.value}"${
                state.download.state === "downloading" ? " disabled" : ""
              }>下载</button>`;
          rows.push(`<tr>
            <td><b>${escapeHtml(item.label)}</b><div class="muted-line mono">${escapeHtml(item.file)}</div></td>
            <td>${kind}</td>
            <td>${labels}</td>
            <td class="mono" style="white-space:nowrap">${size}</td>
            <td>
              <button data-act="copy" data-url="${escapeHtml(item.upstream)}">复制上游地址</button>
              <div class="muted-line">${escapeHtml(item.license)}</div>
            </td>
            <td>${status}<div class="muted-line">${escapeHtml(item.note || "")}</div></td>
            <td><div class="row-actions">${actions}</div></td>
          </tr>`);
        }
      }
      tableBody.innerHTML = rows.join("");
    }

    function renderAll() {
      el("modelsDir").textContent = state.modelsDir ? `${state.modelsDir}` : "models/";
      renderRows();
      renderDownloadCard();
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
        const data = await api.post(EP.modelsDownload, { preset });
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
        renderRows();
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

    root.querySelector("#mRefresh").addEventListener("click", () => {
      load().catch((err) => toast(`刷新失败：${err.message}`, true));
    });

    tableBody.addEventListener("click", (event) => {
      const button = event.target.closest("button[data-act]");
      if (!button) return;
      const preset = button.dataset.preset;
      const act = button.dataset.act;
      if (act === "download") startDownload(preset);
      else if (act === "delete") removeModel(preset);
      else if (act === "test") go("test");
      else if (act === "copy") ctx.copyText(button.dataset.url);
    });

    ctx.describe()
      .then(renderRuntime)
      .catch(() => {
        /* 顶栏已经报过错了，这里不重复打扰 */
      });
    load().catch((err) => {
      tableBody.innerHTML = `<tr><td colspan="7" class="muted">读不到模型清单：${escapeHtml(err.message)}</td></tr>`;
    });
  },
};
