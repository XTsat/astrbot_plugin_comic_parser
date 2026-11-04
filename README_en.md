<div align="center">
<h1>Comic Parser</h1>
<p><strong>Multi-channel comic downloader: paste a work link or an ID to grab a whole comic, then pack and send it in one pipeline — optional password encryption</strong></p>
<p><sub>jmcomic &nbsp;&nbsp; nhentai &nbsp;&nbsp; PicACG &nbsp;&nbsp; WNACG</sub></p>
<p><a href="README.md">中文</a> &nbsp;/&nbsp; <strong>English</strong></p>
</div>

## Features

- **Just paste the link**: `/comic <link>` figures out the site on its own and downloads the whole comic, so you do not have to remember a command per platform
- **Download by ID**: hand over a comic ID or link and get the whole comic
- **Unified packing**: every download is packed as a ZIP archive, a PDF, or a long vertical image — or simply kept on disk without sending
- **Unified encryption**: one global password. ZIP uses AES, PDF uses AES-256. If a password is set but the encryption library is missing, the plugin fails loudly instead of quietly sending an unencrypted file
- **Visible progress**: by default the bot reacts on your command message (❤️ downloading → OK done / 🔨 failed) and skips the extra "download started" message; you can switch to text progress or turn it off
- **Optional result block**: the summary sent with the file (title / author / image count / elapsed) can be turned off
- **Session control**: whitelist / blacklist decide who may download, debounce stops the same comic being requested twice
- **Queued tasks**: global concurrency limit plus per-comic timeout and retry, so several users cannot overload the host
- **Automatic cleanup**: temporary files are deleted right after sending
- **Straight into group files**: turn on "upload to group files" and the finished file lands in the current group's files (optionally in a folder — if the group lacks that folder, the bot creates it when it has the rights and otherwise uses the group file root). QQ already posts a file message for it, so the bot does not send it again in the chat; only when the upload fails does it send the file directly

## Commands

| Command | Permission | Description |
|---------|------------|-------------|
| `/comic <link>` | Everyone (subject to whitelist/blacklist) | Paste the full link of a work page; the site is detected automatically and the whole comic is downloaded (works for every channel below) |
| `/jm <ID or link>` (full name `/jmcomic`) | Everyone (subject to whitelist/blacklist) | Download a whole JM comic |
| `/nh <ID or link>` (full name `/nhentai`) | Everyone (subject to whitelist/blacklist) | Download a whole nhentai gallery |
| `/pica <ID or link>` (full name `/picacg`) | Everyone (subject to whitelist/blacklist) | Download a whole PicaACG comic (needs a PicaACG account in the channel settings) |
| `/hm <ID or link>` (full name `/hanime1`, also `/ha`, `/hanime`) | Everyone (subject to whitelist/blacklist) | Download a whole Hanime1 comic |
| `/ssmh <ID or link>` (full name `/wnacg`, also `/wn`) | Everyone (subject to whitelist/blacklist) | Download a whole WNACG (Shenshi Manhua) gallery |
| `/漫画帮助` | Everyone | Show available channels, pack format, password and progress settings |

> When you just want one comic, pasting its link into `/comic` is the quickest way; when you only have an ID (or the link is not a work page), use the matching channel command.
> Channel commands do not pin the site either: a nhentai link after `/jm` downloads that nhentai gallery, and the download summary states which site was actually used.
> The short commands (`/jm`, `/nh`, `/pica`, `/hm`, `/ssmh`) are what you will use day to day, and they are what the bot shows in its replies; the full site names work exactly the same way.

Examples:

```
/comic https://18comic.vip/album/123456/
/comic https://nhentai.net/g/123456/
/comic https://manhuapica.com/comic/5f1e2d3c4b5a69788796a5b4
/comic https://hanime1.me/comic/100445
/comic https://www.wnacg.site/photos-index-aid-401500.html
/jm 123456
/jm https://18comic.vip/album/123456/
/nh 123456
/nh https://nhentai.net/g/123456/
/pica 5f1e2d3c4b5a69788796a5b4
/hm 100445
/hm https://hanime1.me/comic/100445
/ssmh 401500
/ssmh https://www.wnacg.site/photos-index-aid-401500.html
```

> `/comic` only accepts full links (a work page address with a domain): a bare ID or a PicaACG `pica://` share cannot tell which site it belongs to, so use the matching channel command instead.

## Configuration

### Packing and delivery

<details>
<summary>Packing and delivery (10)</summary>

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| Pack format (`pack_format`) | string | zip | One of zip / pdf / long_img / none (keep locally, do not send) |
| Pack password (`pack_password`) | string | empty | Global encryption password; empty means no encryption |
| Filename template (`filename_template`) | string | id_title | Naming pattern for the output file. Presets: [id]title / [id]title+password / title[id] / title[id]+password / ID only / title only / custom (the first four bracket the ID). Presets marked "+password" write the password into the filename (visible in chat) |
| Custom filename template (`filename_custom`) | string | empty | Only used when the template is "custom", e.g. `{id}-{title}`; placeholders `{title}` `{id}` `{author}` `{password}`; the `(password)` part disappears when no password is set |
| Local save directory (`download_dir`) | string | downloads | Only used by the "keep locally" mode; relative paths are based on the plugin data directory |
| Upload to group files after download (`upload_to_group_file`) | bool | false | Files downloaded in a group are uploaded to that group's files and no longer sent again in the chat; private chats are skipped and a failed upload falls back to sending the file; OneBot (aiocqhttp, e.g. NapCat) only |
| Group file folder (`group_file_folder`) | string | 漫画 | If the group has a folder with this name the file goes inside it (case-insensitive); when it is missing the next option decides; empty means always the group file root |
| Create the group file folder when missing (`group_file_create_folder`) | bool | true | Lets the bot create the target folder before uploading (it must be a group admin or owner); if that fails the file simply goes to the root without an error, and turning this off means only existing folders or the root are used |
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

### WNACG channel

<details>
<summary>WNACG channel settings (6)</summary>

| Setting | Type | Default | Description |
|---------|------|---------|-------------|
| Enable (`enable`) | bool | true | When off, `/ssmh` reports the channel as disabled |
| Site domains (`site_domains`) | text | www.wnacg.buzz, www.wnacg.site | One per line, **tried top to bottom**; change this when the site moves to a new domain, without the `https://` prefix |
| Request timeout (`request_timeout`) | int | 30 | Timeout per request; the site can be slow behind Cloudflare, too small a value causes false failures |
| Concurrent images (`max_concurrent_images`) | int | 4 | Images downloaded in parallel; the image host is a separate domain, 2-8 works best |
| Per-image retries (`retry_times`) | int | 2 | Retries per image; when the image host throttles, a single image is refetched through the browser-like scraper |
| Use proxy (`use_proxy`) | bool | false | Uses the proxy chain plugin proxy → AstrBot proxy → direct; the site is reachable directly from mainland China in our tests, turn this on if it times out or is blocked |

</details>

## Others

### Troubleshooting

**Why did I still receive the file in the chat with "upload to group files" turned on?** Two cases fall back to sending the file directly: the upload failed this time (the reason is shown in the message), or the download did not happen in a group / the protocol backend has no group file support. A failed upload is usually an environment problem — when the plugin and the protocol backend do not share the same machine or the same mounted directory, the backend cannot read the plugin's temp path; a bot account without upload permission also fails.

**Why did the file end up in the group file root instead of the configured folder?** That happens when the group has no such folder and the bot could not create it: creating a group file folder requires the bot to be a group admin or owner, so a regular member cannot do it, and the plugin still uploads to the root instead of wasting the download. Make the bot an admin, or create the folder yourself in the group files.
