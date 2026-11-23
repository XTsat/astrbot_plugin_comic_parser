# AGENTS.md — astrbot_plugin_comic_parser

> 本文件是任何 AI 编码代理（agent）进入本项目的**必读**工作说明书。
> 它约定插件的架构、代码规范、文档维护规则。任何对代码、配置、行为的改动，都必须同时遵守本文档的规则。
>
> 适用项目：`astrbot_plugin_comic_parser`（AstrBot 插件，Python）。
>
> **模板版本**：v0.2.1（2026-11-23） —— 本行标记本文件对应的 `astrbot-plugin` 模板版本。回填项目内容时**不要改动本行**；只有从上游合并到新版本时才把它更新为合并到的版本。
> **来源**：本文件是 [agent-templates](https://github.com/XTsat/agent-templates) 的 `templates/astrbot-plugin/AGENTS.md` 副本，已回填本项目的功能清单、架构选型、目录结构、配置项与命令表。最新版见该仓库 `main` 分支。
> 用户说「更新 AGENTS」时按此流程：① 打开来源仓库该模板的 `AGENTS.md`，比对头部「模板版本」与本地是否一致——一致说明已是最新，直接结束，不必拉取逐节比对 → ② 不一致才拉取来源模板最新版 → ③ 与本地逐节比对（含 `docs/` 下各细则页）→ ④ 合并（保留本项目已回填的插件名、命令前缀、目录结构与项目特有约定）→ ⑤ 展示 diff 与改动清单，等用户确认后再落盘，并把头部「模板版本」同步改为合并到的版本。**禁止整文件覆盖**。

> **细则页**：通用写作规范已从正文拆到 [`docs/`](docs/)，正文只留项目特有条件。动手前按下表读对应文件。

| 你要做的事 | 先读 |
|---|---|
| 写 / 改 `CHANGELOG.md`（流程、写作视角、版本号时机、多会话并行） | [`docs/changelog.md`](docs/changelog.md) |
| CHANGELOG 分组格式、加粗、破坏性标记、老版本折叠 | [`docs/changelog-format.md`](docs/changelog-format.md) |
| 写 / 改 `README.md`（写作视角、何时必须更新、双语） | [`docs/readme.md`](docs/readme.md) |
| README 头部表头与四大板块结构（含示例） | [`docs/readme-structure.md`](docs/readme-structure.md) |
| 写代码注释、写 PR 描述 | [`docs/notes.md`](docs/notes.md) |
| 提交信息、版本号、提交前展示与授权 | [`docs/commit.md`](docs/commit.md) |
| 新增 / 修改配置项 | [`docs/config.md`](docs/config.md) |
| 改 / 新增 `views/` | [`docs/views.md`](docs/views.md) |

**多会话并行**：改 `CHANGELOG.md` / `README.md` 这类共享文件前必须先读取当前内容，只做定位式追加，**严禁整文件覆盖**（会盖掉他人条目）。触发条件与完整协议见 [`docs/changelog.md`](docs/changelog.md) 的「多会话并行开发」节。

---

## 1. 项目概览

**AstrBot 插件**是基于 `astrbot.api.star` 框架的 Python 插件，以 `Star` 子类为核心，通过装饰器注册指令组、事件监听器、过滤器，在 AstrBot 运行时中加载。

**本项目**：`astrbot_plugin_comic_parser`（展示名「漫画下载器」），多渠道漫画下载与搜索插件。**下载为主、搜索为辅**：按 ID 或作品链接下载整本漫画，也可以在渠道命令后加 `sou` 搜索作品（结果按序号给出，回序号即可下载，结果多时可按页翻）；不被动解析聊天里随机出现的链接，也不会自动下载搜索结果。

核心能力：

- **分章节下载（可选能力）**：多章节作品可以只下其中几章——命令末尾写「章节」先列目录，写「章节 1-20」只下这几章；渠道用类属性 `chapter_unit` 声明自己有章节，再实现 `fetch_chapters()` 与 `download(chapters=...)`，不实现也不影响整本下载
- **按 ID / 链接下载整本**：一个渠道一条命令（`/jm <ID 或链接>`、`/nh <ID 或链接>`…），另有 `/comic <作品链接>` 按链接自动路由到渠道；**命令名只是入口别名**，命令后面贴的作品链接指向别的渠道时按链接走（`/jm` + nhentai 链接下的是 nhentai 那本）；渠道只负责取元信息与下载图片
- **上层统一打包，按需加密**：任何渠道下载完都走同一套产物流程——`zip` / `pdf` / `long_img` / `none`；设置全局密码才加密（ZIP 用 AES、PDF 用 AES-256），设置了密码却缺少加密依赖时**失败关闭**，绝不静默产出未加密文件
- **准入与调度**：会话白/黑名单、同会话防抖、全局并发上限、整本超时与重试
- **产物交付**：压缩包以文件形式发出；仅本地保存模式的目录落进数据目录并长期保留
- **搜图（以图搜图）**：`/comic 搜图 [严格]` 或只附图不带参数，把一张截图变成作品；命中后每条结果附一条可直接发送的渠道下载命令（`/nh <ID>`、`/eh <gid>-<token>`），同一本作品有多语言版本时默认只列中文。搜图只产出「作品 ID + 现成命令」，不碰渠道搜索接口，也不进打包链路
- **搜索（可选能力）**：`/jm sou <关键词>` 只搜一个渠道，`/comic sou <关键词>` 并发搜所有支持搜索的渠道；每个渠道单独超时，慢渠道自动跳过；搜索结果按序号给出、只留 ID，回序号即可下载，结果多时本地翻页（翻完缓冲再取上游下一页），不进入下载/打包链路
- **排行榜（可选能力）**：`/comic 排行` 聚合所有渠道、`/jm 排行` 只看一个；日/周/月/总榜可选，渠道缺档时自动改看最近一档并注明；渠道覆盖 `ranking()` 并声明 `ranking_boards` 即自动 `supports_ranking`，不实现也能正常下载
- **封面交付（可选能力）**：下载成功后可单独发一张封面；打开「给封面打码」用本地 NSFW 模型只糊掉裸露部位，模型缺失时**不发封面**（绝不发未打码的图），被平台拦下会自动整张重糊重发；支持封面自动撤回
- **面板（views/panel）**：插件详情页里的五个标签——打码测试、模型下载、渠道测试、状态、设置；与聊天命令共用同一套业务实现（`core/probe.py`、`core/panel.py`），不各写一份
- **维护命令**：`/comic 测试`（实访各渠道 + 查依赖与配置冲突）、`/comic 统计`（累计落盘 + 本次运行增量）、`/comic 清理`（清掉按现有配置已过期的临时产物），三条都限管理员

底板参考（只借形态，不复制代码）：配置底板与「白/黑名单 + 防抖 + 超时重试 + `template_list` 渠道模板」的思路参考同类 AstrBot 解析插件；ZIP/PDF 打包与加密行为参考同类漫画下载插件。下载方向与它们都不同，判断取舍时以「下载为主、搜索为辅」为准。

核心文件清单：

| 文件 | 必选 | 职责 |
|------|------|------|
| `main.py` | ✅ | 插件主体，全部后端逻辑 |
| `metadata.yaml` | ✅ | 插件元数据（必填 `name`/`desc`/`version`/`author`；推荐 `display_name`/`repo`；可选 `support_platforms`/`tags`（**全量、单概念**关键词，README 头部只挑重点）/`short_desc`，`astrbot_version` 只在能明确最低版本时才填） |
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
│   ├── chapters.py              # 章节范围解析（`1-20` / `最新5`）与章节清单数据
│   └── errors.py                # PluginError 族（消息面向用户）
├── utils/                       # 通用工具层
│   ├── filename.py              # 文件名清洗与打包基名
│   ├── files.py                 # 落盘小工具：认出「已下好的图」，避免把写了一半的当成品
│   ├── font.py                  # 图片渲染的字体出口（中日韩字形 + 拉丁/数字两套）
│   ├── format.py                # 体积格式化的通用单位（进度、摘要、失败说明共用；超长按整条截断）
│   ├── link.py                  # 作品链接拆主机名/路径、域名归属判断
│   ├── pack_format.py           # 命令末尾的格式词（`/jm 123456 pdf` 只改这一次）
│   └── title.py                 # 标题里的小标记（「AI 生成」这类括注）识别
└── core/                        # 业务层
    ├── channel.py               # BaseChannel 抽象（渠道做取元信息 + 下载；搜索是可选方法）
    ├── registry.py              # 渠道注册表（新增渠道的六处接线见 §2.2）
    ├── search.py                # 搜索编排：单渠道超时、聚合、缓存、翻页与结果渲染
    ├── result_line.py           # 列表条目排版：标题一行、ID 一行，搜索/搜图/榜单共用
    ├── estimate.py              # 下载前体积估算：真实值优先，否则按页数 × 单张体积（可抽样实测）
    ├── search_query.py          # 搜索条件解析：`tag:x -tag:y` → 结构化条件，各站各自翻译
    ├── ranking.py               # 排行榜：单渠道超时、全渠道聚合、缓存、翻页、序号与文字渲染
    ├── ranking_card.py          # 榜单卡片：把一页榜单画成一张可发送的图（Pillow）
    ├── probe.py                 # 渠道探测：聊天 `/comic 测试` 与面板「渠道测试」共用一份
    ├── health.py                # 环境自检：只读本地状态，查依赖缺失与配置冲突
    ├── stats.py                 # 下载统计：累计计数落盘 + 本次运行增量
    ├── janitor.py               # 手动清理：立刻清掉按现有配置本就该过期的临时产物
    ├── cover.py                 # 封面交付：挑封面 → 打分/打码 → 落成可发送的临时文件
    ├── progress.py              # 跨渠道进度上报：统一状态快照 + 独立心跳
    ├── domain_race.py           # 下载前域名测速择优：同时打一批候选，最快的排最前
    ├── panel.py                 # 插件面板业务逻辑：暂存上传图、按页面参数评分与打码
    ├── selftest.py              # 维护类能力的离线自测（自检/统计/清理/探测/接线）
    ├── nsfw/                    # NSFW 检测与打码：权重管理、YOLO 检测器、封面马赛克
    ├── image_search/            # 搜图（以图搜图）：图片 → 作品 ID，命中后仍交给渠道下载
    │   ├── __init__.py          #   只导出对外 API（客户端、归一化、优选、渲染）
    │   ├── client.py            #   上游接口客户端：缩图、严格度系数、错误翻译
    │   ├── candidates.py        #   聊天图片引用收集与解析（URL / 本地路径 / base64）
    │   ├── service.py           #   归一化 → 同书聚合 → 多语言优选 → 渲染下载指令
    │   └── constants.py         #   入口词、端点、系数、语言与来源映射
    ├── channels/                # 每个渠道一个文件夹，渠道私有代码全在自己文件夹里
    │   ├── jmcomic/             # 禁漫天堂（JM）渠道
    │   │   ├── __init__.py      #   只导出 JMComicChannel（上层只认这个类）
    │   │   ├── channel.py       #   BaseChannel 实现：解析 ID → 元信息 → 下载
    │   │   ├── option.py        #   jmcomic 惰性 import + option 构建
    │   │   ├── progress.py      #   下载会话状态、进度上报、重载中断
    │   │   └── constants.py     #   渠道私有常量（域名清单、请求超时/重试默认值）
    │   ├── nhentai/             # nhentai 渠道
    │   │   ├── __init__.py      #   只导出 NhentaiChannel
    │   │   ├── channel.py       #   BaseChannel 实现：解析 ID → 元信息 → 下载
    │   │   ├── api.py          #   官方 API v2 客户端（元信息主路径，纯 JSON）
    │   │   ├── site.py          #   作品页解析、页面抓取兜底、图床地址拼接
    │   │   ├── downloader.py    #   图片并发下载、重试、图床风控兜底
    │   │   ├── progress.py      #   下载会话状态、进度日志、重载中断
    │   │   ├── constants.py     #   渠道私有常量（域名、超时/重试/并发默认值）
    │   │   └── schema.json      #   本渠道配置模板副本（以 _conf_schema.json 为准）
    │   ├── picacg/              # 哔咔漫画渠道（需要账号登录，图址走签名接口）
    │   ├── ehentai/             # E-Hentai 渠道（唯一自带整本体积的渠道）
    │   ├── hanime1/             # Hanime1 渠道（漫画区，供源与 nhentai 同源）
    │   └── wnacg/               # 紳士漫畫渠道（图址带签名，合辑分章）
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
| 业务层 | `core/` | `channel.py`（渠道抽象）、`registry.py`（注册表）、`channels/<渠道名>/`（每个渠道一个文件夹）、`packer.py`（统一打包加密）、`task.py`（单任务编排）、`group_file.py`（群文件交付）、`guard.py`（白黑名单 + 防抖）、`search.py` / `search_query.py`（搜索编排与条件解析）、`result_line.py`（列表条目统一排版）、`estimate.py`（下载前体积估算）、`ranking.py` / `ranking_card.py`（榜单聚合与卡片）、`probe.py` / `health.py` / `stats.py` / `janitor.py`（测试、自检、统计、清理四条维护链路）、`cover.py`（封面挑选与打码交付）、`progress.py`（跨渠道进度）、`domain_race.py`（域名测速择优）、`panel.py`（面板业务）、`nsfw/`（检测与打码）、`image_search/`（搜图：图片 → 作品 ID + 可发送的下载指令） |
| 工具层 | `utils/` | `filename.py`（文件名清洗）、`files.py`（落盘与断点识别）、`font.py`（渲染字体）、`format.py`（体积单位）、`link.py`（链接拆主机名/路径、域名归属）、`pack_format.py`（末尾格式词）、`title.py`（标题小标记） |
| 定义层 | `domain/` | `constants.py`、`config.py`、`models.py`、`errors.py` |

**渠道与上层的分工是硬约束**：渠道（`core/channels/<渠道名>/`）只允许「解析 ID → 取元信息 → 下载图片到目录」，**禁止**在渠道里做打包、加密、发送、清理；这些属于 `core/packer.py` / `core/task.py` / `main.py`。**搜索是可选能力**：渠道覆盖 `BaseChannel.search()` 即自动 `supports_search`，不实现也能正常下载；搜索结果由 `core/search.py` 统一做超时、聚合、翻页与渲染。新增渠道的固定动作仍是 **六处**（漏一处渠道就跑不起来）：

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
- **日志库**：唯一来源是 `from astrbot.api import logger`（AstrBot 官方插件文档要求「使用 `astrbot.api` 的 logger，而不是 `logging` 模块」）。**禁止** `import logging` / `logging.getLogger(...)` / `logging.basicConfig(...)`，也禁止 `loguru` 等第三方日志库、禁止把日志库写进 `requirements.txt`。理由与分层项目的写法见 4.5
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

> `@register` 的最后一个参数只是注册信息，**不是版本源**。插件版本以 `metadata.yaml` 的 `version` 为准（版本号时机见 [`docs/changelog.md`](docs/changelog.md)）；日常改动不递增；准备发布时改这一处，并把 `@register`、README 徽章等辅助位置同步到同一版本号。

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

> 只写 non-obvious reason、禁止残留中间状态与被注释掉的旧代码、docstring 与分区约定见 [`docs/notes.md`](docs/notes.md)。

### 4.5 日志

- **日志来源唯一**：`from astrbot.api import logger`。**禁止** `import logging` / `logging.getLogger(...)`（含「SDK 缺失时退回标准库 logging」这类降级分支），也**禁止** `loguru` 等第三方日志库——AstrBot 官方插件文档原文是「请务必使用 `from astrbot.api import logger` 来获取日志对象，而不是使用 `logging` 模块」。
- **为什么自建 logger 不行**：`astrbot.api.logger` 按**调用位置**路由到本插件专属 logger（`astrbot.plugin.<插件名>`）。插件内任意模块直接 `from astrbot.api import logger` 就会自动归属本插件，不需要自建、也不需要把 logger 当参数层层传递。自建 `logging.getLogger("astrbot_plugin_x.xxx")` 的名字对不上这条链路：日志没有插件归属、插件详情页的独立日志等级与面板的按插件筛选都不生效。
- **分层多文件项目里要「脱离 AstrBot 单测」的层**（如 `core/`、`webapi/` 的非适配模块）：**不要**为了不 import `astrbot` 就退回标准库 logging；在该层只留一个可注入的日志出口（如 `core/logger.py` 暴露 `get_logger()` / `set_logger()`），由 `main.py` 在 `initialize()` 里注入 `astrbot.api.logger`；未注入时直接写 stderr 兜底，同样不 import `logging`。用一条 AST 测试守住「全仓无 `import logging` / `loguru`」。
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

> 本项目的面板视图在 `views/panel/`（五个标签：打码测试、模型下载、渠道测试、状态、设置），与聊天命令共用 `core/probe.py`、`core/panel.py`，不各写一份。
> 目录结构、bridge 通信、脚本加载方式、沙箱限制与验收点见 [`docs/views.md`](docs/views.md)。**改或新增 `views/` 前先读它。**

---

## 5. 配置与数据文件

### 5.1 配置 Schema（_conf_schema.json）

> 字段格式、类型取值、默认值与 WebUI 呈现规则见 [`docs/config.md`](docs/config.md)。**新增或修改配置项前先读它**，并确认三处同步：`_conf_schema.json` + `domain/config.py` + README 配置表。

**本项目现有配置项（顶层 key，改动时三处同步：schema / `domain/config.py` / README 配置表）**：

| key | 类型 | 归属 |
|---|---|---|
| `packing.pack_format` / `packing.pack_password` | string / string | 打包加密（`core/packer.py`） |
| `packing.filename_template` / `packing.filename_custom` / `packing.filename_max_length` | string / string / int | 产物文件名（`utils/filename.py`） |
| `packing.upload_to_group_file` / `packing.group_file_folder` / `packing.group_file_create_folder` | bool / string / bool | 群文件交付（`core/group_file.py`） |
| `packing.download_dir` | string | 仅本地保存目录 |
| `packing.auto_delete_after_send` / `packing.clean_temp_on_start` | bool / bool | 交付与清理（`main.py`） |
| `download.proxy` | string | 渠道共用代理地址（渠道请求、测速、搜索自动代理重试都用它） |
| `download.progress_style` / `download.show_result_summary` | string / bool | 进度与结果提示（`main.py`） |
| `download.max_concurrent_tasks` / `download.timeout` / `download.retry_times` | int | 调度（`core/task.py`） |
| `download.segment_timeout` / `download.resume_ttl` | int / int | 分段超时与断点保留时长（`core/task.py`） |
| `download.chapter_default_range` / `download.chapter_list_limit` | string / int | 分章节：默认范围与目录显示条数（`domain/chapters.py`、`main.py`） |
| `download.estimate_image_kb` / `download.estimate_sample_enabled` / `download.estimate_sample_count` | int / bool / int | 下载前体积估算：单张体积、是否抽样实测、抽样张数（`core/estimate.py`） |
| `cover.send` / `cover.caption` / `cover.recall_seconds` | bool / bool / int | 封面发送、独立说明与自动撤回（`core/cover.py`） |
| `cover.censor.*`（`enable`/`style`/`block`/`padding`/`full_area`/`retry_strong`） | bool / string / int / int / float / bool | 封面打码（`core/nsfw/censor.py`） |
| `cover.model.*`（`preset`/`auto_download`/`download_url`/`threshold`/`imgsz`） | string / bool / string / float / int | 检测模型档位与权重（`core/nsfw/weights.py`） |
| `probe.enable` / `probe.timeout` / `probe.min_speed` / `probe.cache_ttl` | bool / int / int / int | 下载前域名测速（`core/domain_race.py`） |
| `search.enable` / `search.timeout` / `search.page_size` / `search.aggregate_limit` | bool / int / int / int | 搜索开关与节奏（`core/search.py`） |
| `search.cache_ttl` / `search.cooldown` / `search.auto_proxy` / `search.auto_proxy_timeout` | int / int / bool / int | 缓存、冷却与超时自动代理重试（`core/search.py`） |
| `image_search.enable` / `.base_url` / `.timeout` | bool / string / int | 搜图开关、上游地址与超时（`core/image_search/`） |
| `image_search.min_similarity` / `.max_results` / `.title_limit` / `.prefer_chinese` / `.strict_default` | float / int / int / bool / bool | 搜图结果筛选与展示（`core/image_search/service.py`） |
| `image_search.use_proxy` / `.proxy` | bool / string | 搜图代理：默认直连，代理多会被上游弹挑战页（`core/image_search/client.py`） |
| `ranking.enable` / `.default_board` / `.timeout` / `.page_size` / `.aggregate_limit` | bool / string / int / int / int | 榜单开关与节奏（`core/ranking.py`） |
| `ranking.max_pages` / `.max_chapters` / `.cache_ttl` / `.cooldown` | int / int / int / int | 尺寸上限与缓存（`domain/config.py` 的 `ranking_size_ok`、`core/ranking.py`） |
| `ranking.output` / `ranking.image.*`（`page_size`/`columns`/`show_author`/`show_id`） | string / int / int / bool / bool | 输出形态与卡片排版（`core/ranking_card.py`） |
| `access.whitelist` / `access.blacklist` / `access.debounce_interval` | list / list / int | 准入（`core/guard.py`） |
| `channels` | template_list | 渠道模板；每个渠道一个模板，条目带 `__template_key` 标记归属 |

顶层 key 是 `channels` / `access` / `download` / `packing` / `cover` / `probe` / `search` / `image_search` / `ranking` 九个分组（`channels` 是 `template_list`，其余是 `object`，WebUI 里各成一块区域；顺序即设置页里的展示顺序，渠道排在最前）；`domain/config.py` 只按当前分组读取，不为改名/换组做兼容回退——配置项改名或换组即视为破坏性变更，升级后老位置的值不再生效，需在 CHANGELOG 里写明要用户重设。

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
| `/jm <ID 或链接>` | 全部（受白名单/黑名单约束） | 下载整本禁漫天堂作品 |
| `/<渠道命令> <ID 或链接> 章节`（也认 `章`、`chapter`、`ch`） | 全部（受白名单/黑名单约束） | 列出章节目录（序号 + 标题 + 页数）；不分章节的渠道直接说明 |
| `/<渠道命令> <ID 或链接> 章节 <范围>` | 全部（受白名单/黑名单约束） | 只下指定章节：`3`、`1-20`、`-20`、`100-`、`最新5`、`1-5,9` |
| `/nh <ID 或链接>` | 全部（受白名单/黑名单约束） | 下载整本 nhentai 作品；其余渠道同理（`/pica`、`/hm`、`/ssmh`、`/eh`） |
| `/eh <gid:token 或链接>` | 全部（受白名单/黑名单约束） | E-Hentai 的 ID 是 gid 加 token 两段，只给数字会提示补全 |
| `/comic 搜图 [严格]`（也认 `图搜`、`img`、`以图搜`；只发图不带参数也认） | 全部（受白名单/黑名单约束） | 截图找作品，每条结果附一条可直接发送的下载命令；结果同时记进列表可回序号下载 |
| `/comic 排行 [日榜\|周榜\|月榜\|总榜]`（也认 `rank`、`榜`） | 全部（受白名单/黑名单约束） | 一次取所有支持排行榜的渠道；缺档时自动改看最近一档并注明 |
| `<渠道命令> 排行 [时间窗]` | 全部（受白名单/黑名单约束） | 只看一个渠道的榜单 |
| `/<渠道命令> <ID 或链接> 封面`（也认 `cover`） | 全部（受白名单/黑名单约束） | 只发封面图，不下载整本 |
| `/<渠道命令> <ID 或链接> 简介`（也认 `信息`、`详情`、`info`） | 全部（受白名单/黑名单约束） | 只回标题、作者、页数、体积、章节数与标签，不下载整本。E-Hentai 报真实大小，其余渠道按「页数 × 单张体积」估算并标「约」（见 `core/estimate.py`） |
| `/comic <序号>`（渠道命令同理） | 全部（受白名单/黑名单约束） | 下载最近一次搜索结果或榜单里对应编号的那一本 |
| `/comic 下一页`（也认 `next`；上一页用 `上一页` 或 `prev`） | 全部（受白名单/黑名单约束） | 翻最近一次列表的下一页；搜索编号跨页连续，榜单每页重新从 1 开始 |
| `<下载命令> <格式词>` | 全部（受白名单/黑名单约束） | 单次指定打包格式（`zip`/`pdf`/`long_img`/`none`），不改全局配置 |
| `/comic stop`（渠道命令同理，也认 `取消`、`停止`） | 全部（受白名单/黑名单约束） | 停掉本会话的下载；带 `全部` 清整个队列 |
| `/comic status`（也认 `状态`、`队列`） | 全部（受白名单/黑名单约束） | 看排队与进行中的任务 |
| `/comic retry`（也认 `重试`、`继续`） | 全部（受白名单/黑名单约束） | 从断点续跑；带 `全部` 接上所有被打断的，带格式词可换格式 |
| `/comic help`（也认 `/漫画帮助`、`/comichelp`） | 全部 | 查看可用渠道、打包格式、密码与进度提示设置 |
| `/comic site`（也认 `/comic sites`、`/漫画网站`、`/comicsites`） | 全部 | 列出各渠道官网与作品页链接示意（`<ID>` 占位），方便到站内找作品 ID |
| `/comic 测试 [渠道] [ID 或链接]`（也认 `test`、`check`、`自检`、`体检`、`健康`） | 管理员 | 不带参数：实际访问每个已启用渠道（各走自己的域名与代理），报可用性/耗时/线路/失败原因，并查依赖与配置冲突。带渠道 + ID/链接：只试该渠道，与面板「渠道测试」共用 `core/probe.py` |
| `/comic 统计`（也认 `stats`、`stat`、`数据`） | 管理员 | 下载计数：累计落盘（`stats.json`，带版本号）＋本次运行增量，按渠道分列 |
| `/comic 清理`（也认 `clean`、`cleanup`、`清缓存`） | 管理员 | 立刻清掉按现有配置已过期的临时产物；跳过在用目录、未过期断点、面板暂存与仅本地保存目录 |

新增渠道时：命令名由渠道类的 `command` 类属性决定，并在 `main.py` 里加一个同名的 `cmd_<命令>` 薄方法（AstrBot 的命令装饰器只能静态注册，不能按渠道列表动态生成）。渠道未在 WebUI 配置里添加时，对应命令提示「渠道未启用」。

命令命名约定：`command` 填站点全称（如 `jmcomic`、`nhentai`、`picacg`、`hanime1`），缩写填 `command_aliases`（如 `jm`、`nh`、`pica`、`hm`）；**第一个别名是展示名**，帮助文案、用法提示与 README 里一律用缩写，全称只在括号里备注。`main.py` 里用 `@filter.command(全称, alias={全部缩写})`，`cmd_<全称>` 方法按全称查渠道；命令层与渠道内部报错取用户可见命令时用 `channel.display_command`，别直接用 `command`。

命令名只是入口别名：下载入口会先用 `resolve_link()` 把作品链接归到它自己的渠道，裸 ID 才落到命令对应的渠道。因此**渠道命令后面贴别的站点的链接按链接走**（`/jm` + nhentai 链接下的是 nhentai 那本），既不能回退成「用命令的渠道硬解析链接里的数字」，也不要在命令层另写一套域名判断——那种情况下从链接里抠出的数字会指向另一本站的另一本。

---
## 7. 文档维护规范（强制，不可省略）

任何改动在合并前，必须同步维护 `README.md` 与 `CHANGELOG.md`。**未同步文档 = 任务未完成。**

本项目对应的细则页：

- **CHANGELOG**：流程、写作视角、版本号时机 → [`docs/changelog.md`](docs/changelog.md)；分组格式、加粗、破坏性标记、老版本折叠 → [`docs/changelog-format.md`](docs/changelog-format.md)
- **README**：写作视角与双语要求 → [`docs/readme.md`](docs/readme.md)；头部表头与四大板块结构 → [`docs/readme-structure.md`](docs/readme-structure.md)
- **注释与 PR 描述** → [`docs/notes.md`](docs/notes.md)

**多会话并行**：改 `CHANGELOG.md` / `README.md` 这类共享文件前必须先读取当前内容，只做定位式追加，**严禁整文件覆盖**（会盖掉他人条目）。触发条件与完整协议见 [`docs/changelog.md`](docs/changelog.md) 的「多会话并行开发」节。

---

## 8. 提交与发布流程

> 提交信息内容标准、版本号规范、提交前展示与授权的一次性规则见 [`docs/commit.md`](docs/commit.md)。**提交前必读**，要点：

- **提交前必须先向用户展示**：待提交文件清单 + `git diff --stat` + **2–3 个候选提交标题**（注明侧重并标出推荐项），用户选定后才 commit。
- **只有用户明确要求时才 commit / push**；授权一次性，不延续（确认 commit ≠ 授权 push）。
- **提交信息**：Conventional Commits，`<type>(<scope>): <subject>`；subject 中文动宾短句、≤ 28 汉字、只讲一件事；正文可选，只写 diff 看不出来的取舍。
- **版本号只在准备发布时递增**：日常提交不碰 `metadata.yaml` 的 `version`；一次发版汇总多个提交后才确认版本号，并把 README 徽章、i18n 等辅助位置同步到同一新号。
- **提交前必须跑**：`python -c "import main"` 无语法错误（或 `ruff check` 通过）；有测试则测试通过。

---

## 9. 最终检查清单

### 9.1 每次改动完成时（写完代码就查）

- [ ] 架构：分层多文件风格与本项目既有代码一致，新文件放对目录（`main.py → core → utils / domain` 单向依赖）
- [ ] 渠道边界：渠道只做「解析 ID → 取元信息 → 下载图片」，打包 / 加密 / 发送 / 清理留在 `core/` 与 `main.py`；渠道之间禁止互相 import
- [ ] 注释：只写 non-obvious reason，无残留中间状态、无被注释掉的旧代码（细则见 [`docs/notes.md`](docs/notes.md)）
- [ ] 日志：来源只有 `astrbot.api` 的 logger（无 `import logging` / `logging.getLogger` / 第三方日志库），带 `[{PLUGIN_NAME}]` 前缀，异常分类记录，无空 catch
- [ ] README：涉及用户可见变更时已同步，且面向用户书写
- [ ] 配置：新增配置项已按扁平 `{key: {...}}` 写进 `_conf_schema.json`，并在 `domain/config.py` 与代码读写中同步（三处同步）
- [ ] 本地测试：`python core/selftest.py` 通过（或 `ruff check` 通过）

### 9.2 提交前（只在 commit 前查一次，不提交就不用过）

- [ ] CHANGELOG：`[Unreleased]` 已同步且**只写相对上一版的变化**，同一变化合并为一条，条目面向用户，保留他人既有记录（细则见 [`docs/changelog.md`](docs/changelog.md)）
- [ ] 提交信息：Conventional Commits + [`docs/commit.md`](docs/commit.md) 内容标准——type 按「使用者得到什么」选、scope 一个模块词或无、subject ≤ 28 汉字只讲一件事、正文只在 diff 看不出来时才写、空话已删
- [ ] 提交粒度：一个提交只做一件事，文档与代码同提交，无半成品 / 中间态
- [ ] 提交前展示：已给出文件清单、`git diff --stat`、2–3 个候选标题，并取得本轮明确确认

### 9.3 按需检查（只在改动涉及该区域时）

- [ ] 改了 `views/` → 通信只走 bridge、脚本外部 `type="module"`、bridge 有兜底报错、未用 `localStorage`/`target="_blank"`、上传限了大小、视图标题已进 i18n（细则见 [`docs/views.md`](docs/views.md)）
- [ ] 改了 CHANGELOG 折叠 → `[Unreleased]` 与最近 3 段展开，第 4 段起各自 `<details>`，带 `⚠️ 破坏性变更` 的永不折叠（细则见 [`docs/changelog-format.md`](docs/changelog-format.md)）
- [ ] 改了 README → 头部首行 `<img src="logo.png" alt="插件名" width="160" />`（中英两版都有、`alt` 已填）；配置 > 4 项时按类别分组、单类 ≥ 4 项折叠（细则见 [`docs/readme-structure.md`](docs/readme-structure.md)）
- [ ] 发版 → 只改 `metadata.yaml` 的 `version`（与 CHANGELOG 版本段、tag 一致），并同步 `@register`、README 徽章、i18n
- [ ] 新建项目 → 未自行创建 `LICENSE`、README 无许可证章节
- [ ] 改了 `metadata.yaml` → `tags` 全量且一标签一概念；README 头部只列重点
