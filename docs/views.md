# 插件可视化视图（views/）

> 本文件是 [`../AGENTS.md`](../AGENTS.md) 的细则页：WebUI 视图的目录结构、bridge 通信、脚本加载方式与错误兜底。
> 不在自动注入范围内，需要时用 `read` 打开。本文件上限 10 KB。

---

## 插件可视化视图（views/）

**什么时候做视图**：表单复杂、需要状态面板 / 日志 / 文件上传下载 / SSE 实时流 / 图表时做（`views/`）；只是让用户填几个配置项，就用 `_conf_schema.json`，不要为了好看写页面。

**目录与启用**
- `views/<view_name>/index.html` 是唯一入口，没有 `index.html` 的目录会被忽略；`view_name` 用简单目录名（`panel`、`settings`），不要 `.`、`..`、点开头或含 `/`、`\`。
- **不需要在 metadata 里声明**：AstrBot 自动扫描 `views/`（旧 `pages/` 目录仅作兼容别名，新项目一律用 `views/`）。新增/删除视图目录后要**重载插件**；只改静态资源刷新视图即可。
- 前端**零构建**：原生 ES module + 自己的 CSS，不引入打包器、npm 依赖与第三方 UI 框架。
- **`views/` 与 `pages/` 同时存在时只认 `views/`**：把旧页面从 `pages/` 迁到 `views/` 时整目录搬过去，别两处各留一份（另一份会静默不显示）。

**通信只走 bridge**
- 页面拿到的是 `window.AstrBotPluginView`（别名 `AstrBotPluginPage`），可用：`ready()`、`getContext()`、`getLocale()`、`getI18n()`、`t(key, fallback)`、`onContext(handler)`、`apiGet(ep, params)`、`apiPost(ep, body)`、`upload(ep, file)`（字段名固定 `file`）、`download(ep, params, filename)`、`subscribeSSE(ep, handlers, params)`、`unsubscribeSSE(id)`。
- **禁止裸 `fetch` / `new EventSource`**：视图跑在沙箱 iframe 里，拿不到 Dashboard 会话与 token，`EventSource` 也带不了鉴权头（必然 401）。
- **bridge 何时就绪（机制）**：AstrBot 把 SDK 的 `<script>` 注在 `</body>` 之前，也就是**晚于页面里所有内联/同步脚本、早于外部 `type="module"` 脚本**（module 默认 defer）。所以：常规写法用外部 module 脚本；页面首屏就得用 bridge 时，在 `<head>` 里显式写 `<script src="/api/plugin/page/bridge-sdk.js"></script>`（不必自己加 query，服务端会改写）。**别在内联脚本里同步读 `window.AstrBotPluginView`**，那一刻它还不存在。
- **取不到 bridge 要有兜底**：不要直接 `window.AstrBotPluginView.apiGet(...)`（undefined 会抛裸错）。用 `window.AstrBotPluginView ?? window.AstrBotPluginPage` 取，取不到时：短轮询等一会儿（例如 20 次 × 100ms）或直接抛一句中文提示「请从 AstrBot 插件详情页打开本视图」，让用户知道是入口不对而不是插件坏了。
- 初始化先 `await bridge.ready()` 再渲染；语言/主题变化用 `onContext()` 重新渲染，并在 `beforeunload` 里取消监听与 `unsubscribeSSE`。

**后端接口**
- `context.register_web_api(f"/{PLUGIN_NAME}/<action>", handler, ["GET"], "接口描述")`，路由**必须带插件名前缀**；视图侧 endpoint 不带前缀（`apiGet("stats")`）。动态段用 `<item_id>`、`<path:name>`，以关键字参数传给 handler。
- handler 用 `astrbot.api.web` 的 `request` / `json_response` / `error_response` / `file_response` / `stream_response`，不要向业务代码暴露 FastAPI/Quart 原生对象；`request` 只在 handler 执行期间可用，`request.query.get(...)`、`await request.json(default={})`、`await request.files()` 读取入参。
- 视图专用接口直接返回业务 JSON；需要报错用 `error_response(msg, status_code=400)`（bridge 会 reject）。返回 `{status:"ok",data}` 时 bridge 自动解包成 `data`，别依赖这个兼容行为。
- SSE：后端 `stream_response()`，每帧以 `\n\n` 结尾；视图侧 `subscribeSSE` 接收，超时/断线由 bridge 管理。
- 后端仍要校验视图传来的路径、文件名、数值范围（视图侧数据一律不可信）；落盘用 `get_astrbot_plugin_data_path()` 下的插件数据目录并重命名文件。
- **上传大小自己限**：Dashboard 只对自家聊天上传路由限制体积（512MB），插件扩展路由没有统一上限；`upload()` 单文件、字段名固定 `file`，后端要自己按 `upload.content_length` 或 `save_upload_stream(..., max_bytes=...)` 卡一道，再决定报错文案。

**沙箱限制（写页面之前先认清，别事后返工）**
视图由 Dashboard 以 `<iframe sandbox="allow-scripts allow-forms allow-downloads">` 加载，响应头带 `X-Frame-Options: SAMEORIGIN`、`Content-Security-Policy: object-src 'none'; base-uri 'self'`、`Cache-Control: no-store`、`Referrer-Policy: no-referrer`。由此：

- **没有 `allow-same-origin`** → 页面是不透明源：`localStorage` / `sessionStorage` / `indexedDB` / `document.cookie` 一律不可用（直接抛错）。页面状态只能放内存或后端；刷新即丢。
- **没有 `allow-popups`** → `window.open()`、`<a target="_blank">` 一律被拦。别设计「新标签页打开」：要在别处使用的链接就复制文本、要走下载就用 `bridge.download()`。
- **不要拿嵌套 `<iframe>` 当主界面**：外层已是受限沙箱，内嵌页面继承同样的限制（无 cookie、无弹窗、无存储），目标站还可能用 `X-Frame-Options: DENY` / `frame-ancestors` 直接拒载；需要外部页面时让用户自己开浏览器，或把那个能力做成插件后端接口。
- **不能访问父窗口 DOM / Dashboard 存储**（跨源，`window.parent.*` 会直接抛错，父页面只能经 bridge 与你通信）；`object-src 'none'` 表示别用 `<object>`/`<embed>`，`base-uri 'self'` 表示别用 `<base>` 指外部。
- 页面内容 URL 由 Dashboard 下发（带一次性路径令牌），**不要手动拼内容 URL、不要自己加 `asset_token`、不要用 `..` 跳目录**；SPA 用 hash routing（history routing 刷新时要求该路径真有文件）。

**样式与文案**
- 颜色/间距走 CSS 变量，`:root` 放亮色、`[data-theme="dark"]` 放暗色（AstrBot 已在 `<html>` 上同步 `data-theme`，服务端预注入避免闪烁）；类名 kebab-case。
- 视图标题/描述写 `.astrbot-plugin/i18n/<locale>.json` 的 `views.<view_name>.title` / `.description`，页面内文案用 `bridge.t("views.<view_name>.<key>", fallback)`；改 locale 文件后要重载插件。旧的 `pages.<view_name>.*` 只是兼容键——迁移期可以两个键都写（老 WebUI 不丢标题），稳定后只留 `views.*`。
- 插入 DOM 的外部文本必须先转义（页面内自备 `escapeHtml()`），不要把后端字符串直接塞进 `innerHTML`。

**自检**：视图没出现 → 检查 `views/<view>/index.html` 是否存在、插件是否启用、详情页是否刷新；bridge 未定义 → 脚本是否外部 module 且在 SDK 之后；接口 404 → 注册路由是否带插件名前缀、endpoint 是否多写了 `/` 或 query；上传为空 → 字段名必须是 `file`；SSE 401 → 是否绕过了 `bridge.subscribeSSE`。

---
