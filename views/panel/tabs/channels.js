// 渠道测试页：选一个漫画渠道，填 ID 或作品链接，看元信息与封面能不能取到。
// 接口：GET panel/channels · POST panel/channels/fetch
export default {
  id: "channels",
  title: "渠道测试",

  mount(root, ctx) {
    const { el, api, toast, escapeHtml, EP, go } = ctx;

    const state = {
      channels: [],
      last: null,
      busy: false,
    };

    root.innerHTML = `
      <div class="pane-grid">
        <div class="card">
          <h2>渠道测试</h2>
          <div class="hint">
            用真实作品试一个渠道是否通：填作品 ID 或整条作品链接都可以，链接指向别的站点时按链接那家走。
            这一步只取元信息与封面（不下载整本），所以很快；拿到的封面可以直接送进「打码测试」。
          </div>
          <div class="bar">
            <div class="field">
              <label for="cChannel">渠道</label>
              <select id="cChannel"></select>
            </div>
            <div class="field wide">
              <label for="cText">作品 ID / 链接</label>
              <input type="text" id="cText" placeholder="例如 1047667 或 https://…/g/1047667" />
            </div>
            <button id="cFetch" class="primary">测试</button>
            <span class="spacer"></span>
            <span id="cState" class="badge">—</span>
          </div>
        </div>

        <div class="card" id="cResult" hidden></div>

        <div class="card">
          <h2>渠道能力</h2>
          <div class="hint">「取封面」一列决定这个渠道能不能在面板里做封面预览（不下一整本）。</div>
          <table class="data-table" id="cTable">
            <thead><tr><th>渠道</th><th>命令</th><th>状态</th><th>搜索</th><th>取封面</th></tr></thead>
            <tbody></tbody>
          </table>
        </div>
      </div>`;

    const tableBody = root.querySelector("#cTable tbody");

    function renderTable() {
      if (!state.channels.length) {
        tableBody.innerHTML = `<tr><td colspan="5" class="muted">没有读到渠道清单</td></tr>`;
        return;
      }
      tableBody.innerHTML = state.channels
        .map(
          (item) => `<tr>
            <td>${escapeHtml(item.command)}</td>
            <td class="mono">/${escapeHtml(item.display_command)}</td>
            <td>${item.enabled ? '<span class="tag ok">已启用</span>' : '<span class="tag warn">未启用</span>'}</td>
            <td>${item.supports_search ? "支持" : "—"}</td>
            <td>${item.supports_cover ? '<span class="tag ok">可以</span>' : '<span class="tag warn">仅元信息</span>'}</td>
          </tr>`,
        )
        .join("");
    }

    function renderSelect() {
      el("cChannel").innerHTML = state.channels
        .map(
          (item) =>
            `<option value="${escapeHtml(item.command)}"${item.enabled ? "" : " disabled"}>${
              escapeHtml(item.display_command)
            }（${escapeHtml(item.command)}）${item.enabled ? "" : " · 未启用"}</option>`,
        )
        .join("");
      const first = state.channels.find((item) => item.enabled);
      if (first) el("cChannel").value = first.command;
    }

    function renderResult(data) {
      const card = el("cResult");
      state.last = data;
      const comic = data.comic || {};
      const tags = (comic.tags || [])
        .map((tag) => `<span class="tag">${escapeHtml(tag)}</span>`)
        .join("");
      const cover = data.image
        ? `<img src="${data.image.data_url}" alt="封面" style="max-width:320px;border-radius:8px;border:1px solid var(--line)" />`
        : `<div class="hint warn">${escapeHtml(data.cover_error || "没有封面")}</div>`;
      card.hidden = false;
      card.innerHTML = `
        <h2>${escapeHtml(comic.title || comic.comic_id || "结果")}</h2>
        <div class="kv"><span>渠道</span><span class="mono">${escapeHtml(comic.channel || "")}</span></div>
        <div class="kv"><span>作品 ID</span><span class="mono">${escapeHtml(comic.comic_id || "")}</span></div>
        <div class="kv"><span>作者</span><span>${escapeHtml(comic.author || "—")}</span></div>
        <div class="kv"><span>页数 / 章节</span><span class="mono">${comic.page_count || "—"}</span></div>
        <div class="kv"><span>标签</span><span>${tags || "—"}</span></div>
        <div style="margin-top:12px">${cover}</div>
        <div class="row-actions" style="margin-top:12px">
          ${data.image ? '<button id="cToTest" class="primary">送去打码测试</button>' : ""}
          <button id="cCopyId">复制作品 ID</button>
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
        el("cResult").innerHTML = `<h2>这次没通</h2><div class="hint warn">${escapeHtml(err.message)}</div>`;
        toast(`测试失败：${err.message}`, true);
      } finally {
        button.disabled = false;
      }
    }

    el("cFetch").addEventListener("click", fetchOne);
    el("cText").addEventListener("keydown", (event) => {
      if (event.key === "Enter") fetchOne();
    });

    // 渠道清单走 describe 的缓存，五个页面共一份
    ctx
      .describe()
      .then((info) => {
        state.channels = info.channels || [];
        renderSelect();
        renderTable();
      })
      .catch((err) => {
        tableBody.innerHTML = `<tr><td colspan="5" class="muted">读不到渠道清单：${escapeHtml(err.message)}</td></tr>`;
      });
  },
};
