// 状态页：运行时、权重、任务队列、渠道启用情况与磁盘占用，一眼看清「为什么没生效」。
// 接口：GET panel/status
export default {
  id: "status",
  title: "状态",

  mount(root, ctx) {
    const { el, api, toast, escapeHtml, fmtBytes, EP } = ctx;

    let timer = 0;

    root.innerHTML = `
      <div class="page">
        <div class="page-head">
          <div>
            <h2>状态</h2>
            <p class="hint">每 5 秒自动刷新一次；查完可以停掉自动刷新省事。</p>
          </div>
          <div class="row-actions">
            <span id="sUpdated" class="badge"></span>
            <button id="sAuto">停止自动刷新</button>
            <button id="sRefresh" class="primary">立即刷新</button>
          </div>
        </div>

        <div class="stats" id="sStats"></div>

        <div class="card">
          <div class="card-head"><h2>运行时</h2></div>
          <div class="defs" id="sRuntime"></div>
        </div>

        <div class="card">
          <div class="card-head"><h2>权重</h2></div>
          <div class="defs" id="sModels"></div>
        </div>

        <div class="card">
          <div class="card-head"><h2>下载任务</h2></div>
          <div class="defs" id="sTasks"></div>
        </div>

        <div class="card">
          <div class="card-head"><h2>渠道</h2></div>
          <div class="table-scroll"><table class="data-table compact" id="sChannels"></table></div>
        </div>

        <div class="card">
          <div class="card-head"><h2>磁盘与暂存</h2></div>
          <div class="defs" id="sDisk"></div>
        </div>
      </div>`;

    // 定义列表：左列固定 120px，值列吃满剩余宽度。表格那种 <th width:200px> 在宽屏下
    // 会留出两百多像素的空档，标签与值各飘一头。
    const kv = (rows) =>
      rows
        .map(([key, value]) => `<div class="def"><span>${key}</span><div>${value}</div></div>`)
        .join("");

    const stat = (label, value, tone) =>
      `<div class="stat"><span>${label}</span><b class="${tone || ""}">${value}</b></div>`;

    function render(data) {
      const plugin = data.plugin || {};
      const runtime = data.runtime || {};
      el("sRuntime").innerHTML = kv([
        ["插件", `${escapeHtml(plugin.display_name || "漫画下载器")} ${escapeHtml(plugin.version || "")}`],
        [
          "检测运行时",
          runtime.ready
            ? `<span class="tag ok">onnxruntime ${escapeHtml(runtime.version || "")}</span>`
            : `<span class="tag bad">未就绪</span> ${escapeHtml(runtime.error || "")}`,
        ],
        ["数据目录", `<span class="mono">${escapeHtml(plugin.data_dir || "—")}</span>`],
        ["权重目录", `<span class="mono">${escapeHtml(plugin.models_dir || "")}</span>`],
      ]);

      const models = data.models || {};
      const installed = models.installed || [];
      const rows = [
        ["已安装", `${models.installed_count || 0} 档 · 共 ${fmtBytes(models.total_bytes || 0)}`],
        ["自动下载", models.auto_download ? "已开启（缺权重时加载阶段会自己下）" : "已关闭"],
      ];
      if (installed.length) {
        rows.push([
          "清单",
          installed
            .map(
              (item) =>
                `<span class="tag${item.current ? " ok" : ""}">${escapeHtml(item.label)}</span>`,
            )
            .join(""),
        ]);
        rows.push([
          "权重文件",
          installed
            .map((item) => `<div class="muted-line mono">${escapeHtml(item.file || "")}</div>`)
            .join(""),
        ]);
      }
      const download = models.download || {};
      if (download.state === "downloading") {
        rows.push([
          "正在下载",
          `${escapeHtml(download.preset || "")} · ${Math.round((download.percent || 0) * 100)}%`,
        ]);
      } else if (download.error) {
        rows.push(["上次下载失败", `<span class="tag bad">${escapeHtml(download.error)}</span>`]);
      }
      el("sModels").innerHTML = kv(rows);

      const tasks = data.tasks || {};
      const running = tasks.running || [];
      el("sTasks").innerHTML = kv([
        ["在跑", `${tasks.running_count || 0} 个`],
        ["排队", `${tasks.queue_count || 0} 个`],
        [
          "明细",
          running.length
            ? running
                .map(
                  (item) =>
                    `<div class="muted-line">${escapeHtml(item.label || item.session_id || "")}${
                      item.total ? ` · ${item.done || 0}/${item.total}` : ""
                    }</div>`,
                )
                .join("")
            : "—",
        ],
      ]);

      const channels = data.channels || [];
      el("sChannels").innerHTML = channels.length
        ? `<thead><tr><th>渠道</th><th>命令</th><th>状态</th><th>搜索</th><th>取封面</th></tr></thead><tbody>${channels
            .map(
              (item) => `<tr>
                <td>${escapeHtml(item.command)}</td>
                <td class="mono">/${escapeHtml(item.display_command)}</td>
                <td>${item.enabled ? '<span class="tag ok">已启用</span>' : '<span class="tag warn">未启用</span>'}</td>
                <td>${item.supports_search ? '<span class="tag ok">支持</span>' : '<span class="tag">不支持</span>'}</td>
                <td>${item.supports_cover ? '<span class="tag ok">可以</span>' : '<span class="tag">仅元信息</span>'}</td>
              </tr>`,
            )
            .join("")}</tbody>`
        : `<tbody><tr><td class="muted">没有读到渠道</td></tr></tbody>`;

      const disk = data.disk || {};
      const images = data.images || {};
      el("sDisk").innerHTML = kv([
        [
          "剩余空间",
          disk.total
            ? `${fmtBytes(disk.free || 0)} 可用 / 共 ${fmtBytes(disk.total)}`
            : "读不到",
        ],
        ["面板暂存图片", `${images.cached || 0} / ${images.max || 0} 张（超出会挤掉最早的）`],
      ]);

      const stats = data.tasks || {};
      const modelInfo = data.models || {};
      const diskInfo = data.disk || {};
      const imageInfo = data.images || {};
      el("sStats").innerHTML = [
        stat("在跑 / 排队", `${stats.running_count || 0} / ${stats.queue_count || 0}`),
        stat("已装权重", `${modelInfo.installed_count || 0} 档`),
        stat("磁盘可用", diskInfo.total ? fmtBytes(diskInfo.free || 0) : "读不到"),
        stat("面板暂存", `${imageInfo.cached || 0} / ${imageInfo.max || 0} 张`),
        stat(
          "检测运行时",
          runtime.ready ? "就绪" : "未就绪",
          runtime.ready ? "ok" : "bad",
        ),
      ].join("");

      el("sUpdated").textContent = `更新于 ${new Date().toLocaleTimeString()}`;
      el("sUpdated").className = "badge ok";
    }

    async function refresh(manual) {
      try {
        render(await api.get(EP.status));
      } catch (err) {
        el("sUpdated").textContent = `读状态失败：${err.message}`;
        el("sUpdated").className = "badge bad";
        if (manual) toast(`读状态失败：${err.message}`, true);
      }
    }

    function schedule() {
      clearTimeout(timer);
      timer = setTimeout(async () => {
        await refresh(false);
        schedule();
      }, 5000);
    }

    el("sRefresh").addEventListener("click", () => refresh(true));
    el("sAuto").addEventListener("click", () => {
      const button = el("sAuto");
      const stopping = button.dataset.on !== "0";
      button.dataset.on = stopping ? "0" : "1";
      button.textContent = stopping ? "开启自动刷新" : "停止自动刷新";
      if (stopping) clearTimeout(timer);
      else schedule();
    });
    el("sAuto").dataset.on = "1";

    refresh(true).then(schedule);
  },
};
