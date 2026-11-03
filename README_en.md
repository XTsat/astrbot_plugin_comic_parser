<div align="center">
<h1>Comic Parser</h1>
<p><strong>Multi-channel comic downloader: grab a whole comic by ID, then pack and send it in one pipeline — optional password encryption</strong></p>
<p><sub>Comic download &nbsp;&nbsp; JM comic &nbsp;&nbsp; 18comic &nbsp;&nbsp; Multi-channel</sub></p>
<p><a href="README.md">中文</a> &nbsp;/&nbsp; <strong>English</strong></p>
</div>

## Features

- **Download by ID**: hand over a comic ID or link and get the whole comic
- **Unified packing**: every download is packed as a ZIP archive, a PDF, or a long vertical image — or simply kept on disk without sending
- **Unified encryption**: one global password. ZIP uses AES, PDF uses AES-256. If a password is set but the encryption library is missing, the plugin fails loudly instead of quietly sending an unencrypted file
- **Visible progress**: by default the bot reacts on your command message (❤️ downloading → OK done / 🔨 failed) and skips the extra "download started" message; you can switch to text progress or turn it off
- **Optional result block**: the summary sent with the file (title / author / image count / elapsed) can be turned off
- **Session control**: whitelist / blacklist decide who may download, debounce stops the same comic being requested twice
- **Queued tasks**: global concurrency limit plus per-comic timeout and retry, so several users cannot overload the host
- **Automatic cleanup**: temporary files are deleted right after sending

## Commands

| Command | Permission | Description |
|---------|------------|-------------|
| `/jm <ID or link>` (full name `/jmcomic`) | Everyone (subject to whitelist/blacklist) | Download a whole JM comic |
| `/nh <ID or link>` (full name `/nhentai`) | Everyone (subject to whitelist/blacklist) | Download a whole nhentai gallery |
| `/pica <ID or link>` (full name `/picacg`) | Everyone (subject to whitelist/blacklist) | Download a whole PicaACG comic (needs a PicaACG account in the channel settings) |
| `/hm <ID or link>` (full name `/hanime1`, also `/ha`, `/hanime`) | Everyone (subject to whitelist/blacklist) | Download a whole Hanime1 comic |
| `/漫画帮助` | Everyone | Show available channels, pack format, password and progress settings |

> The short commands (`/jm`, `/nh`, `/pica`, `/hm`) are what you will use day to day, and they are what the bot shows in its replies; the full site names work exactly the same way.

Examples:

```
/jm 123456
/jm https://18comic.vip/album/123456/
/nh 123456
/nh https://nhentai.net/g/123456/
/pica 5f1e2d3c4b5a69788796a5b4
/hm 100445
/hm https://hanime1.me/comic/100445
```

## Configuration

### Packing and delivery

<details>
<summary>Packing and delivery (7)</summary>

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| Pack format (`pack_format`) | string | zip | One of zip / pdf / long_img / none (keep locally, do not send) |
| Pack password (`pack_password`) | string | empty | Global encryption password; empty means no encryption |
| Filename template (`filename_template`) | string | id_title | Naming pattern for the output file. Presets: [id]title / [id]title+password / title[id] / title[id]+password / ID only / title only / custom (the first four bracket the ID). Presets marked "+password" write the password into the filename (visible in chat) |
| Custom filename template (`filename_custom`) | string | empty | Only used when the template is "custom", e.g. `{id}-{title}`; placeholders `{title}` `{id}` `{author}` `{password}`; the `(password)` part disappears when no password is set |
| Local save directory (`download_dir`) | string | downloads | Only used by the "keep locally" mode; relative paths are based on the plugin data directory |
| Delete local file after sending (`auto_delete_after_send`) | bool | true | Removes temporary files once sent |
| Clean temp on startup (`clean_temp_on_start`) | bool | true | Empties the temp directory when the plugin loads, clearing leftovers from crashes or forced restarts |

</details>

### Download control

<details>
<summary>Download control (6)</summary>

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| Concurrent download tasks (`max_concurrent_tasks`) | int | 1 | Global task limit; extra requests queue up |
| Per-comic timeout (`download_timeout`) | int | 1800 | Seconds allowed for one whole comic before it counts as failed |
| Download retries (`download_retry_times`) | int | 2 | Retries after a failed download |
| Send result summary (`show_result_summary`) | bool | true | When off, only the file is sent without the title / author / image count / elapsed block; the keep-locally mode still reports the path |
| Progress style (`progress_style`) | string | emoji | `emoji` (default) / `text` / `none`; the emoji mode reacts ❤️ on the command message, then swaps to OK or 🔨. Private chats and clients without reaction support fall back to text progress automatically |
| Proxy address (`proxy`) | string | empty | Such as `http://host:port`; only channels with "use proxy" enabled use it. When empty, AstrBot's own `http_proxy` is reused if configured |

</details>

### Sessions and limits

<details>
<summary>Sessions and limits (3)</summary>

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| Whitelist sessions (`whitelist`) | list | empty | When non-empty, only these sessions may download; takes priority over the blacklist |
| Blacklist sessions (`blacklist`) | list | empty | These sessions cannot use the download commands |
| Debounce seconds (`debounce_interval`) | int | 60 | Ignores repeated requests for the same comic within this window; 0 disables it |

</details>

### Download channels

<details>
<summary>JM comic channel options (10)</summary>

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| Enable (`enable`) | bool | true | When off, `/jm` reports the channel as disabled |
| Client type (`client_type`) | string | api | API / Web / Custom API / Custom Web; `api` is more compatible, the web client is faster but region-restricted |
| Custom API domains (`api_domains`) | text | 4 common API domains | One per line, **tried top to bottom** (next one on failure) |
| Custom web domains (`html_domains`) | text | 7 common web domains | One per line, tried top to bottom |
| Request timeout (`request_timeout`) | int | 30 | Timeout per request; a smaller value gives up on bad nodes faster |
| Image format (`image_suffix`) | string | .jpg | Extension used when saving images |
| Per-request retries (`retry_times`) | int | 5 | Retries after a single failed request; a smaller value gives up on bad nodes faster |
| Concurrent chapters (`max_concurrent_photos`) | int | 3 | Chapters downloaded in parallel |
| Concurrent images (`max_concurrent_images`) | int | 5 | Images downloaded in parallel per chapter |
| Use proxy (`use_proxy`) | bool | false | Uses the proxy chain plugin proxy → AstrBot proxy → direct; JM has domestic mirrors, so usually no proxy is needed |

</details>

### nhentai channel

<details>
<summary>nhentai channel settings (7)</summary>

| Setting | Type | Default | Description |
|---------|------|---------|-------------|
| Enable (`enable`) | bool | true | When off, `/nh` reports the channel as disabled |
| Site domains (`site_domains`) | text | nhentai.net | One per line, **tried top to bottom**; the official API and the page fallback share this list, so a mirror goes here |
| Image domains (`image_domains`) | text | empty | Empty means the image host list is fetched from the site (recommended); fill one per line to force the first |
| Request timeout (`request_timeout`) | int | 30 | Timeout per request; the site is far away for most networks, so keep it generous |
| Concurrent images (`max_concurrent_images`) | int | 5 | Images downloaded in parallel; the image host dislikes high concurrency, 3-8 works best |
| Retries per image (`retry_times`) | int | 2 | Retries for a single image; when the image host pushes back, that page is re-fetched through a second path |
| Use proxy (`use_proxy`) | bool | true | Uses the proxy chain plugin proxy → AstrBot proxy → direct; most networks need one to reach nhentai |

</details>

### PicaACG channel

<details>
<summary>PicaACG channel settings (8)</summary>

| Setting | Type | Default | Description |
|---------|------|---------|-------------|
| Enable (`enable`) | bool | true | When off, `/pica` reports the channel as disabled |
| PicaACG account email (`account`) | string | empty | **Required**: PicaACG has no anonymous access, so episode and image lists need a login |
| PicaACG password (`password`) | string | empty | A throwaway account is recommended; the password only lives in the plugin config |
| Signature key (`secret_key`) | string | public app key | Only change it if the upstream signing scheme changes |
| Request timeout (`request_timeout`) | int | 30 | Timeout per request; the API is slow on some networks, keep it at 15 or more |
| Per-request retries (`retry_times`) | int | 3 | Only server errors and rate limits are retried; 0 disables retries |
| Concurrent images (`max_concurrent_images`) | int | 5 | Images downloaded in parallel; 3-6 works best |
| Use proxy (`use_proxy`) | bool | true | Uses the proxy chain plugin proxy → AstrBot proxy → direct; most networks need one to reach PicaACG |

</details>

### Hanime1 channel

<details>
<summary>Hanime1 channel settings (8)</summary>

| Setting | Type | Default | Description |
|---------|------|---------|-------------|
| Enable (`enable`) | bool | true | When off, `/hm` reports the channel as disabled |
| Site domains (`site_domains`) | text | hanimeone.me, hanime1.me | One per line, **tried top to bottom**; the comic section now lives on hanimeone.me, hanime1.me only keeps older works |
| Image host (`image_domains`) | text | empty | **Usually leave empty**: the host from the page is used, and unreachable ones are swapped automatically; fill it only when none work, e.g. i.nhentai.net |
| Reader proxy (`fetch_proxy`) | string | https://r.jina.ai/ | Pages are fetched through this public reader service by default (images still come straight from the image host); clear it to talk to the site directly only |
| Request timeout (`request_timeout`) | int | 30 | Timeout per request; the site can be slow behind Cloudflare, too small a value causes false failures |
| Concurrent images (`max_concurrent_images`) | int | 5 | Images downloaded in parallel; the image host dislikes high concurrency, 3-8 works best |
| Per-request retries (`retry_times`) | int | 2 | Retries per image; when the image host throttles, a single image is refetched through the browser-like scraper |
| Use proxy (`use_proxy`) | bool | true | Uses the proxy chain plugin proxy → AstrBot proxy → direct; a different proxy exit IP is usually what gets past Cloudflare |

</details>
