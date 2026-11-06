# AGENTS.md — astrbot_plugin_comic_parser

> 本文件是任何 AI 编码代理（agent）进入本项目的**必读**工作说明书。
> 它约定插件的架构、代码规范、文档维护规则。任何对代码、配置、行为的改动，都必须同时遵守本文档的规则。
>
> 适用项目：`astrbot_plugin_comic_parser`（AstrBot 插件，Python）。
>
> **模板版本**：v0.1.7（2026-11-04） —— 本行标记本文件对应的 `astrbot-plugin` 模板版本。回填项目内容时**不要改动本行**；只有从上游合并到新版本时才把它更新为合并到的版本。
> **来源**：本文件是 [agent-templates](https://github.com/XTsat/agent-templates) 的 `templates/astrbot-plugin/AGENTS.md` 副本，已回填本项目的功能清单、架构选型、目录结构、配置项与命令表。最新版见该仓库 `main` 分支。
> 用户说「更新 AGENTS」时按此流程：① 打开来源仓库该模板的 `AGENTS.md`，比对头部「模板版本」与本地是否一致——一致说明已是最新，直接结束，不必拉取逐节比对 → ② 不一致才拉取来源模板最新版 → ③ 与本地逐节比对 → ④ 合并（保留本项目已回填的插件名、命令前缀、目录结构与项目特有约定）→ ⑤ 展示 diff 与改动清单，等用户确认后再落盘，并把头部「模板版本」同步改为合并到的版本。**禁止整文件覆盖**。

---

## 1. 项目概览

**AstrBot 插件**是基于 `astrbot.api.star` 框架的 Python 插件，以 `Star` 子类为核心，通过装饰器注册指令组、事件监听器、过滤器，在 AstrBot 运行时中加载。

**本项目**：`astrbot_plugin_comic_parser`（展示名「漫画下载器」），多渠道漫画下载与搜索插件。**下载为主、搜索为辅**：按 ID 或作品链接下载整本漫画，也可以在渠道命令后加 `sou` 搜索作品（结果只给 ID 与下载命令）；不被动解析聊天里随机出现的链接，也不会自动下载搜索结果。

核心能力：

- **按 ID / 链接下载整本**：一个渠道一条命令（`/jm <ID 或链接>`、`/nh <ID 或链接>`…），另有 `/comic <作品链接>` 按链接自动路由到渠道；**命令名只是入口别名**，命令后面贴的作品链接指向别的渠道时按链接走（`/jm` + nhentai 链接下的是 nhentai 那本）；渠道只负责取元信息与下载图片
- **上层统一打包，按需加密**：任何渠道下载完都走同一套产物流程——`zip` / `pdf` / `long_img` / `none`；设置全局密码才加密（ZIP 用 AES、PDF 用 AES-256），设置了密码却缺少加密依赖时**失败关闭**，绝不静默产出未加密文件
- **准入与调度**：会话白/黑名单、同会话防抖、全局并发上限、整本超时与重试
- **产物交付**：压缩包以文件形式发出；仅本地保存模式的目录落进数据目录并长期保留
- **搜索（可选能力）**：`/jm sou <关键词>` 只搜一个渠道，`/comic sou <关键词>` 并发搜所有支持搜索的渠道；每个渠道单独超时，慢渠道自动跳过；搜索结果只给 ID 与下载命令，不进入下载/打包链路

底板参考（只借形态，不复制代码）：配置底板与「白/黑名单 + 防抖 + 超时重试 + `template_list` 渠道模板」的思路参考同类 AstrBot 解析插件；ZIP/PDF 打包与加密行为参考同类漫画下载插件。下载方向与它们都不同，判断取舍时以「下载为主、搜索为辅」为准。

核心文件清单：

| 文件 | 必选 | 职责 |
|------|------|------|
| `main.py` | ✅ | 插件主体，全部后端逻辑 |
| `metadata.yaml` | ✅ | 插件元数据（必填 `name`/`desc`/`version`/`author`；推荐 `display_name`/`repo`；可选 `support_platforms`/`tags`（**全量**关键词，README 头部只挑代表）/`short_desc`，`astrbot_version` 只在能明确最低版本时才填） |
| `_conf_schema.json` | ✅ | WebUI 配置 Schema（扁平 `{key: {type, description, default, ...}}`，定义所有可配置项） |
| `README.md` | ✅ | 中文文档（功能说明、命令表、配置说明） |
| `CHANGELOG.md` | ✅ | 变更日志 |
| `LICENSE` | 可选 | 许可证——**仅在用户明确指定许可证时创建**，禁止自行套用默认许可证 |
| `logo.png` | ✅ | 插件图标（1:1，推荐 256×256） |
| `requirements.txt` | 可选 | 运行时依赖 |
| `.gitignore` | 可选 | Git 忽略规则 |
| `.astrbot-plugin/` | 可选 | 插件 i18n 目录（`i18n/<locale>.json`：`metadata` 插件名/描述、`config` 配置项文案、`views` 视图标题） |
| `views/` | 可选 | WebUI 可视化视图目录（原生 JS + bridge；AstrBot 4.x 首选，`pages/` 只是兼容别名） |

> **许可证按需，且不进 README**：用户没明确说要「用哪个许可证」时，**不创建 `LICENSE` 文件、不在 metadata 或代码里标注许可证**，禁止默认套用 AGPL-3.0、MIT、Apache-2.0 等常见许可证；只有用户明确指定某个许可证时才新建 `LICENSE`。**README 一律不放许可证章节**——即使项目已有 `LICENSE` 文件，也不在 README 里写许可证小节。

---

## 2. 目录结构与架构

### 2.1 项目结构

插件以目录形式存放于 AstrBot 的 `data/plugins/` 目录下，目录名即插件名（AstrBot 4.x；`addons/` 是 3.x 旧路径，已废弃）。

```
astrbot_plugin_comic_parser/
├── main.py                      # Star 子类：命令、准入、进度转发、结果发送
├── metadata.yaml                # 插件元数据
├── _conf_schema.json            # 配置 Schema（含 channels 的 template_list 模板）
├── requirements.txt             # jmcomic / pyzipper / pymupdf / Pillow
├── CHANGELOG.md / README.md / README_en.md / logo.png
├── domain/                      # 数据定义层（无内部依赖）
│   ├── constants.py             # 常量与默认值
│   ├── config.py                # 配置 → 强类型 PluginSettings（脏值兜底）
│   ├── models.py                # ComicMeta / DownloadedComic / PackResult / SearchItem / SearchPage
│   └── errors.py                # PluginError 族（消息面向用户）
├── utils/                       # 通用工具层
│   ├── filename.py              # 文件名清洗与打包基名
│   └── link.py                  # 作品链接拆主机名/路径、域名归属判断
└── core/                        # 业务层
    ├── channel.py               # BaseChannel 抽象（渠道做取元信息 + 下载；搜索是可选方法）
    ├── registry.py              # 渠道注册表（新增渠道的六处接线见 §2.2）
    ├── search.py                # 搜索编排：单渠道超时、聚合、缓存、结果渲染
    ├── channels/                # 每个渠道一个文件夹，渠道私有代码全在自己文件夹里
    │   ├── jmcomic/             # JM 漫画渠道
    │   │   ├── __init__.py      #   只导出 JMComicChannel（上层只认这个类）
    │   │   ├── channel.py       #   BaseChannel 实现：解析 ID → 元信息 → 下载
    │   │   ├── option.py        #   jmcomic 惰性 import + option 构建
    │   │   ├── progress.py      #   下载会话状态、进度上报、重载中断
    │   │   └── constants.py     #   渠道私有常量（域名清单、请求超时/重试默认值）
    │   └── nhentai/             # nhentai 渠道
    │       ├── __init__.py      #   只导出 NhentaiChannel
    │       ├── channel.py       #   BaseChannel 实现：解析 ID → 元信息 → 下载
    │       ├── api.py          #   官方 API v2 客户端（元信息主路径，纯 JSON）
    │       ├── site.py          #   作品页解析、页面抓取兜底、图床地址拼接
    │       ├── downloader.py    #   图片并发下载、重试、图床风控兜底
    │       ├── progress.py      #   下载会话状态、进度日志、重载中断
    │       ├── constants.py     #   渠道私有常量（域名、超时/重试/并发默认值）
    │       └── schema.json      #   本渠道配置模板副本（以 _conf_schema.json 为准）
    ├── packer.py                # **统一打包与加密**：zip / pdf / long_img / none
    ├── task.py                  # 单次任务编排：元信息 → 下载 → 打包
    ├── group_file.py            # 群文件交付：找群文件夹 → 传文件 → 成败与落点
    └── guard.py                 # 白/黑名单 + 防抖
```

运行时数据目录（`StarTools.get_data_dir()` 下）：`temp/` 放下载与待发送产物（发送后清空），`downloads/` 仅在「仅本地保存」模式下承载用户资产。

### 2.2 架构倾向

AstrBot 插件有两种主流架构风格，**新建项目时二选一，并在整个生命周期内保持一致**：

| 风格 | 适用场景 | 参考项目 |
|---|---|---|
| **单文件** | 逻辑简单（< 1500 行） | `astrbot_plugin_msg_forward_cc`、`astrbot_plugin_model_status` |
| **分层多文件** | 逻辑复杂，需模块化 | `astrbot_plugin_help_panel_typst`（main.py → core → utils → domain） |

**单文件风格**：
- 所有逻辑在 `main.py` 一个文件中，通过注释分隔条组织：`# 内部工具方法 → # Web API → # 数据持久化 → # 执行与调度 → # 聊天命令 → # 生命周期`
- 新增方法按职责放入对应分区

**分层多文件风格**：
- 依赖方向严格单向：`main.py → core → utils / domain`；`utils → domain`；`domain` 不依赖任何人
- `domain/`：数据定义层（constants、config、schemas），最底层，无内部依赖
- `utils/`：通用工具层（font、image、hash 等），禁止 import core/ 或 main
- `core/`：核心业务层（analyzer、renderer、worker），纯 Python 逻辑
- 新增常量 → 放 `domain/constants.py`，禁止散落硬编码（只有服务单个渠道的常量放进该渠道文件夹的 `constants.py`，见下方渠道文件夹约定）
- 新增配置项 → 改 `domain/config.py` 的 dataclass 字段 + 兜底逻辑 + `_conf_schema.json`

**本项目采用：分层多文件**。落点固定如下，新增代码按此归位，不要另开平行目录：

| 层 | 目录 | 本项目实际内容 |
|---|---|---|
| 命令层 | `main.py` | Star 子类、命令注册、准入、进度节流、发送与清理 |
| 业务层 | `core/` | `channel.py`（渠道抽象）、`registry.py`（注册表）、`channels/<渠道名>/`（每个渠道一个文件夹）、`packer.py`（统一打包加密）、`task.py`（单任务编排）、`group_file.py`（群文件交付）、`guard.py`（白黑名单 + 防抖）、`search.py`（搜索超时/聚合/渲染） |
| 工具层 | `utils/` | `filename.py`（文件名清洗）、`link.py`（链接拆主机名/路径、域名归属） |
| 定义层 | `domain/` | `constants.py`、`config.py`、`models.py`、`errors.py` |

**渠道与上层的分工是硬约束**：渠道（`core/channels/<渠道名>/`）只允许「解析 ID → 取元信息 → 下载图片到目录」，**禁止**在渠道里做打包、加密、发送、清理；这些属于 `core/packer.py` / `core/task.py` / `main.py`。**搜索是可选能力**：渠道覆盖 `BaseChannel.search()` 即自动 `supports_search`，不实现也能正常下载；搜索结果由 `core/search.py` 统一做超时、聚合与渲染。新增渠道的固定动作仍是 **六处**（漏一处渠道就跑不起来）：

1. `core/channels/<渠道名>/` 新建渠道文件夹：在 `channel.py` 里写 `BaseChannel` 子类（只实现 `normalize_id` / `match_link` / `fetch_meta` / `download`），渠道私有模块都留在该文件夹内，由 `__init__.py` 导出渠道类
2. 加进 `core/registry.py` 的 `CHANNEL_CLASSES`
3. `main.py` 里加同名的 `cmd_<命令>` 薄方法（命令装饰器无法按渠道列表动态生成）
4. `_conf_schema.json` 的 `channels.templates` 加同名模板（渠道私有配置项放这里，读配置一律走 `BaseChannel.opt_*` 系列方法，自动兜底脏值）
5. 需要新依赖时补 `requirements.txt`，并同步 `metadata.yaml` 的 `desc`/`short_desc`/`tags`、README（中英）与 CHANGELOG 文案
6. 实现 `default_options()`（至少含 `enable: True`）——`build_channels()` 用它兜底，内置渠道因此默认启用，老配置无需手动添加

**渠道文件夹约定（每个渠道一个文件夹，互不干扰）**：`core/channels/` 下**只放渠道文件夹，不再放单文件渠道**。以 `jmcomic/` 为模板：

| 文件 | 职责 |
|---|---|
| `__init__.py` | 只导出渠道类（`__all__ = ["JMComicChannel"]`），上层与 `core/channels/__init__.py` 只认这个类 |
| `channel.py` | `BaseChannel` 子类本体：ID 解析、取元信息、下载编排 |
| `option.py` | 第三方库的接入细节（惰性 import、配置 → 库 option 的翻译） |
| `progress.py` | 下载会话状态、进度上报、插件重载时的取消 |
| `constants.py` | 渠道私有常量：域名清单、该站点的请求超时与重试默认值等 |

- 渠道私有模块之间用包内相对 import（`from .option import ...`），跨层引用按深度写足点数（本层到 `core/` 是 `...`，到插件根 `domain/` 是 `....`）
- **渠道之间禁止互相 import**：共用逻辑要往上提（放 `core/` 或 `utils/`），不要从 A 渠道 import B 渠道的东西
- **常量跟着归属走**：只服务一个渠道的常量（域名清单、该站点的默认超时/重试、接口路径）写进该渠道的 `constants.py`；跨渠道共用的常量（`IMAGE_SUFFIXES`、打包格式、文件名模板等）留在 `domain/constants.py`
- 渠道文件夹里**不要回写 `_conf_schema.json`**：私有配置项统一定义在 `channels.templates` 的同名模板里

---

## 3. 技术栈与依赖

- **Python 3.10+**（AstrBot 4.x 最低要求）
- **AstrBot API**：`astrbot.api.star`（Context, Star, register, StarTools）、`astrbot.api.event`（filter, AstrMessageEvent）、`astrbot.api`（logger, AstrBotConfig）
- **依赖管理**：`requirements.txt` 中声明，复杂依赖优先 **惰性 import**（在方法内 import，避免启动时强制加载）
- **禁止引入**：不引入与原功能无关的依赖；非必要不新增 `requirements.txt` 声明
- AstrBot 运行时已自带的依赖（如 `aiohttp`、`certifi`）可在惰性 import 中引用，import 失败时优雅降级

---

## 4. 核心代码规范

### 4.1 插件骨架

```python
import ...
from collections.abc import AsyncGenerator

from astrbot.api.star import register, Star, Context
from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult
from astrbot.api import AstrBotConfig, logger

PLUGIN_NAME = "astrbot_plugin_xxx"
DEFAULT_TIMEOUT = 30

@register(PLUGIN_NAME, "作者名", "插件描述", "v0.1.0")
class MyPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig) -> None:
        super().__init__(context)
        self.config = config
        # 初始化其它资源

    @filter.command_group("xxx")
    def xxx_group(self) -> None:
        pass

    @xxx_group.command("help")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cmd_help(
        self, event: AstrMessageEvent
    ) -> AsyncGenerator[MessageEventResult, None]:
        yield event.plain_result("帮助信息")
```

> `@register` 的最后一个参数只是注册信息，**不是版本源**。插件版本以 `metadata.yaml` 的 `version` 为准（见 7.1 版本号）；日常改动不递增；准备发布时改这一处，并按 7.1 把 `@register`、README 徽章等辅助位置同步到同一版本号。

### 4.2 命名约定

| 元素 | 约定 |
|------|------|
| 模块级常量 | `UPPER_SNAKE`（`DEFAULT_TIMEOUT`、`MAX_CONCURRENCY`） |
| 私有方法 | `_` 前缀 + `snake_case`（`_rebuild_media_component`） |
| Web API 方法 | `api_` 前缀 |
| 命令方法 | `cmd_` 前缀 |
| 类名 | `PascalCase`（`MsgForward`、`ModelStatusPlugin`） |
| 文件名 | `snake_case`（`main.py`、`_conf_schema.json`） |

### 4.3 类型与语法

- Python 3.10+ 现代联合类型写法（`dict \| None`、`list[dict]`、`tuple[str, str]`）
- 函数签名**必须完整类型注解**（参数 + 返回类型）；用 `yield` 返回结果的命令/事件处理方法标注为 `AsyncGenerator[MessageEventResult, None]`
- 禁止 `Any` 作为类型注解（除非调用方签名强制要求）
- 禁止 `# type: ignore`（无理由的类型作弊）

### 4.4 注释规范

- 注释规范见第 7 章「文档维护规范」的 [7.3 注释规范](#73-注释规范)

### 4.5 日志

- 统一 `logger.info/warning/error`，**必须带 `[{PLUGIN_NAME}]` 前缀**
- 错误信息带 emoji 前缀（如 `❌`、`⚠️`）
- 精确捕获异常类型（`asyncio.TimeoutError`），禁止空 `except: pass`（除非注释说明理由）
- 异常分类记录：ValueError = 非法参数，OSError = 文件/IO 错误，各自记录，单规则失败不影响其它规则

### 4.6 错误处理与防御性编程

- 面对不规范的输入必须优雅 fallback（参考 `_get_safe_plugin_info` 风格）
- 外部 IO 必须 try/except 并记录日志，**不允许静默吞异常**
- 配置值防御式校验：`max(1, int(...))`、`(TypeError, ValueError)` → 默认值
- 降级链路清晰：首选方案 → 兜底方案 → 占位文本

### 4.7 并发

- 异步方法使用 `async/await`，**禁止阻塞事件循环**
- 并发限制用 `asyncio.Semaphore(MAX_CONCURRENCY)` + `asyncio.as_completed`，`finally` 中取消未完成任务
- 渲染/计算重活必须走 `asyncio.to_thread` 或子进程
- `ProcessPoolExecutor` 即用即销（`with ProcessPoolExecutor(...)`），子进程结束强制释放内存

### 4.8 回复消息

- 统一 `yield event.plain_result(...)`
- 用户可读输出用中文 + emoji 图标风格

### 4.9 权限控制

- 管理类命令加 `@filter.permission_type(filter.PermissionType.ADMIN)`

### 4.10 插件可视化视图（views/）

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

## 5. 配置与数据文件

### 5.1 配置 Schema（_conf_schema.json）

- 新增配置项 → 先在 `_conf_schema.json` 补 schema，再在代码中实现读写；schema 里有的项必须在 README 配置表中出现（漏写 = 文档不全）
- **格式是扁平字典**：顶层 key 就是配置项 key，值是该配置项的 schema 节点（见下方示例）。**没有 `fields` 数组、没有 `schemaVersion` 之类的自造外壳**——AstrBot 直接把整个 JSON 当「key → 节点」解析，套错外壳等于没有配置项
- 常用节点字段：`type`（必填，`string`/`text`/`int`/`float`/`bool`/`object`/`list`/`dict`/`template_list`）、`description`（WebUI 显示的配置名）、`hint`（右侧问号里的补充说明）、`default`、`options`+`labels`+`subtitles`（下拉选项）、`slider`（`{"min","max","step"}`，仅 int/float）、`secret`（密钥类字符串遮罩显示）、`invisible`、`items`（`object`/`dict` 的子 schema）、`_special`（用户/会话选择器等）
- 配置分组用 `object` 嵌套表达（子 schema 放 `items`），WebUI 会按层级展示；**不要在 schema 里发明分组字段**
- 配置项在文档（README 配置表、CHANGELOG）中引用时，**中文名在前、英文 key 在后**：统一写成「中文名（`key`）」形式，例如「超时时间（`timeout`）」「转发图片到游戏内（`forward_image_to_mc`）」；中文名取自 `_conf_schema.json` 的 `description` 字段（`hint` 是问号提示，不要用它当配置名），`key` 用反引号包裹。
- 配置持久化：`self.config["rules"]` 读写 → `self.config.save_config()` 保存
- 数据文件存于 `astrbot/data/plugin_data/<插件名>/`（通过 `StarTools.get_data_dir("插件名")` 获取路径）
- 写操作持 `asyncio.Lock`，读写用 `encoding="utf-8"` + `ensure_ascii=False`
- 原子写入：先写 `.tmp` 再 replace

**本项目现有配置项（顶层 key，改动时三处同步：schema / `domain/config.py` / README 配置表）**：

| key | 类型 | 归属 |
|---|---|---|
| `packing.pack_format` / `packing.pack_password` | string / string | 打包加密（`core/packer.py`） |
| `packing.filename_template` / `packing.filename_custom` | string / string | 产物文件名（`utils/filename.py`） |
| `packing.download_dir` | string | 仅本地保存目录 |
| `packing.auto_delete_after_send` / `packing.clean_temp_on_start` | bool / bool | 交付与清理（`main.py`） |
| `packing.upload_to_group_file` / `packing.group_file_folder` / `packing.group_file_create_folder` | bool / string / bool | 群文件交付（`core/group_file.py`） |
| `download.progress_style` / `download.show_result_summary` | string / bool | 进度与结果提示（`main.py`） |
| `download.max_concurrent_tasks` / `download.download_timeout` / `download.download_retry_times` | int | 调度（`core/task.py`） |
| `download.proxy` | string | 渠道共用代理地址 |
| `search.enable` / `search.timeout` / `search.page_size` / `search.aggregate_limit` | bool / int / int / int | 搜索开关与节奏（`core/search.py`） |
| `search.cache_ttl` / `search.cooldown` | int / int | 结果缓存与会话冷却（`core/search.py`） |
| `access.whitelist` / `access.blacklist` / `access.debounce_interval` | list / list / int | 准入（`core/guard.py`） |
| `channels` | template_list | 渠道模板；每个渠道一个模板，条目带 `__template_key` 标记归属 |

顶层 key 是 `packing` / `download` / `probe` / `search` / `access` / `channels` 六个分组（前五个是 `object`，WebUI 里各成一块区域）；`domain/config.py` 读分组值时对老的扁平 key 有回退，存量配置不会失效。

渠道模板里的条目会被 `domain/config.py` 收敛成 `{渠道名: 条目}`，渠道私有字段（如 `client_type`、`image_suffix`）由渠道自己用 `BaseChannel.opt_*` 读取，**不要提升成顶层配置**——否则新增渠道会不断污染顶层 schema。

最小可用示例（`_conf_schema.json`）：

```json
{
  "timeout": {
    "type": "int",
    "description": "请求超时时间（秒）",
    "hint": "超过该时长未响应即判定失败并重试",
    "default": 30,
    "slider": {"min": 1, "max": 300, "step": 1}
  },
  "api_key": {
    "type": "string",
    "description": "API Key",
    "secret": true,
    "default": ""
  },
  "rules": {
    "type": "template_list",
    "description": "规则列表",
    "default": []
  },
  "advanced": {
    "type": "object",
    "description": "高级设置",
    "items": {
      "notify_admin": {
        "type": "bool",
        "description": "失败时通知管理员",
        "default": true
      }
    }
  }
}
```

### 5.2 配置读取模式

```python
DEFAULT_SETTINGS = {
    "timeout": 30,
    "max_retries": 3,
}

# 合并：只保留已知字段，丢弃未知字段
settings = {k: self.config.get(k, v) for k, v in DEFAULT_SETTINGS.items()}
```

---

## 6. 指令注册与命令清单

### 6.1 指令注册模式

```python
@filter.command_group("pfx")           # 指令前缀 /pfx
def pfx_group(self) -> None:
    pass

@pfx_group.command("sub")              # 子命令 /pfx sub
@filter.permission_type(filter.PermissionType.ADMIN)
async def cmd_sub(
    self, event: AstrMessageEvent
) -> AsyncGenerator[MessageEventResult, None]:
    yield event.plain_result("结果")
```

### 6.2 事件监听

```python
@filter.event_message_type(filter.EventMessageType.ALL)
async def on_message(
    self, event: AstrMessageEvent
) -> AsyncGenerator[MessageEventResult, None]:
    # 处理全部消息（若不 yield，则标注 -> None）
    pass
```

### 6.3 命令表格式（README 中呈现）

| 命令 | 权限 | 说明 |
|------|------|------|
| `/comic <作品链接>` | 全部（受白名单/黑名单约束） | 按链接自动识别站点并下载整本 |
| `/comic sou <关键词>` | 全部（受白名单/黑名单约束） | 并发搜索所有支持搜索的渠道；每渠道单独超时，慢渠道跳过 |
| `/jm sou <关键词>`（渠道命令同理） | 全部（受白名单/黑名单约束） | 只搜该渠道；`sou` 也认 `search`、`搜索`、`搜` |
| `/jm <ID 或链接>` | 全部（受白名单/黑名单约束） | 下载整本 JM 漫画 |
| `/nh <ID 或链接>` | 全部（受白名单/黑名单约束） | 下载整本 nhentai 作品 |
| `/漫画帮助`（别名 `comichelp`） | 全部 | 查看可用渠道、打包格式、密码与进度提示设置 |
| `/漫画网站`（别名 `comicsites`） | 全部 | 列出各渠道官网与作品页链接示意（`<ID>` 占位），方便到站内找作品 ID |

新增渠道时：命令名由渠道类的 `command` 类属性决定，并在 `main.py` 里加一个同名的 `cmd_<命令>` 薄方法（AstrBot 的命令装饰器只能静态注册，不能按渠道列表动态生成）。渠道未在 WebUI 配置里添加时，对应命令提示「渠道未启用」。

命令命名约定：`command` 填站点全称（如 `jmcomic`、`nhentai`、`picacg`、`hanime1`），缩写填 `command_aliases`（如 `jm`、`nh`、`pica`、`hm`）；**第一个别名是展示名**，帮助文案、用法提示与 README 里一律用缩写，全称只在括号里备注。`main.py` 里用 `@filter.command(全称, alias={全部缩写})`，`cmd_<全称>` 方法按全称查渠道；命令层与渠道内部报错取用户可见命令时用 `channel.display_command`，别直接用 `command`。

命令名只是入口别名：下载入口会先用 `resolve_link()` 把作品链接归到它自己的渠道，裸 ID 才落到命令对应的渠道。因此**渠道命令后面贴别的站点的链接按链接走**（`/jm` + nhentai 链接下的是 nhentai 那本），既不能回退成「用命令的渠道硬解析链接里的数字」，也不要在命令层另写一套域名判断——那种情况下从链接里抠出的数字会指向另一本站的另一本。

---

## 7. 文档维护规范（强制，不可省略）

> 本项目的**硬性要求**：任何改动在合并前，必须同步维护 `README.md` 与 `CHANGELOG.md`。未同步文档 = 任务未完成。

### 7.1 CHANGELOG 维护

#### 流程

- 日常改动先记在 `## [Unreleased]` 段（顶部固定）。**发版时**再把内容移动到带版本号的新段。
- **先记后合**：记录阶段同一工作周期内的改动各自独立记入 `[Unreleased]`（同一功能的多处调整也先各记一条，避免遗漏）；提交或发版前的整理阶段统一过一遍 `[Unreleased]`，把描述同一件事的多条合并成一条，不相关改动保持独立（判断标准：用户会不会把它当成同一件事，细则见下方「写作视角」）。
- **发版日期**：版本段日期 `(YYYY-MM-DD)` 填**当前日期**（以发版当天的系统时间为准，例如 `2026-09-22`）。

#### 格式

统一使用 **分组式**（与 scaffold 的 `CHANGELOG.md.template` 一致）：按分类用 `###` 子标题分组，条目用「标题 + 冒号 + 说明」，**仅重点条目/重点内容才加粗**：

```markdown
# Changelog

## [Unreleased]

> 一句话总结这个版本（可选）

### 新增
- 功能 A：说明
- **功能 B**：说明

### 修复
- 问题 C：说明

## v0.3.0 (2026-07-06)

> 一句话总结这个版本（可选）

### 新增
- 功能 D：说明
```

分组子标题分类：`新增` / `修复` / `变更` / `移除` / `性能`（只保留本次有内容的分类，无内容的分类不写空标题）

**加粗规则**：条目默认不加粗；写 changelog 时先识别本次改动中的**重点改动**（核心新功能、重要修复、破坏性变更等），只对重点改动用 `**加粗**` 标题强调，其余条目一律不加粗。

**版本总结**：版本标题（`## [Unreleased]` / `## vX.Y.Z (YYYY-MM-DD)`）与分类子标题之间，可加一行 `> 引用` 形式的一句话总结（概括该版本/该批改动的主旨），可选，不强制。

#### 写作视角（面向用户）

CHANGELOG 的读者是**插件使用者**（服务器管理员、群成员），不是开发者。条目只写「用户能感知到的变化」，不写内部实现：

- **写用户视角的结果**：用户能做什么、会看到什么、行为/默认值/配置有什么不同。
- **禁止内部实现细节**：类名/函数名、方法拆分、数据结构、算法、缓存与并发策略、日志埋点、依赖升级、重构过程，一律不写。
- **禁止出现代码标识符与文件路径**（如 `_rebuild_media_component`、`core/worker.py`）；用户直接接触的**命令名**、**配置项 key** 除外。
- **无用户感知的改动不记入 CHANGELOG**：纯内部重构、格式化、注释与测试调整等，写了用户也读不懂、也用不上。
- **只写相对上一版的变化**：每条回答「这个版本和用户手上的版本比，变了什么」，只讲差异；不复述仍然有效的旧功能，不写「继续优化」「体验更好」这类没有信息量的空话。
- **同一个变化合并成一条**：同一功能的多处调整、跨多个提交的同一件事，在提交或发版前的整理阶段合并为一条；同一内容不要在不同分类或不同版本段里重复出现。
- **一句话说清改了什么**：按「做了什么 + 用户得到什么」写完，不加背景、动机与实现过程，也不用复杂形容；用户扫一眼就能对上自己关心的功能。
- 判断标准：使用者读完一条，能决定「要不要升级、升级后有什么不同」。

正反例：

```markdown
### 新增
- ✅ 转发图片到游戏内（`forward_image_to_mc`）：开启后图片以地图画形式发送
- ❌ 新增 `_rebuild_media_component` 并重构消息构造链路，改用 dataclass 缓存节点

### 修复
- ✅ 修复超时后不再重试、消息被静默丢弃的问题
- ❌ 修复 `as_completed` 回调未捕获 `asyncio.TimeoutError` 导致的 `finally` 泄漏
```

#### 版本号（准备发布时才确认）

- **日常开发不碰版本号**：只往 `[Unreleased]` 追加条目，`metadata.yaml` 的 `version` 保持原值；每个功能、每个修复各自是一次独立提交，提交里不带版本号变更，也不改辅助位置的版本字样。
- **准备发布时才确认并递增一次**：一次发版汇总了多个独立的功能/修复提交，等合并到指定版本、把 `[Unreleased]` 内容移入 `## vX.Y.Z (YYYY-MM-DD)` 的同一次操作里，才确认版本号（SemVer 见 8.2）、写进下方的**唯一版本源文件**、把辅助位置同步成同一个版本号并打 `git tag vX.Y.Z`。**不能每改一点就递增**——否则版本号会随提交次数突进，与实际发布节奏脱节。
- **唯一版本源**：`metadata.yaml` 的 `version` 字段（`vX.Y.Z` 格式，AstrBot 读取的插件版本）。需要「插件版本」时一律以它为准。

| 版本源文件 | 位置 |
|------|------|
| `metadata.yaml` | `version` 字段（`vX.Y.Z` 格式） |

下列位置属于**辅助信息，不是版本源**，不决定版本号（需要「插件版本」时以 `metadata.yaml` 为准）：`main.py` 中 `@register` 传入的版本号、`README.md` / `README_en.md` 的版本徽章与正文版本字样、`.astrbot-plugin/i18n` 中的版本文案。**日常提交不动它们；准备发布时必须逐个同步到同一个新版本号**，不得残留旧版本号或自相矛盾的版本字样。也不要新增此类重复版本号——需要展示版本时从 `metadata.yaml` 读取，不手写。

#### 多会话并行开发

**⚠️ 更新 `CHANGELOG.md` 前必须先读取当前文件内容**，识别并保留其他会话已写入的既有条目（含 `[Unreleased]` 下未提交的功能），只追加自己的条目，**严禁整文件覆盖或删除他人记录**。

### 7.2 README 维护

#### 7.2.1 写作视角（面向用户）

README 的读者是**插件使用者**（服务器管理员、群成员），不是开发者。只写用户能直接用到的东西：

- **该写的**：能做什么（能力清单）、怎么装（默认一句话：插件市场/WebUI 安装页安装）、怎么配（配置表）、怎么用（指令表与示例）、确有需要提前告知的限制（版本、协议端只在有明确限制时才写，见 7.2.3）。
- **不该写的**：实现原理与设计理念、架构分层与模块划分、算法与数据结构、内部调用流程、类名/函数名/文件路径、性能优化手段、依赖内部实现。
- **代码只贴用户要用的最小片段**：安装命令、配置项、指令示例；不贴内部实现代码。
- **技术细节翻译成用户语言**：不写「用 `asyncio.Semaphore` 限制并发」，写「同时处理的任务数量上限（`max_concurrency`），超出后排队」。
- 判断标准：使用者只读 README，就能独立完成安装 → 配置 → 使用 → 排障。
- **常见问题按需写**：只有用户真的踩到、并且会反复问的排障问题才补「常见问题」小节，一条给一个结论；没有这类问题就不建该小节，也不为凑结构造条目。

正反例：

```markdown
- ✅ 支持把图片以地图画形式转发到游戏内（需在配置中开启）
- ❌ 图片经 `_rebuild_media_component` 转为 dataclass 节点，再由 worker 渲染为地图画
```

#### 7.2.2 头部的表头格式

每个插件维护 **两个** README（中文 `README.md` + 英文 `README_en.md`），**中文版为权威版本（源）**，任何修改先落中文版、再同步到英文版（方向规则见 7.2.5）。表头格式如下（**元素间不留空行**）：

**中文版（`README.md`）：**

```html
<div align="center">
<h1>DisplayName</h1>
<p><strong>插件描述（desc）</strong></p>
<!-- sub 行只放 2–4 个代表标签（每个主要能力方向各取一个），全量标签留在 metadata.yaml 的 tags -->
<p><sub>标签1 &nbsp;&nbsp; 标签2 &nbsp;&nbsp; 标签3</sub></p>
<p><strong>中文</strong> &nbsp;/&nbsp; <a href="README_en.md">English</a></p>
</div>
```

**英文版（`README_en.md`）：**

```html
<div align="center">
<h1>Plugin Name</h1>
<!-- 英文 h1 取插件名后缀首字母大写：astrbot_plugin_model_status → Model Status -->
<p><strong>Plugin description</strong></p>
<!-- only 2–4 representative tags here; the full tag list lives in metadata.yaml -->
<p><sub>tag1 &nbsp;&nbsp; tag2 &nbsp;&nbsp; tag3</sub></p>
<p><a href="README.md">中文</a> &nbsp;/&nbsp; <strong>English</strong></p>
</div>
```

命名规则：
- **中文版 h1**：直接取 `metadata.yaml` 的 `display_name` 字段（如 `模型状态检测`、`跨平台消息转发`）。
- **英文版 h1**：取插件名 `astrbot_plugin_xxx` 中的 `xxx` 部分，拆分为单词后每个首字母大写。例如 `astrbot_plugin_reread` → `Reread`；`astrbot_plugin_model_status` → `Model Status`；`astrbot_plugin_help_panel_typst` → `Help Panel Typst`。
- **desc**：中文版用 `metadata.yaml` 的 `desc` 字段；英文版做地道翻译。
- **标签（tags）**：`metadata.yaml` 的 `tags` 是**全量**清单——各能力方向的关键词都列上（供市场分类与搜索），**不因为 README 头部放不下就删减**；README 头部的 `<sub>` 行**只放代表标签（2–4 个）**，按重要性排序、每个主要能力方向各取一个，只能从 `tags` 里挑（不要新增 `tags` 里没有的标签），用 `&nbsp;&nbsp;` 分隔。中文版用中文标签，英文版用英文标签。

#### 7.2.3 README 四大结构

README 正文严格按以下 4 大板块组织，但不强制使用 `##` 标题层级，可灵活组合：

| 板块 | 内容 | 说明 |
|------|------|------|
| **一、功能/介绍** | 功能介绍、特性列表、用户使用场景、截图/演示 | 独立板块 |
| **二、指令** | 命令表格（命令、权限、说明）、使用示例 | 独立板块 |
| **三、配置** | 配置项表格（配置项、类型、默认值、说明）、`_conf_schema.json` 说明；配置项多时按类别分小节、长尾表折叠（见下方「配置板块怎么写」） | 独立板块 |
| **四、其它** | 依赖要求、常见问题、贡献指南、鸣谢等；确有需要提前告知的限制（某功能要求更高版本、只支持某些协议端）才补「前提」 | 剩余所有内容放这里 |

注意点：
- 板块 4 是兜底区域，不强制子标题顺序。**默认不写分步「安装/快速开始」**：插件从插件市场或 WebUI 安装页装完即用，依赖由 AstrBot 自动安装，AstrBot 版本由 `metadata.yaml` 的 `astrbot_version` 在安装/加载时拦截，把「克隆到某目录 → 重启 → 重载」三步抄进 README 只是噪音。确实需要手动安装时才补安装步骤（例如插件未上架插件市场）。
- **版本与协议端都不是必写项**：能由 `metadata.yaml` 的 `astrbot_version` 表达的最低版本要求，不在 README 重复一遍；只有 metadata 表达不了的**明确限制**才写（某功能需要更高版本、只支持某几个协议端、需要特定部署方式），一两句说完。没有这类限制就不建「前提」小节，也不为凑结构写「需要 AstrBot 4.x」这种空话。**`astrbot_version` 本身也只在能明确最低支持版本时才填**——说不准就不填（保持注释状态），不要写估算范围，更不要为了让字段不空而填。
- **板块一/板块四只写用户视角**：写「运行环境与依赖要求」而非框架选型理由、架构分层、模块职责；实现原理一律不进 README（写作视角见 7.2.1）。
- 更新后通读一遍 README，确保与新行为一致（示例：默认值、超时说明、数据目录等）。
- **「常见问题」是可选子标题**：只有真的出现用户会踩的排障问题才补，默认不建空标题（见 7.2.1）。
- **不写许可证章节**：README 不放许可证小节（含「License」小节、许可证徽章、许可证名标注），即使项目已有 `LICENSE` 文件也不写；新增时若骨架里有这类小节直接删掉。

**配置板块怎么写（板块三）**：

- **配置表是备查资料，不是配置入口**：用户在 WebUI 插件详情页的设置里直接改配置、改完即时生效，很少专门翻 README 找配置项——所以阈值放低（能折叠就折叠），说明列写短，只讲「这一项控制什么」。
- 配置少就不分类：**≤ 4 项**直接一张表，不为了对称硬拆小节。
- 更多就按类别分组：每类一个 `###` 子标题 + 该类一张表，顺序按用户上手顺序（基础/常用在前，进阶/排障在后）；分类要与 `_conf_schema.json` 的真实结构对得上（顶层 key 与 `object` 分组），不要写出 WebUI 里根本不存在的分组。
- **单类 ≥ 4 项就折叠**（不必等到单类 > 10 行），或属于纯参考内容（逐字段明细、平台对照表、模板变量）时，把该类整表包进 `<details>` 折叠，`<summary>` 自解释（类别名 + 项数）：

```markdown
<details>
<summary>高级设置（12 项）</summary>

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| 并发上限（`max_concurrency`） | int | 3 | 同时处理的任务数量上限，超出后排队 |

</details>
```

- **关键配置不进折叠**：首次使用必须填的（API Key、必填地址、功能总开关）留在外面；折叠只装「不读也能用」的参考内容。
- **折叠在 WebUI 会降级**：AstrBot 插件详情页内嵌的 README 会过滤 `details`/`summary` 标签（README 弹窗与 GitHub 正常折叠），折叠块在那里变成普通文字、表格照常显示——因此折叠内容必须自洽可读，不能靠折叠藏必要信息。
- 表格列固定「配置项 / 类型 / 默认值 / 说明」；配置项写「中文名（`key`）」（见 5.1），类型全篇统一（写 schema 的 `type`，或写 WebUI 里的中文类型），不要一张表里混用两种写法。

#### 7.2.4 何时必须更新 README

| 改动类型 | 必须更新的板块 |
|----------|--------------|
| 功能新增/删除 | 一、功能/介绍 |
| 使用方式/命令变化 | 二、指令 |
| 配置项变化 | 三、配置（定位到对应类别小节，必要时拆分类或调整折叠） |
| 依赖/版本要求变化 | 四、其它（确有限制需要提前告知时才补「前提」），能明确最低版本时同步改 `metadata.yaml` 的 `astrbot_version` |
| 已知问题/行为变化（用户会踩到时） | 四、其它（常见问题部分，按需新增） |
| 视图页面新增/改名/删除 | 一、功能/介绍（说明入口位置），并同步 `.astrbot-plugin/i18n` 的 `views.*` 标题 |

**何时不需要更新 README：**
- 纯内部重构（行为完全不变）、样式调整、bug 修复（除非修复改变了用户可见行为）

#### 7.2.5 双语文档同步

- 中文版用 `README.md`，英文版用 `README_en.md`。
- **中文为主（单向同步）**：中文版是**权威版本（源）**，英文版跟随中文版。所有改动**先落中文版，再从中文同步到英文版**；**禁止反向**——不得先改英文版再回填中文，不得用英文版覆盖中文版。
- 章节结构必须保持一致，新增章节两个文件都要加。
- 中文版使用自然中文，英文版使用地道英文，**禁止**逐字机翻、禁止留下半翻译的句子。
- 头部的语言切换链接互相指向对方文件。
- 修改时逐条 diff，确保无内容遗漏或错位。

#### 7.2.6 格式与多会话约束

- **禁止无意义的换行**：HTML 头部区块内元素间不留空行；正文长句不要人为软换行（一段文字保持一行，由 Markdown 自动折行显示）；空行只用于分隔标题、表格、列表、代码块等结构性区块。
- **多会话并行修改**：更新 `README.md` / `README_en.md` 前必须先读取当前文件内容，识别并保留其他会话已写入的既有内容，只追加/修改自己的部分，**严禁整文件覆盖或删除他人记录**。

### 7.3 注释规范

- **注释只写 non-obvious reason**：解释「为什么这么做」，而不是「做了什么」（代码本身应自文档化）
  - 坏：`# 设置超时时间为30秒`（代码已写 `timeout = 30`）
  - 好：`# 30秒超时，因为上游API的SLA承诺P99为25秒`
- **禁止残留 intermediate attempts**：不保留被注释掉的旧代码、TODO 但未完成的方案、调试输出
- 方法级 docstring 用中文说明用途/参数/返回（仅对 `_` 开头私有方法可省略，如果用途足够明显）
- 代码分区用 `# ---- 分区名 ----` 分隔条

### 7.4 PR 描述规范

- **PR 描述只写最终行为**：描述最终完成了什么，而不是过程中尝试了什么
- 禁止提及：尝试过的方案、被否定的设计、从未合入的中间状态
- 只写 diff 里看不出来的取舍理由（如「选用方案A因为性能比B高20%」）

---

## 8. 提交与发布流程

### 8.1 提交规范

- 提交信息遵循 **Conventional Commits**：`<type>(<scope>): <中文短句式 subject>`（如 `feat: 开放颜色配置项`、`fix: 失败发送通知补重试`、`docs: 补充配置说明`）
- **禁止**将未完成的功能、多轮迭代的中间状态、被废弃的方案写进提交信息
- **提交前必须先向用户展示，等用户确认后再执行 commit**，展示内容缺一不可：
  - 待提交文件清单
  - 改动概要（如 `git diff --stat`）
  - **提交标题，尽量给 2–3 个候选**（同一改动的不同侧重点：做了什么 / 为什么改 / 是否带 scope），逐条注明侧重供用户挑选并标出推荐项；确实只有一种合理写法时才只给一个
  - 用户选定标题（或自拟）后才 commit；涉及推送时另附提交 hash、提交信息、目标分支与改动概要
- 推荐顺序：完成功能代码 → 跑通验证 → 更新 CHANGELOG 与 README → 提交
- 文档更新与代码改动**在同一提交中完成**（不单独拆「补文档」提交）
- 只有用户明确要求时才 commit / push
- **授权一次性，不延续**（「上一轮说过推送」≠ 本轮可以提交）：
  - 用户的确认**只对当次动作生效**；下一次改动必须重新展示改动内容与提交标题，重新确认
  - 展示后改动内容又变了（用户提了新要求、你又改了文件，哪怕只改一行），**原确认立即失效**，必须重新展示并再次确认
  - 下列情形**都不算授权**：上一轮 / 上一个任务 / 上一个会话说过「提交」「推送」；会话恢复（resume / fork）后继承的历史授权；用户对功能或代码表示满意；用户正在讨论其它需求；用户未回复
  - 授权范围逐项独立：确认 commit ≠ 授权 push；确认推送某分支 ≠ 授权后续继续提交或推送；确认一个提交 ≠ 授权 `--amend` 或追加提交
  - 执行前自检：能指出用户**本轮**对**本次改动**的明确同意原话；指不出来就先问，不要执行

### 8.2 版本号规范

- 采用 **SemVer**：新功能 → `minor` 递增；bug 修复/小改进 → `patch` 递增
- **只在准备发布时递增**（规则见 7.1）：单个功能/修复提交不碰版本号；一次发版汇总多个独立提交、合并到指定版本时才确认版本号，改 `metadata.yaml` 的 `version`，并把 `@register`、README 徽章、i18n 等辅助位置同步到同一版本号——逐次递增会让版本号突进
- 发版一致性：`metadata.yaml` version = CHANGELOG 新版本段号 = `git tag vX.Y.Z`，tag 推送同样需用户确认

### 8.3 测试与验证

- 项目无强制测试框架，**改动后自检语法与逻辑即可**
- 若项目已补充单测，改动后需保证测试通过
- 格式检查：`python -m ruff format .`（如果项目历史有 ruff 提交）

---

## 9. 最终检查清单（每次任务完成前）

- [ ] 架构：所选风格（单文件 / 分层多文件）与既有代码一致
- [ ] 注释：只写 non-obvious reason，无残留中间状态、无被注释掉的旧代码
- [ ] 日志：`[{PLUGIN_NAME}]` 前缀，异常分类记录，无空 catch
- [ ] CHANGELOG：`[Unreleased]` 已追加条目，**只写相对上一版的变化**、同一变化已合并为一条（无重复条目），条目面向用户（无内部实现细节、无复杂形容即可看懂），保留了他人的既有记录
- [ ] README：涉及用户可见变更时已同步更新，且面向用户书写（无实现原理与内部细节）
- [ ] README 配置板块：> 4 项时已按类别分组，单类 ≥ 4 项已用 `<details>` 折叠且 summary 自解释，首次必填的关键配置没被藏进折叠（配置表是备查资料，宁折叠勿铺长）
- [ ] 标签：`metadata.yaml` 的 `tags` 是全量能力关键词（未因 README 只显示代表标签而删减）；中英两份 README 头部只放 2–4 个代表标签，且都能在 `tags` 里找到
- [ ] README 安装：未写分步「快速开始/安装」（插件市场/安装页装完即用）；版本与协议端仅在确有明确限制时才写，最低版本能明确时写进 `metadata.yaml` 的 `astrbot_version`（不在 README 重复，也不填估算值）
- [ ] 许可证：未自行创建 `LICENSE`、README 中无许可证小节/徽章/许可证名（用户明确指定许可证时才建 `LICENSE`，仍不写进 README）
- [ ] 版本号：日常任务不递增版本号；仅发版任务改 `metadata.yaml` 的 `version`（与 CHANGELOG 新版本段、tag 一致），并把 `@register`、README 徽章、i18n 等辅助位置同步到同一版本号
- [ ] 配置：新增配置项已按扁平 `{key: {...}}` 格式写进 `_conf_schema.json`，并在代码中同步读写
- [ ] 视图（改动或新增 `views/` 时）：通信只走 bridge（无裸 `fetch`/`EventSource`）、脚本是外部 `type="module"` 或 head 里显式引入 SDK、拿 bridge 有兜底报错、没有用 `localStorage` 或 `target="_blank"`、上传自己限了大小、视图标题已进 `.astrbot-plugin/i18n/<locale>.json` 的 `views.<name>`
- [ ] 提交信息：Conventional Commits 格式（`type: 中文短句式`）
- [ ] 提交前展示：已给出待提交文件清单、`git diff --stat`、2–3 个候选提交标题（逐条注明侧重并标出推荐项），并取得用户对本次改动的明确确认（一次性授权，未沿用上一轮）
- [ ] 本地测试：`python -c "import main"` 无语法错误（或 ruff 格式通过）