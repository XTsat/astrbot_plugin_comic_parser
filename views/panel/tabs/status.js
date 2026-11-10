// 状态页：运行时、模型目录、已装权重、渠道启用情况、正在跑的任务。
// 接口：GET panel/status
export default {
  id: "status",
  title: "状态",
  mount(root, ctx) {
    root.innerHTML = `<div class="pane-grid"><div class="card"><h2>状态</h2>
      <div class="hint">这一页正在建设中。</div></div></div>`;
  },
};
