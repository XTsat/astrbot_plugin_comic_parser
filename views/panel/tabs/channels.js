// 渠道测试页：选一个漫画渠道，填 ID 或作品链接，看元信息与能否取到封面。
// 接口：GET panel/channels · POST panel/channels/fetch
export default {
  id: "channels",
  title: "渠道测试",
  mount(root, ctx) {
    root.innerHTML = `<div class="pane-grid"><div class="card"><h2>渠道测试</h2>
      <div class="hint">这一页正在建设中。</div></div></div>`;
  },
};
