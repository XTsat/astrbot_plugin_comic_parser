# 配置 Schema（_conf_schema.json）

> 本文件是 [`../AGENTS.md`](../AGENTS.md) 的细则页：`_conf_schema.json` 的字段格式、类型取值与 WebUI 呈现规则。
> 不在自动注入范围内，需要时用 `read` 打开。本文件上限 10 KB。

---

## 配置 Schema（_conf_schema.json）

- 新增配置项 → 先在 `_conf_schema.json` 补 schema，再在代码中实现读写；schema 里有的项必须在 README 配置表中出现（漏写 = 文档不全）
- **格式是扁平字典**：顶层 key 就是配置项 key，值是该配置项的 schema 节点（见下方示例）。**没有 `fields` 数组、没有 `schemaVersion` 之类的自造外壳**——AstrBot 直接把整个 JSON 当「key → 节点」解析，套错外壳等于没有配置项
- 常用节点字段：`type`（必填，`string`/`text`/`int`/`float`/`bool`/`object`/`list`/`dict`/`template_list`）、`description`（WebUI 显示的配置名）、`hint`（右侧问号里的补充说明）、`default`、`options`+`labels`+`subtitles`（下拉选项）、`slider`（`{"min","max","step"}`，仅 int/float）、`secret`（密钥类字符串遮罩显示）、`invisible`、`items`（`object`/`dict` 的子 schema）、`_special`（用户/会话选择器等）
- 配置分组用 `object` 嵌套表达（子 schema 放 `items`），WebUI 会按层级展示；**不要在 schema 里发明分组字段**
- 配置项在文档（README 配置表、CHANGELOG）中引用时，**中文名在前、英文 key 在后**：统一写成「中文名（`key`）」形式，例如「超时时间（`timeout`）」「转发图片到其它群（`forward_image`）」；中文名取自 `_conf_schema.json` 的 `description` 字段（`hint` 是问号提示，不要用它当配置名），`key` 用反引号包裹。
- 配置持久化：`self.config["rules"]` 读写 → `self.config.save_config()` 保存
- 数据文件存于 `astrbot/data/plugin_data/<插件名>/`（通过 `StarTools.get_data_dir("插件名")` 获取路径）
- 写操作持 `asyncio.Lock`，读写用 `encoding="utf-8"` + `ensure_ascii=False`
- 原子写入：先写 `.tmp` 再 replace

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
