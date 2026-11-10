// 模型下载页：把可选权重全列出来（类别、大小、来源、许可、装没装），在这里下载/删除。
// 接口：GET panel/models · POST panel/models/download · GET panel/models/status · POST panel/models/delete
export default {
  id: "models",
  title: "模型下载",
  mount(root, ctx) {
    root.innerHTML = `<div class="pane-grid"><div class="card"><h2>模型下载</h2>
      <div class="hint">这一页正在建设中。</div></div></div>`;
  },
};
