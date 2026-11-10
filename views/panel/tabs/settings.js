// 设置页：把插件配置表单化，改完直接存回插件配置（等价于 AstrBot 设置页，但更集中）。
// 接口：GET panel/settings · POST panel/settings { values: {"组.键": 值} }
export default {
  id: "settings",
  title: "设置",

  mount(root, ctx) {
    const { el, api, toast, escapeHtml, EP } = ctx;

    // baseline 是「上一次读/存成功时的值」，徽标只用它算「有几项改了还没保存」
    const state = { groups: [], baseline: null, writable: true, skipped: [] };

    root.innerHTML = `
      <div class="page">
        <div class="page-head">
          <div>
            <h2>设置</h2>
            <p class="hint">
              插件配置的表单版：改完点「保存」。渠道模板条目（每个站点的账号、域名、代理等）
              结构较深，仍在 AstrBot 的设置页里改；其余项都能在这里改。
            </p>
            <p class="hint" id="cfgNote"></p>
          </div>
          <div class="row-actions">
            <span id="cfgState" class="badge"></span>
            <button id="cfgReload">放弃改动并重读</button>
            <button id="cfgSave" class="primary">保存</button>
          </div>
        </div>
        <div id="cfgGroups" class="stack"></div>
      </div>`;

    const container = root.querySelector("#cfgGroups");

    function control(item) {
      const id = `f_${item.key.replace(/\./g, "_")}`;
      const common = `id="${id}" data-key="${item.key}"`;
      if (item.type === "bool") {
        return `<label class="check-inline"><input type="checkbox" ${common}${
          item.value ? " checked" : ""
        } /> <span>${item.value ? "已开启" : "已关闭"}</span></label>`;
      }
      if (item.type === "string" && Array.isArray(item.options) && item.options.length) {
        const options = item.options
          .map((value, index) => {
            const label = (item.option_labels || [])[index] || value;
            return `<option value="${escapeHtml(value)}"${value === item.value ? " selected" : ""}>${escapeHtml(
              label,
            )}</option>`;
          })
          .join("");
        return `<select ${common}>${options}</select>`;
      }
      if (item.type === "int" || item.type === "float") {
        const slider = item.slider || {};
        const step = slider.step || (item.type === "float" ? 0.01 : 1);
        const min = slider.min !== undefined ? ` min="${slider.min}"` : "";
        const max = slider.max !== undefined ? ` max="${slider.max}"` : "";
        return `<input type="number" ${common} value="${item.value ?? ""}" step="${step}"${min}${max} />`;
      }
      if (item.type === "list") {
        const text = Array.isArray(item.value) ? item.value.join("\n") : item.value || "";
        return `<textarea ${common} rows="2">${escapeHtml(text)}</textarea>`;
      }
      const type = item.secret ? "password" : "text";
      return `<input type="${type}" ${common} value="${escapeHtml(item.value ?? "")}" />`;
    }

    function render() {
      if (!state.groups.length) {
        container.innerHTML = `<div class="card"><div class="hint">没有读到配置项</div></div>`;
        return;
      }
      container.innerHTML = state.groups
        .map((group) => {
          let lastParent = "";
          const rows = group.items
            .map((item) => {
              const parent = item.parent || "";
              const heading =
                parent && parent !== lastParent
                  ? `<div class="form-sub">${escapeHtml(parent)}</div>`
                  : "";
              lastParent = parent;
              // 配置项 key 留成标签下方的小字：用户查 README 或 schema 时对得上，
              // 但不该跟中文名抢视线
              return `${heading}<div class="form-row">
                <div class="label">
                  <b>${escapeHtml(item.label)}</b>
                  <span class="key">${escapeHtml(item.key)}</span>
                </div>
                <div class="control${item.type === "list" ? " span" : ""}">${control(item)}</div>
                <div class="help">${item.hint ? escapeHtml(item.hint) : ""}</div>
              </div>`;
            })
            .join("");
          return `<div class="card">
            <div class="card-head"><h2>${escapeHtml(group.label)}</h2></div>
            ${group.hint ? `<p class="legend">${escapeHtml(group.hint)}</p>` : ""}
            <div class="form-rows">${rows}</div>
          </div>`;
        })
        .join("");

      if (state.skipped.length) {
        container.insertAdjacentHTML(
          "beforeend",
          `<div class="card"><div class="card-head"><h2>没放进本页的配置</h2></div>${state.skipped
            .map(
              (item) =>
                `<div class="muted-line">${escapeHtml(item.key)}：${escapeHtml(item.reason)}</div>`,
            )
            .join("")}</div>`,
        );
      }
    }

    function collect() {
      const values = {};
      for (const node of container.querySelectorAll("[data-key]")) {
        const key = node.dataset.key;
        if (node.type === "checkbox") values[key] = node.checked;
        else if (node.tagName === "TEXTAREA") {
          values[key] = node.value
            .split("\n")
            .map((line) => line.trim())
            .filter(Boolean);
        } else values[key] = node.value;
      }
      return values;
    }

    /** 徽标只报「改了几项还没保存」：总项数看表单就够了，写满屏数字没人关心。 */
    function syncDirty() {
      if (!state.baseline) return;
      const current = collect();
      let dirty = 0;
      for (const [key, value] of Object.entries(current)) {
        const base = state.baseline[key];
        const same =
          Array.isArray(value) || Array.isArray(base)
            ? JSON.stringify(value) === JSON.stringify(base)
            : String(value ?? "") === String(base ?? "");
        if (!same) dirty += 1;
      }
      el("cfgState").textContent = dirty ? `${dirty} 项未保存` : "没有未保存的改动";
      el("cfgState").className = dirty ? "badge warn" : "badge";
    }

    async function load() {
      const data = await api.get(EP.settings);
      state.groups = data.groups || [];
      state.skipped = data.skipped || [];
      state.writable = data.writable !== false;
      render();
      el("cfgSave").disabled = !state.writable;
      el("cfgNote").textContent = state.writable
        ? "「模型档位」「渠道」「打包」这类改动保存后要重载插件才生效。"
        : "当前插件版本不支持在面板里保存配置，请用 AstrBot 的设置页。";
      state.baseline = collect();
      syncDirty();
    }

    async function save() {
      const values = collect();
      el("cfgSave").disabled = true;
      el("cfgState").textContent = "保存中…";
      try {
        const data = await api.post(EP.settings, { values });
        const parts = [`已保存 ${data.saved} 项`];
        if (data.rejected && data.rejected.length) {
          parts.push(`未通过校验 ${data.rejected.length} 项（${data.rejected.join("、")}）`);
        }
        if (data.need_reload) parts.push("改动要重载插件才生效");
        toast(parts.join("；"));
        if (data.settings) {
          state.groups = data.settings.groups || state.groups;
          state.skipped = data.settings.skipped || state.skipped;
          render();
        }
        state.baseline = collect();
        el("cfgState").textContent = "已保存";
        el("cfgState").className = "badge ok";
      } catch (err) {
        el("cfgState").textContent = "保存失败";
        el("cfgState").className = "badge bad";
        toast(`保存失败：${err.message}`, true);
      } finally {
        el("cfgSave").disabled = !state.writable;
      }
    }

    container.addEventListener("input", syncDirty);
    container.addEventListener("change", syncDirty);

    el("cfgSave").addEventListener("click", save);
    el("cfgReload").addEventListener("click", () => {
      load().catch((err) => toast(`重读失败：${err.message}`, true));
    });

    load().catch((err) => {
      container.innerHTML = `<div class="card"><div class="hint">读不到配置：${escapeHtml(err.message)}</div></div>`;
    });
  },
};
