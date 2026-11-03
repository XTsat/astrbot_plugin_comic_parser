<div align="center">
<h1>漫画下载器</h1>
<p><strong>多渠道漫画下载插件：按 ID 下载整本漫画，统一打包发送，支持密码加密</strong></p>
<p><sub>漫画下载 &nbsp;&nbsp; JM 漫画 &nbsp;&nbsp; 禁漫天堂 &nbsp;&nbsp; 多渠道</sub></p>
<p><strong>中文</strong> &nbsp;/&nbsp; <a href="README_en.md">English</a></p>
</div>

## 功能

- **按 ID 直接下载**：给出漫画 ID 或链接即可下载整本
- **统一打包**：下载完成后统一打包为 ZIP 压缩包、PDF 文档或纵向长图，也可以只保存到本地不发送
- **统一加密**：全局只有一个打包密码，ZIP 用 AES 加密、PDF 用 AES-256 加密；设置了密码但没有安装加密依赖时会直接报错，不会悄悄发出未加密的文件
- **进度可见**：默认在命令消息上贴表情（❤️ 下载中 → OK 完成 / 🔨 失败），贴上了就不再发「开始下载」这类消息；也可切换为文本进度或关闭
- **结果说明可关**：下载完成的摘要（标题 / 作者 / 图片数 / 耗时）可以关掉，只发文件
- **会话管控**：白名单 / 黑名单决定哪些会话能用，防抖避免同一本被反复下载
- **任务排队**：全局并发上限 + 整本超时与重试，多人同时使用时不会把机器拖垮
- **自动清理**：文件发出后删除本地临时文件，长期使用也不占满磁盘

## 指令

| 指令 | 权限 | 说明 |
|------|------|------|
| `/jm <ID 或链接>`（全称 `/jmcomic`） | 全部（受白名单/黑名单约束） | 下载整本 JM 漫画 |
| `/nh <ID 或链接>`（全称 `/nhentai`） | 全部（受白名单/黑名单约束） | 下载整本 nhentai 作品 |
| `/pica <ID 或链接>`（全称 `/picacg`） | 全部（受白名单/黑名单约束） | 下载整本哔咔漫画（需在渠道配置里填哔咔账号） |
| `/hm <ID 或链接>`（全称 `/hanime1`，也可 `/ha`、`/hanime`） | 全部（受白名单/黑名单约束） | 下载整本 Hanime1 漫画 |
| `/漫画帮助` | 全部 | 查看当前可用渠道、打包格式、密码与进度提示设置 |

> 缩写（`/jm`、`/nh`、`/pica`、`/hm`）是日常用法，插件提示里展示的也是缩写；站点全称同样可以敲，两种写法效果一样。

示例：

```
/jm 123456
/jm https://18comic.vip/album/123456/
/nh 123456
/nh https://nhentai.net/g/123456/
/pica 5f1e2d3c4b5a69788796a5b4
/hm 100445
/hm https://hanime1.me/comic/100445
```

## 配置

### 打包与交付

<details>
<summary>打包与交付（7 项）</summary>

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| 打包格式（`pack_format`） | string | zip | 可选 zip / pdf / long_img（纵向长图）/ none（仅本地保存不发送） |
| 打包密码（`pack_password`） | string | 空 | 全局加密密码，留空表示不加密 |
| 文件名模板（`filename_template`） | string | id_title | 产物文件名格式，预置：[ID]标题 / [ID]标题+密码 / 标题[ID] / 标题[ID]+密码 / 仅 ID / 仅标题 / 自定义模板（前四种的 ID 用方括号）。带「+密码」的模板会把密码写进文件名（会出现在聊天记录里） |
| 自定义文件名模板（`filename_custom`） | string | 空 | 仅在模板选「自定义模板」时生效，例如 `{id}-{title}`；可用 `{title}` `{id}` `{author}` `{password}`，密码留空时 `(密码)` 段自动去掉 |
| 本地保存目录（`download_dir`） | string | downloads | 仅「仅本地保存」模式使用，相对路径基于插件数据目录 |
| 发送后删除本地文件（`auto_delete_after_send`） | bool | true | 发出后清理临时文件 |
| 启动时清理临时目录（`clean_temp_on_start`） | bool | true | 插件加载时清空临时目录，清理崩溃/强杀留下的半成品 |

</details>

### 下载控制

<details>
<summary>下载控制（6 项）</summary>

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| 同时下载任务数（`max_concurrent_tasks`） | int | 1 | 全局并发任务上限，超出后排队 |
| 单本下载超时（`download_timeout`） | int | 1800 | 一整本下载的超时秒数，超时判失败并重试 |
| 下载失败重试次数（`download_retry_times`） | int | 2 | 整本下载失败后的重试次数 |
| 发送完成摘要（`show_result_summary`） | bool | true | 关闭后只发文件，不再附「标题 / 作者 / 图片数 / 耗时」说明；仅本地保存模式仍提示保存路径 |
| 进度提示方式（`progress_style`） | string | emoji | 贴表情（默认）/ 文本进度 / 关闭；贴表情在命令消息上贴爱心 ❤️，完成换成 OK、失败换成 🔨；私聊或不支持表情回应时自动改用文本进度 |
| 代理地址（`proxy`） | string | 空 | 形如 `http://host:port`；渠道里勾选「使用代理」后才生效。留空时会自动用 AstrBot 本体的 `http_proxy`（如果配了） |

</details>

### 会话与限制

<details>
<summary>会话与限制（3 项）</summary>

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| 白名单会话（`whitelist`） | list | 空 | 非空时只有列表内会话可用，优先级高于黑名单 |
| 黑名单会话（`blacklist`） | list | 空 | 列表内会话不能使用下载命令 |
| 防抖秒数（`debounce_interval`） | int | 60 | 同一会话在该时间内重复请求同一本时忽略，0 表示关闭 |

</details>

### 下载渠道

<details>
<summary>JM 漫画渠道配置（10 项）</summary>

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| 启用（`enable`） | bool | true | 关闭后 `/jm` 提示渠道未启用 |
| 客户端类型（`client_type`） | string | api | API 接口 / 网页端 / 自定义 API 接口 / 自定义网页端；api 兼容性好，网页端效率高但限制地区 |
| 自定义 API 接口域名（`api_domains`） | text | 4 个常用接口域名 | 一行一个，**从上到下依次降级**（前一个失败换下一个） |
| 自定义网页端域名（`html_domains`） | text | 7 个常用网页域名 | 一行一个，从上到下依次降级 |
| 请求超时（`request_timeout`） | int | 30 | 单次请求超时秒数；调小让坏节点更快放弃 |
| 图片格式（`image_suffix`） | string | .jpg | 下载图片保存的扩展名 |
| 单请求重试次数（`retry_times`） | int | 5 | 单个网络请求失败后的重试次数，调小让坏节点更快放弃 |
| 并发章节数（`max_concurrent_photos`） | int | 3 | 同时下载的章节数量 |
| 并发图片数（`max_concurrent_images`） | int | 5 | 每章同时下载的图片数量 |
| 使用代理（`use_proxy`） | bool | false | 开启后按「插件代理 → AstrBot 本体代理 → 直连」取代理；JM 有国内镜像，一般不用开 |

</details>

### nhentai 渠道

<details>
<summary>nhentai 渠道配置（7 项）</summary>

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| 启用（`enable`） | bool | true | 关闭后 `/nh` 提示渠道未启用 |
| 站点域名（`site_domains`） | text | nhentai.net | 一行一个，**从上到下依次降级**；官方接口与页面兜底共用这份清单，换镜像站改这里 |
| 图床域名（`image_domains`） | text | 空 | 留空自动取站点给的图床清单（推荐）；也可手填一行一个，取第一个 |
| 请求超时（`request_timeout`） | int | 30 | 单次请求超时秒数；境外站点延迟高，调太小容易误判失败 |
| 并发图片数（`max_concurrent_images`） | int | 5 | 同时下载的图片数量；图床对高并发敏感，建议 3-8 |
| 单张图片重试次数（`retry_times`） | int | 2 | 单张图失败后的重试次数；图床偶发风控时会自动改走另一条通道再取一次 |
| 使用代理（`use_proxy`） | bool | true | 开启后按「插件代理 → AstrBot 本体代理 → 直连」取代理；多数网络环境访问 nhentai 需要开 |

</details>

### 哔咔渠道

<details>
<summary>哔咔渠道配置（8 项）</summary>

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| 启用（`enable`） | bool | true | 关闭后 `/pica` 提示渠道未启用 |
| 哔咔账号邮箱（`account`） | string | 空 | **必填**：哔咔不支持匿名访问，没登录就取不到章节与图片 |
| 哔咔密码（`password`） | string | 空 | 建议使用小号，密码只存在插件配置里 |
| 签名密钥（`secret_key`） | string | 哔咔 app 公开密钥 | 只有签名算法变更时才需要改 |
| 请求超时（`request_timeout`） | int | 30 | 单次请求超时秒数；哔咔接口在部分网络下较慢，不建议低于 15 |
| 单请求重试次数（`retry_times`） | int | 3 | 只有服务端错误与限流才重试，0 表示不重试 |
| 并发图片数（`max_concurrent_images`） | int | 5 | 同时下载的图片数量；建议 3-6 |
| 使用代理（`use_proxy`） | bool | true | 开启后按「插件代理 → AstrBot 本体代理 → 直连」取代理；多数网络环境访问哔咔需要开 |

</details>

### Hanime1 渠道

<details>
<summary>Hanime1 渠道配置（8 项）</summary>

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| 启用（`enable`） | bool | true | 关闭后 `/hm` 提示渠道未启用 |
| 站点域名（`site_domains`） | text | hanimeone.me、hanime1.me | 一行一个，**从上到下依次降级**；漫画区现在在 hanimeone.me，hanime1.me 上只留老作品 |
| 图床域名（`image_domains`） | text | 空 | **一般留空**：留空则用页面给出的图床，并在连不上时自动换备用图床；只有在都不通时才手填，例如 i.nhentai.net |
| 读取代理地址（`fetch_proxy`） | string | https://r.jina.ai/ | 默认用公开读取服务代取页面（图片仍直连图床），装好即可抓页；不想让页面经过第三方就清空，清空后只直连站点 |
| 请求超时（`request_timeout`） | int | 30 | 单次请求超时秒数；站点在 Cloudflare 后面偶尔慢，调太小容易误判失败 |
| 并发图片数（`max_concurrent_images`） | int | 5 | 同时下载的图片数量；图床对高并发敏感，建议 3-8 |
| 单请求重试次数（`retry_times`） | int | 2 | 单张图失败后的重试次数；图床偶发风控时会自动改走另一条通道再取一次 |
| 使用代理（`use_proxy`） | bool | true | 开启后按「插件代理 → AstrBot 本体代理 → 直连」取代理；站点被 Cloudflare 拦时通常要靠代理换出口 IP |

</details>
