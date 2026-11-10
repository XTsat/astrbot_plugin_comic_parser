// 设置页：把插件配置表单化，改完直接存回插件的配置（等价于 WebUI 里的设置页）。
// 接口：GET panel/settings · POST panel/settings
export default {
  id: "settings",
  title: "设置",
  mount(root, ctx) {
    root.innerHTML = `<div class="pane-grid"><div class="card"><h2>设置</h2>
      <div class="hint">这一页正在建设中。</div></div></div>`;
  },
};
