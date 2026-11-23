# README 结构与表头

> 本文件是 [`../AGENTS.md`](../AGENTS.md) 的细则页：`README.md` 头部的表头格式与四大板块结构（含完整示例）。
> 不在自动注入范围内，需要时用 `read` 打开。本文件上限 10 KB。

---

#### 7.2.2 头部的表头格式

每个插件维护 **两个** README（中文 `README.md` + 英文 `README_en.md`），**中文版为权威版本（源）**，任何修改先落中文版、再同步到英文版（方向规则见 7.2.5）。表头格式如下（**元素间不留空行**）：

**中文版（`README.md`）：**

```html
<div align="center">
<img src="logo.png" alt="DisplayName" width="160" />
<h1>DisplayName</h1>
<p><strong>插件描述（desc）</strong></p>
<!-- sub 行只挑插件真正的重点功能（数量不限，可为 metadata tags 的变体），全量标签留在 metadata.yaml 的 tags -->
<p><sub>标签1 &nbsp;&nbsp; 标签2 &nbsp;&nbsp; 标签3</sub></p>
<p><strong>中文</strong> &nbsp;/&nbsp; <a href="README_en.md">English</a></p>
</div>
```

**英文版（`README_en.md`）：**

```html
<div align="center">
<img src="logo.png" alt="Plugin Name" width="160" />
<h1>Plugin Name</h1>
<!-- 英文 h1 取插件名后缀首字母大写：astrbot_plugin_model_status → Model Status -->
<p><strong>Plugin description</strong></p>
<!-- list only the plugin's headline features here (no fixed count; wording may be a variant of the metadata tags); the full tag list lives in metadata.yaml -->
<p><sub>tag1 &nbsp;&nbsp; tag2 &nbsp;&nbsp; tag3</sub></p>
<p><a href="README.md">中文</a> &nbsp;/&nbsp; <strong>English</strong></p>
</div>
```

命名规则：
- **Logo**：头部第一行固定放插件图标 `<img src="logo.png" alt="插件名" width="160" />`——`logo.png` 与本 README 同目录、用相对路径引用（不写 GitHub raw 绝对链接、不复用 `metadata.yaml` 的远程图标地址）；`alt` 必填（中文版写 `display_name`，英文版写英文插件名），宽度固定 `160`，不加圆角/阴影等额外样式；中英两版都要有。
- **中文版 h1**：直接取 `metadata.yaml` 的 `display_name` 字段（如 `模型状态检测`、`跨平台消息转发`）。
- **英文版 h1**：取插件名 `astrbot_plugin_xxx` 中的 `xxx` 部分，拆分为单词后每个首字母大写。例如 `astrbot_plugin_reread` → `Reread`；`astrbot_plugin_model_status` → `Model Status`；`astrbot_plugin_help_panel_typst` → `Help Panel Typst`。
- **desc**：中文版用 `metadata.yaml` 的 `desc` 字段；英文版做地道翻译。
- **标签（tags）**：`metadata.yaml` 的 `tags` 是**全量关键词清单**（供市场分类与搜索），**不因为 README 头部放不下就删减**；并且**每个标签只放一个概念**——复合词要拆开（「消息转发」→「消息」「转发」，「状态检测」→「状态」「检测」），不写「A+B」式复合词、不写整句，这样一个词条能被独立检索命中。README 头部的 `<sub>` 行只挑**插件真正的重点功能**当标签，**数量不硬性限定**（重点少就两三个，重点多可多列），按重要性排序，用 `&nbsp;&nbsp;` 分隔；README 的标签**可以写成 `tags` 的变体**（措辞更短、更面向用户，允许复合短语，如「消息转发」对应 `tags` 里的「消息」「转发」），不必逐字照抄，但必须是插件真实具备、用户能感知的能力，**不虚构、不夸大**。中文版用中文标签，英文版用英文标签。

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
- 表格列固定「配置项 / 类型 / 默认值 / 说明」；配置项写「中文名（`key`）」（见 [`config.md`](config.md)），类型全篇统一（写 schema 的 `type`，或写 WebUI 里的中文类型），不要一张表里混用两种写法。
