// 状态页：运行时、权重、任务队列、渠道启用情况与磁盘占用，一眼看清「为什么没生效」。
// 接口：GET panel/status
export default {
  id: "status",
  title: "状态",

  mount(root, ctx) {
    const { el, api, toast, escapeHtml, fmtBytes, EP, go } = ctx;

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
          <p class="legend">「取封面」决定这个渠道能不能在面板里做封面预览（只取元信息、不下一整本）。</p>
          <div class="channel-split">
            <div class="channel-test">
              <div class="field">
                <label for="cChannel">渠道</label>
                <select id="cChannel"></select>
              </div>
              <div class="field">
                <label for="cText">作品 ID / 链接</label>
                <input type="text" id="cText" placeholder="例如 1047667 或 https://…/g/1047667" />
              </div>
              <div class="row-actions">
                <button id="cFetch" class="primary">测试</button>
                <span id="cState" class="badge"></span>
              </div>
              <div id="cResult" hidden></div>
            </div>
            <div class="table-scroll">
              <table class="data-table" id="sChannels"></table>
            </div>
          </div>
        </div>

        <div class="card">
          <div class="card-head"><h2>磁盘与暂存</h2></div>
          <div class="defs" id="sDisk"></div>
        </div>
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
      // 渠道下拉：jm 最常用，排最前并默认选中；它排在清单里第二位时不必让人每次翻下拉
      const tests = [...channels].sort((a, b) => rankChannel(a) - rankChannel(b));
      const previous = el("cChannel").value;
      el("cChannel").innerHTML = tests
        .map(
          (item) =>
            `<option value="${escapeHtml(item.command)}"${item.enabled ? "" : " disabled"}>${escapeHtml(
              item.display_command,
            )}（${escapeHtml(item.command)}）${item.enabled ? "" : " · 未启用"}</option>`,
        )
        .join("");
      // 每 5 秒刷新会重建下拉：把用户当前选的渠道留着，别一刷新就跳回默认
      const keep = tests.find((item) => item.command === previous);
      const fallback = tests.find((item) => item.enabled);
      if (keep) el("cChannel").value = keep.command;
      else if (fallback) el("cChannel").value = fallback.command;

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

    /** jm 优先，其余保持后端给的顺序（后端顺序 = 渠道注册顺序）。 */
    function rankChannel(item) {
      return item.command === "jmcomic" || item.display_command === "jm" ? 0 : 1;
    }

    function renderResult(data) {
      const card = el("cResult");
      const comic = data.comic || {};
      const tags = (comic.tags || []).map((tag) => `<span class="tag">${escapeHtml(tag)}</span>`).join("");
      const cover = data.image
        ? `<img src="${data.image.data_url}" alt="封面" />`
        : `<div class="hint warn">${escapeHtml(data.cover_error || "没有封面")}</div>`;
      card.hidden = false;
      card.innerHTML = `
        <div class="card-head" style="margin-top: var(--sp-3)">
          <h2>${escapeHtml(comic.title || comic.comic_id || "结果")}</h2>
        </div>
        <div class="result-split">
          <div class="result-facts">
            <div class="kv"><span>渠道</span><span class="mono">${escapeHtml(comic.channel || "")}</span></div>
            <div class="kv"><span>作品 ID</span><span class="mono">${escapeHtml(comic.comic_id || "")}</span></div>
            <div class="kv"><span>作者</span><span>${escapeHtml(comic.author || "—")}</span></div>
            <div class="kv"><span>页数 / 章节</span><span class="mono">${comic.page_count || "—"}</span></div>
            <div class="kv"><span>标签</span><span class="tag-row" style="justify-content:flex-end">${tags || "—"}</span></div>
            <div class="row-actions" style="margin-top: 10px">
              ${data.image ? '<button id="cToTest" class="primary">送去打码测试</button>' : ""}
              <button id="cCopyId">复制作品 ID</button>
            </div>
          </div>
          <div class="result-cover">${cover}</div>
        </div>`;

      const toTest = card.querySelector("#cToTest");
      if (toTest) {
        toTest.addEventListener("click", () => {
          // 交给测试页消化：面板内共享一张「待处理图片」，切过去就自动加进列表
          ctx.shared.pendingImage = {
            image_id: data.image.image_id,
            data_url: data.image.data_url,
            width: data.image.width,
            height: data.image.height,
            bytes: data.image.bytes,
            name: `${comic.title || comic.comic_id} · 封面`,
          };
          go("test");
        });
      }
      card.querySelector("#cCopyId").addEventListener("click", () => ctx.copyText(comic.comic_id || ""));
    }

    async function fetchOne() {
      const channel = el("cChannel").value;
      const text = el("cText").value.trim();
      if (!channel || !text) {
        toast("先选渠道并填作品 ID 或链接", true);
        return;
      }
      const button = el("cFetch");
      button.disabled = true;
      el("cState").textContent = "测试中…";
      el("cState").className = "badge warn";
      const started = performance.now();
      try {
        const data = await api.post(EP.channelsFetch, { channel, text });
        const ms = Math.round(performance.now() - started);
        renderResult(data);
        el("cState").textContent = `通 · ${ms} ms`;
        el("cState").className = "badge ok";
      } catch (err) {
        el("cState").textContent = "失败";
        el("cState").className = "badge bad";
        el("cResult").hidden = false;
        el("cResult").innerHTML = `<div class="hint warn" style="margin-top: var(--sp-3)">${escapeHtml(err.message)}</div>`;
        toast(`测试失败：${err.message}`, true);
      } finally {
        button.disabled = false;
      }
    }

    el("cFetch").addEventListener("click", fetchOne);
    el("cText").addEventListener("keydown", (event) => {
      if (event.key === "Enter") fetchOne();
    });

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
