// 漫画下载器面板：标签页外壳 + 各页共用的 bridge 封装。
//
// 通信只走 bridge（沙箱 iframe 里没有 cookie、没有 token，裸 fetch / EventSource 必然 401）。
// 每个标签页是一个独立模块，导出 { id, title, mount(root, ctx) }；mount 在首次切到该页时
// 才调用一次（懒加载：打开面板不会同时打五个接口）。
import testTab from "./tabs/test.js";
import modelsTab from "./tabs/models.js";
import statusTab from "./tabs/status.js";
import settingsTab from "./tabs/settings.js";

const TABS = [testTab, modelsTab, statusTab, settingsTab];

const bridge = window.AstrBotPluginView || window.AstrBotPluginPage;

const el = (id) => document.getElementById(id);

/** 页面没在对的入口打开、或后端不可用：直接说清楚，别让用户对着一堆空控件猜。 */
function fatal(message) {
  const badge = el("rtBadge");
  if (badge) {
    badge.textContent = message;
    badge.className = "badge bad";
  }
  toast(message, true);
}

function toast(message, isError) {
  const box = el("toast");
  if (!box) return;
  const node = document.createElement("div");
  node.textContent = message;
  if (isError) node.className = "err";
  box.appendChild(node);
  setTimeout(() => node.remove(), isError ? 5200 : 2600);
}

function escapeHtml(text) {
  return String(text).replace(/[&<>"']/g, (ch) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[ch]);
}

function fmtBytes(bytes) {
  const value = Number(bytes) || 0;
  if (value >= 1024 * 1024) return `${(value / 1024 / 1024).toFixed(1)} MB`;
  if (value >= 1024) return `${(value / 1024).toFixed(0)} KB`;
  return `${value} B`;
}

async function copyText(text) {
  try {
    await navigator.clipboard.writeText(String(text));
    toast("已复制");
  } catch {
    toast("复制失败：沙箱里没有剪贴板权限，请手动选中复制", true);
  }
}

/** 各页共用的上下文：接口调用、文案、跳转。 */
const ctx = {
  bridge,
  el,
  toast,
  escapeHtml,
  fmtBytes,
  copyText,
  // 路径必须带插件前缀：bridge 会把它拼到 /api/v1/plugins/extensions/<插件名>/ 后面
  EP: {
    describe: "panel/describe",
    upload: "panel/upload",
    score: "panel/score",
    censor: "panel/censor",
    download: "panel/download",
    models: "panel/models",
    modelsStatus: "panel/models/status",
    modelsDownload: "panel/models/download",
    modelsDelete: "panel/models/delete",
    modelsUpload: "panel/models/upload",
    modelsInstall: "panel/models/install",
    imageDelete: "panel/images/delete",
    channels: "panel/channels",
    channelsFetch: "panel/channels/fetch",
    status: "panel/status",
    settings: "panel/settings",
  },
  api: {
    get: (endpoint, params) => bridge.apiGet(endpoint, params),
    post: (endpoint, body) => bridge.apiPost(endpoint, body),
    upload: (endpoint, file) => bridge.upload(endpoint, file),
    download: (endpoint, params, filename) => bridge.download(endpoint, params, filename),
  },
  t: (key, fallback) => bridge.t(key, fallback),
  /** 跳到另一个标签页（例如「没有已安装模型」时指向模型下载页）。 */
  go(tabId) {
    selectTab(tabId);
  },
  /** 各页共享的只读快照：describe 只取一次。 */
  shared: { describe: null },
};

/** 统一的 describe 缓存：首屏元信息各页都要用，只请求一次。 */
ctx.describe = async () => {
  if (!ctx.shared.describe) {
    ctx.shared.describe = await ctx.api.get(ctx.EP.describe);
  }
  return ctx.shared.describe;
};

const mounted = new Set();

function selectTab(tabId) {
  for (const button of el("tabs").querySelectorAll("button")) {
    button.classList.toggle("on", button.dataset.tab === tabId);
  }
  for (const pane of el("panes").querySelectorAll(".pane")) {
    pane.hidden = pane.dataset.tab !== tabId;
  }
  const tab = TABS.find((item) => item.id === tabId);
  if (tab && !mounted.has(tabId)) {
    mounted.add(tabId);
    const root = el("panes").querySelector(`.pane[data-tab="${tabId}"]`);
    Promise.resolve(tab.mount(root, ctx)).catch((error) => {
      root.innerHTML = `<div class="card"><h2>这一页没能加载</h2><div class="hint">${
        escapeHtml(error && error.message ? error.message : String(error))
      }</div></div>`;
    });
  }
  if (tab && typeof tab.activate === "function") tab.activate();
  // 记住上次停留的标签页：面板常在「调参 → 看状态」之间来回切
  try {
    sessionStorage.setItem("comic-panel-tab", tabId);
  } catch {
    /* 沙箱里 sessionStorage 可能不可用，忽略 */
  }
}

function buildTabs() {
  const nav = el("tabs");
  nav.innerHTML = TABS.map(
    (tab, index) =>
      `<button data-tab="${tab.id}"${index === 0 ? ' class="on"' : ""}>${tab.title}</button>`,
  ).join("");
  nav.addEventListener("click", (event) => {
    const button = event.target.closest("button[data-tab]");
    if (button) selectTab(button.dataset.tab);
  });
}

async function boot() {
  buildTabs();

  if (!bridge) {
    fatal("请从 AstrBot 插件详情页打开本视图（直接打开文件拿不到 bridge）");
    return;
  }

  try {
    await bridge.ready();
  } catch {
    /* 忽略：主题与语言都有兜底 */
  }

  const applyContext = (context) => {
    const dark = context && context.isDark;
    document.documentElement.dataset.theme = dark ? "dark" : "light";
    el("panelTitle").textContent = ctx.t("views.panel.title", "漫画下载器面板");
    document.title = el("panelTitle").textContent;
  };
  applyContext(bridge.getContext());
  const unsubscribe = bridge.onContext(applyContext);
  window.addEventListener("beforeunload", () => {
    if (typeof unsubscribe === "function") unsubscribe();
  });

  // 运行时状态（onnxruntime 是否就绪、插件版本）放在顶栏，五页共用
  try {
    const info = await ctx.describe();
    const runtime = info.runtime || {};
    const badge = el("rtBadge");
    if (runtime.ready) {
      badge.textContent = `onnxruntime ${runtime.version || ""}`.trim();
      badge.className = "badge ok";
    } else {
      badge.textContent = runtime.error || "检测运行时未就绪";
      badge.className = "badge warn";
    }
    const plugin = info.plugin || {};
    el("pluginBadge").textContent = `${plugin.display_name || "漫画下载器"} ${plugin.version || ""}`.trim();
  } catch (error) {
    fatal(`后端没响应：${error && error.message ? error.message : error}`);
  }

  selectTab("test");
}

boot();
