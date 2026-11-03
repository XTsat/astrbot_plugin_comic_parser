# 哔咔漫画渠道（pica）——渠道模板

一句话：**哔咔协议能下，原插件下不动**。本模板保留真实协议（签名、登录、分页、图片直链），重写编排层，挂一条 `/pica <ID 或链接>` 下载整本。

## 文件职责

| 文件 | 职责 |
|---|---|
| `__init__.py` | 只导出 `PicaChannel`，上层只认这个类 |
| `channel.py` | `BaseChannel` 实现：ID 解析 → 元信息 → 下载；目录命名与进度计数也在这里 |
| `api.py` | 哔咔接口细节：HMAC 签名、登录、翻页、图片直链拼接、单图落盘 |
| `constants.py` | 该站点专用常量（接口域名、app 头、签名密钥、图片节点、默认值） |
| `schema.json` | 要贴进 `_conf_schema.json` 的 `channels.templates.pica` 模板 |
| `selftest.py` | 本地假站点自测（不联网、不需要账号）+ `--live` 真站探针 |

渠道只做「取元信息 + 下载图片」；打包、加密、发送、清理全在上层，这里看不到 zip/pdf/密码的逻辑。

## 配置项（`channels.templates.pica`）

| 配置项 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| 启用（`enable`） | bool | true | 关闭后 `/pica` 提示渠道未启用 |
| 哔咔账号邮箱（`account`） | string | 空 | 必填：哔咔没有匿名访问，没登录取不到章节与图片 |
| 哔咔密码（`password`） | string | 空 | 建议用小号 |
| 签名密钥（`secret_key`） | string | 哔咔 app 公开密钥 | 只有签名算法变更时才需要改 |
| 请求超时（`request_timeout`） | int | 30 | 单次请求超时秒数 |
| 单请求重试次数（`retry_times`） | int | 3 | 0 表示不重试；只有 5xx 与限流才重试，4xx 直接报错 |
| 并发图片数（`max_concurrent_images`） | int | 5 | 同时下载的图片数量，建议 3–6 |
| 使用代理（`use_proxy`） | bool | true | 走全局代理；哔咔在多数网络环境需要代理 |

## 接线（本仓库已完成；新仓库照抄六处）

1. `core/channels/pica/` 渠道文件夹 ✅（`channel.py` 里是 `BaseChannel` 子类，私有模块都留在本文件夹）
2. `core/registry.py` 的 `CHANNEL_CLASSES` 加入 `PicaChannel` ✅
3. `main.py` 加 `cmd_pica` 薄方法（`/pica <ID 或链接>`）✅
4. `_conf_schema.json` 的 `channels.templates` 加 `pica` 模板，并在 `default` 里加默认条目 ✅
5. 依赖：只用 AstrBot 自带的 `httpx`，`requirements.txt` 不用改 ✅
6. `default_options()` 已实现（`enable: True`），老配置无需手动添加 ✅

## 自测

```bash
# 本地假站点：验证签名口径、翻页、落盘、失败计数、端到端打包
python core/channels/pica/selftest.py

# 真站探针：在能访问哔咔的网络里确认「真站也下得动」
PICA_ACCOUNT=邮箱 PICA_PASSWORD=密码 PICA_COMIC_ID=24位ID \
  python core/channels/pica/selftest.py --live
```

假站点按哔咔的口径**校验每个请求的 HMAC 签名**（含 query），签名算错就回 401 —— 所以「签名对不对」这一项是真被验证过的。

本地自测覆盖 14 项：ID 解析、元信息、图片落盘张数、单张失败不拖垮整单、目录顺序、进度总数、章节翻页、签名全通过、登录态、图片接口调用、端到端 ZIP、图片直链三种前缀。

## 已知限制

- **必须有哔咔账号**：没有登录态取不到章节与图片清单；登录态失效会提示重新发起请求。
- **网络**：哔咔多数网络环境需要代理；做本模板的执行环境无法直连（只解析出 IPv6、HTTPS 超时，同环境 PyPI 正常），真站结论请用 `--live` 自行验证。
- **证书与协议降级**：哔咔接口与图片节点证书普遍不合规，客户端 `verify=False`；图片请求会把 `https://` 降级为 `http://`（原插件同样如此）。
- **付费 / 已下架**：上游不给图片列表时直接报错，不产出空包。
- **并发**：图床对高并发敏感，`max_concurrent_images` 建议 3–6。
