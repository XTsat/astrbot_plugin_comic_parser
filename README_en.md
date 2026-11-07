<div align="center">
<img src="logo.png" alt="Comic Parser" width="160" />
<h1>Comic Parser</h1>
<p><strong>Multi-channel comic downloader with built-in search: find a work, download the whole comic, then pack and send it in one pipeline — optional password encryption</strong></p>
<p><sub>jmcomic &nbsp;&nbsp; nhentai &nbsp;&nbsp; search &nbsp;&nbsp; wnacg</sub></p>
<p><a href="README.md">中文</a> &nbsp;/&nbsp; <strong>English</strong></p>
</div>

## Features

- **Just paste the link**: `/comic <link>` figures out the site on its own and downloads the whole comic, so you do not have to remember a command per platform
- **Download by ID**: hand over a comic ID or link and get the whole comic
- **Search right in the chat**: `/jm sou <keyword>` searches JM only, while `/comic sou <keyword>` searches every channel that supports search. Each channel is timed separately, so one slow site is skipped instead of holding up the whole command. Add a type prefix such as `tag:` or `author:` before the keyword, and prefix it with `-` to exclude. Hits are numbered continuously and each line only shows the title, author/page count and ID, with a single download example at the end; reply with the number to download (e.g. `/comic 1` or `/jm 1`). Longer result sets are split into pages: send `/comic 下一页` or `/jm 下一页` for the next page (`上一页` goes back; `next` / `prev` also work), numbers continue across pages, and everything stays valid for 10 minutes
- **E-Hentai channel**: `/eh <gid:token or link>` (full name `/ehentai`) downloads a whole E-Hentai gallery. Without a cookie it fetches the 1280px-wide resampled images anonymously; originals are only used once you add a cookie and keep "prefer originals" on, and each original is charged against the site's image quota by file size. The quota is counted per IP: when it runs out the download stops and keeps its breakpoint, so `/comic retry` continues — long galleries usually take a few passes
- **Unified packing**: every download is packed as a ZIP archive, a PDF, or a long vertical image — or simply kept on disk without sending
- **Format per download**: want a different format just this once? Put it at the end of the command (`/jm 123456 pdf`, `/comic <link> long_img`) and it applies to that one download only — no need to flip the setting back and forth
- **Unified encryption**: one global password. ZIP uses AES, PDF uses AES-256. If a password is set but the encryption library is missing, the plugin fails loudly instead of quietly sending an unencrypted file
- **Visible progress**: by default the bot reacts on your command message (❤️ downloading or searching → OK done / 🔨 failed) and skips the extra "download started" message; you can switch to text progress or turn it off. Download progress refreshes every 15 seconds, so even a chapter that takes minutes keeps reporting, and chapters and pages are shown together (e.g. "1/2 chapters · 12/50 pages"); a search across all channels also reports how many channels have answered, and pagination tells you when it has to fetch the next upstream page
- **Pause at the deadline, resume next time**: a request that runs too long stops first and keeps the images it already fetched; `/comic retry` continues from there without refetching those pages. Breakpoints expire after the configured time
- **Optional result block**: the summary sent with the file (title / author / image count / elapsed) can be turned off
- **Cover image after each download** (off by default): turn it on and every finished download sends one cover image before the file, with the explicit parts censored (mosaic / blur / black bar, on by default) using boxes from a local NSFW detection model, falling back to censoring the whole cover when too much of it is flagged; the caption with title / author / page count / explicitness follows "Send download summary"
- **Session control**: whitelist / blacklist decide who may download, debounce stops the same comic being requested twice
- **Fastest domain first**: with several site domains or image hosts, the plugin probes them all before downloading (response time plus a small download sample) and starts from the fastest one, so a slow mirror no longer makes you sit and wait; rankings are cached for a while instead of being measured on every comic. Probe timeout, slow-node threshold and cache lifetime are configurable, and the whole thing can be switched off
- **Queued tasks**: global concurrency limit plus per-comic timeout and retry, so several users cannot overload the host; while waiting you are told what the running download is doing and how far it got
- **Automatic cleanup**: temporary files are deleted right after sending
- **Straight into a folder in the group files**: on by default, the finished file lands in the target folder of the current group's files (named 漫画 by default); if the group lacks that folder the bot creates it when it has the rights and only falls back to the group file root when it cannot. QQ already posts a file message for it, so the bot does not send it again in the chat; only when the upload fails does it send the file directly

## Commands

| Command | Permission | Description |
|---------|------------|-------------|
| `/comic <link>` | Everyone (subject to whitelist/blacklist) | Paste the full link of a work page; the site is detected automatically and the whole comic is downloaded (works for every channel below); an optional pack format can follow the link, see "Pack format per download" below |
| `/comic sou <keyword>` | Everyone (subject to whitelist/blacklist) | Search every channel that supports search at once; type prefixes such as `tag:` and `author:` plus `-` exclusions are supported; hits are numbered continuously, and `/comic <number>` downloads the matching one |
| `/jm sou <keyword>` (same for `/nh`, `/pica`, `/hm`, `/ssmh`, `/eh`) | Everyone (subject to whitelist/blacklist) | Search that channel only; `sou` can also be written as `search`, `搜索` or `搜`; type prefixes and `-` exclusions are supported; after a search, send `/<channel command> <number>` to download |
| `/jm 下一页` (also `/jm next`; go back with `/jm 上一页` or `/jm prev`) | Everyone (subject to whitelist/blacklist) | Turn the page of the latest search in this session; `/comic 下一页` and `/comic 上一页` work the same way; numbers continue across pages and still download by number |
| `/jm <ID or link>` (full name `/jmcomic`) | Everyone (subject to whitelist/blacklist) | Download a whole JM comic |
| `/nh <ID or link>` (full name `/nhentai`) | Everyone (subject to whitelist/blacklist) | Download a whole nhentai gallery |
| `/pica <ID or link>` (full name `/picacg`) | Everyone (subject to whitelist/blacklist) | Download a whole PicaACG comic (needs a PicaACG account in the channel settings) |
| `/hm <ID or link>` (full name `/hanime1`, also `/ha`, `/hanime`) | Everyone (subject to whitelist/blacklist) | Download a whole Hanime1 comic |
| `/ssmh <ID or link>` (full name `/wnacg`, also `/wn`) | Everyone (subject to whitelist/blacklist) | Download a whole WNACG (Shenshi Manhua) gallery |
| `/eh <gid:token or link>` (full name `/ehentai`) | Everyone (subject to whitelist/blacklist) | Download a whole E-Hentai gallery; the ID has two parts (gid plus token), so a bare number is rejected with a hint (anonymous 1280px; originals need a cookie) |
| `/jm 123456 pdf` (same for any download command, `/comic` and search hit numbers) | Everyone (subject to whitelist/blacklist) | Sets the pack format for this one download, overriding the "Pack format" setting; the values are `zip` / `pdf` / `long_img` / `none` (save locally only), and the Chinese words `压缩包` / `文档` / `长图` / `本地` work too. Without it the configured format is used |
| `/comic stop` (also `/jm stop`, `/nh stop`, …) | Everyone | Cancels the downloads of your own session, including requests still queueing; images already fetched are kept, so `/comic retry` resumes. Admins can use `/comic stop all` to cancel every session's downloads |
| `/comic status` (also `/comic 状态`, `/comic 队列`) | Everyone (admins can use `/comic status 全部` for every session) | Shows the downloads running and queueing in this session: live progress, bytes fetched and elapsed time for running ones, queue position for waiting ones; read-only, it never interrupts a download |
| `/comic retry` (also `/comic 重试`, `/comic 继续`) | Everyone (subject to whitelist/blacklist) | Continues the last comic this session downloaded: after a pause, an interrupted download or a failure, send this instead of scrolling back for the original command; an added format word (`/comic retry zip`) changes the format for this retry only. Only works under `/comic`, not with channel commands |
| `/漫画帮助` | Everyone | Show available channels, pack format, password and progress settings |
| `/漫画网站` (also `/comicsites`) | Everyone | List every supported site's homepage plus a sample work-page link with `<ID>` as a placeholder, so you can browse for comics and find IDs; you can also search in chat with `/comic sou` |

> When you just want one comic, pasting its link into `/comic` is the quickest way; when you only have an ID (or the link is not a work page), use the matching channel command.
> Channel commands do not pin the site either: a nhentai link after `/jm` downloads that nhentai gallery, and the download summary states which site was actually used.
> The short commands (`/jm`, `/nh`, `/pica`, `/hm`, `/ssmh`, `/eh`) are what you will use day to day, and they are what the bot shows in its replies; the full site names work exactly the same way.

Examples:

```
/comic https://18comic.vip/album/123456/
/comic https://nhentai.net/g/123456/
/comic https://manhuapica.com/comic/5f1e2d3c4b5a69788796a5b4
/comic https://hanime1.me/comic/100445
/comic https://www.wnacg.site/photos-index-aid-401500.html
/comic https://e-hentai.org/g/4306312/0123456789/
/jm 123456
/jm https://18comic.vip/album/123456/
/nh 123456
/nh https://nhentai.net/g/123456/
/pica 5f1e2d3c4b5a69788796a5b4
/hm 100445
/hm https://hanime1.me/comic/100445
/ssmh 401500
/ssmh https://www.wnacg.site/photos-index-aid-401500.html
/eh 4306312:0123456789
/eh https://e-hentai.org/g/4306312/0123456789/
/comic sou tentacles
/comic sou tag:"big breasts" -tag:NTR corruption
/comic 1
/comic 下一页
/jm 123456 pdf
/comic https://nhentai.net/g/123456/ long_img
/jm 123456 none
/jm sou 無職転生
/jm sou tag:"big breasts"
/jm 1
/jm 下一页
/nh sou tentacles
```

> To use a different format for one download, put the format word at the very end of the command: `/jm 123456 pdf`, `/comic <link> long_img`, `/jm 1 none`. The values are those of the "Pack format" setting — `zip` / `pdf` / `long_img` / `none` (the Chinese words `压缩包` / `文档` / `长图` / `本地` work as well). It only affects that download and never changes the setting; while searching with `sou`, the trailing word still counts as part of the keyword.
> Search hits show only the title, author/page count, a continuous number and the ID; the download example is shown once at the end. Send `/comic 1` (or `/jm 1`) to download the first hit, and the channel is picked automatically from the result. A new search replaces the numbers, and they stay valid for 10 minutes.
> When there is more than one page, the footer shows a next-page hint such as `下一页：/jm 下一页`; `上一页` (or `prev`) goes back. Paging first uses results already fetched and only asks the site for its next page when that buffer runs out, so flipping a page does not run a fresh search. Numbers continue across pages: after moving to page 2, `/jm 9` still downloads the ninth hit.
> `/comic` only accepts full links (a work page address with a domain) or a number from the latest search: a bare ID or a PicaACG `pica://` share cannot tell which site it belongs to, so use the matching channel command instead.
> An E-Hentai ID has two parts, gid and token (e.g. `4306312:0123456789`); a bare number is rejected with a hint. `gid:token`, `gid/token`, and `gid-token` are all accepted, with or without a trailing slash. Copying the gallery link from your browser's address bar is the easiest way.
> Do not know where to find the ID? Send `/漫画网站` to see each site's homepage plus a sample work-page link (`<ID>` placeholder); once you find the comic there, replace `<ID>` with the number in the link.
> Search results show only title, author/page count and ID; a hit is not a promise that it can be downloaded (it may need a login, be delisted, or be region-locked). Open the work page to confirm, then download it by number or ID.

### Search filters

Add a type prefix before the search term; both positive and negative filters (prefix the field with `-`) are supported:

- `tag:` tag; `author:` author (also `artist:`); `actor:` character (also `character:`); `work:` title (also `title:`); `parody:` original work; `group:` circle; `language:` language
- The matching Chinese prefixes also work: `标签:`, `作者:`, `角色:`, `作品:`, `原作:`, `社团:`, `语言:`
- A prefix is only recognised at the start of a token, case-insensitive, with either a half-width or full-width colon. Quote values that contain spaces, e.g. `tag:"big breasts"`
- Any remaining words are still searched as ordinary keywords, so a type condition and extra keywords can be combined: `/comic sou tag:"big breasts" -tag:NTR corruption`

Coverage: JM supports native filtering for `tag` / `author` / `actor` / `work`; E-Hentai supports all seven fields through its native syntax (`tag` / `work` are approximated with bare words). nhentai, PicaACG, wnacg and Hanime1 only accept a single keyword upstream, so they search by keyword and say so in the results; exclusions have no effect on those channels.

A JM field search can only target one field at a time. When extra keywords, multiple fields or cross-field exclusions are present, JM falls back to its general search and says so in the results; E-Hentai can combine all of these natively.

## Configuration

### Download channels

<details>
<summary>JM comic channel options (10)</summary>

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| Enable (`enable`) | bool | true | When off, `/jm` reports the channel as disabled |
| Client type (`client_type`) | string | api | API / Web / Custom API / Custom Web; `api` is more compatible, the web client is faster but region-restricted |
| Custom API domains (`api_domains`) | text | 4 common API domains | One per line, next one is tried on failure; with "probe before download" on, the fastest one is tried first |
| Custom web domains (`html_domains`) | text | 7 common web domains | One per line, next one is tried on failure; with "probe before download" on, the fastest one is tried first |
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
| Site domains (`site_domains`) | text | nhentai.net | One per line, next one is tried on failure; with "probe before download" on, the fastest site goes first. The official API and the page fallback share this list, so a mirror goes here |
| Image domains (`image_domains`) | text | empty | Empty means the image host list is fetched from the site (recommended); you may also fill one per line. With several candidates the fastest measured one is used — this is what the whole download's speed depends on |
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
| Site domains (`site_domains`) | text | hanimeone.me, hanime1.me | One per line, next one is tried on failure; with "probe before download" on, they are probed directly and the fastest goes first. The comic section now lives on hanimeone.me, hanime1.me only keeps older works |
| Image host (`image_domains`) | text | empty | **Usually leave empty**: the host from the page is used, unreachable ones are swapped automatically, and with several candidates the fastest measured one wins; fill it only when none work, e.g. i.nhentai.net |
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

### E-Hentai channel

<details>
<summary>E-Hentai channel settings (10)</summary>

| Setting | Type | Default | Description |
|---------|------|---------|-------------|
| Enable (`enable`) | bool | true | When off, `/eh` reports the channel as disabled |
| Site domains (`site_domains`) | text | e-hentai.org, exhentai.org | One per line, next one is tried on failure; keep `e-hentai.org` first (works anonymously); `exhentai.org` needs a cookie and is skipped without one; with "probe before download" on, they are probed and the fastest goes first |
| Use proxy (`use_proxy`) | bool | true | Uses the proxy chain plugin proxy → AstrBot proxy → direct; most mainland networks need one to reach either site |
| Request timeout (`request_timeout`) | int | 30 | Timeout per request; the site is overseas and occasionally flaky, so too small a value causes false failures |
| Request interval (`request_interval`) | float | 1.0 | Minimum seconds between page requests (work page and image page); the site is sensitive to paging speed, so a smaller value can trigger a temporary ban (1 second by default) |
| Concurrent images (`max_concurrent_images`) | int | 3 | Images downloaded in parallel; the site counts image quota per IP, so 2-4 works best |
| Retries per image (`retry_times`) | int | 2 | Retries per image after a server error or network hiccup; a spent quota (509) and a login wall are not retried — the download stops and keeps the breakpoint |
| Max pages (`max_pages`) | int | 0 | Pages per download, 0 means the whole gallery; for large galleries set a cap together with segmented resume so a single request does not run too long |
| Prefer originals (`download_original`) | bool | true | Only effective with a cookie: originals are tried first and charged against the site's quota at 10× the file size in MB; a page whose original fails falls back to its 1280px version |
| Cookie (`cookie`) | string | empty | Optional. Account credential, do not leak; empty means anonymous 1280px resampled images. Fill `ipb_member_id` and `ipb_pass_hash` (plus `igneous` for exhentai), separated by semicolons, to fetch originals |

</details>

> The site's image quota is counted per IP: when it runs out the download stops and keeps its breakpoint, so `/comic retry` continues; a cookie does not bypass the quota of a shared exit IP. Originals cost far more quota than resampled images, so turn "prefer originals" off if you only want to read.

### Sessions and limits

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| Whitelist sessions (`whitelist`) | list | empty | When non-empty, only these sessions may download or search; takes priority over the blacklist |
| Blacklist sessions (`blacklist`) | list | empty | These sessions cannot use download or search commands |
| Debounce seconds (`debounce_interval`) | int | 60 | Ignores repeated requests for the same comic within this window; 0 disables it |

### Download and network

<details>
<summary>Download and network (8 options)</summary>

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| Proxy (`proxy`) | string | empty | Used by any channel that enables "use proxy", and by the pre-download probe; empty falls back to AstrBot's own `http_proxy`, else direct |
| Progress style (`progress_style`) | string | emoji | Reaction / text / off; falls back to text when the backend cannot react |
| Send download summary (`show_result_summary`) | bool | true | Turn off to send the file only, without the title / author / image count / elapsed block |
| Concurrent downloads (`max_concurrent_tasks`) | int | 1 | Global limit; extra requests queue and show the running comic's progress |
| Per-comic timeout (`timeout`) | int | 1800 | Seconds per download attempt before it fails and retries |
| Retry times (`retry_times`) | int | 2 | Retries after a failed download; 0 disables |
| Continuous download per request (`segment_timeout`) | int | 900 | Stops at the deadline and keeps a breakpoint so `/comic retry` resumes; 0 disables segmentation |
| Breakpoint lifetime (`resume_ttl`) | int | 7200 | How long paused images are kept for resuming with `/comic retry`; 0 keeps none |

</details>

### Packing and delivery

<details>
<summary>Packing and delivery (10 options)</summary>

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| Pack format (`pack_format`) | string | pdf | ZIP archive / PDF / long vertical image / keep on disk only |
| Pack password (`pack_password`) | string | empty | One global password: ZIP uses AES, PDF uses AES-256; empty means no encryption. A missing crypto dependency fails loudly instead of sending an unencrypted file |
| Filename template (`filename_template`) | string | id_short_title | Naming rule for the output; templates with "+password" put the password into the filename |
| Custom filename template (`filename_custom`) | string | empty | Only used when "Filename template" is "custom", e.g. `{title}-{id_short}`; placeholders are documented in the README |
| Upload to group files (`upload_to_group_file`) | bool | true | After a group download the file goes into the target folder; QQ already posts a file message, so the chat is not spammed. Group chats + OneBot only |
| Group file folder (`group_file_folder`) | string | 漫画 | Only used when "Upload to group files" is on. Target folder name (case-insensitive); empty means the group file root |
| Auto-create group folder (`group_file_create_folder`) | bool | true | Only used when "Upload to group files" is on. Create the folder when missing (bot must be admin/owner), else fall back to the root |
| Local save directory (`download_dir`) | string | downloads | Where "keep on disk only" mode stores the files |
| Delete local files after sending (`auto_delete_after_send`) | bool | true | Removes temporary output after delivery; local-only mode never deletes |
| Clean temp on startup (`clean_temp_on_start`) | bool | true | Empties the temp directory at load time, removing leftovers from crashes; breakpoints are cleared too |

</details>

### Cover and model

<details>
<summary>Cover and model (6 + 6 options)</summary>

**Cover**

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| Send cover after download (`send`) | bool | false | Send one cover image before the packed file after every successful download |
| Censor the cover (`censor`) | bool | true | The local model **automatically locates exposed areas** (nipples, genitals, sex scenes) and blurs only those, keeping the rest of the image intact. **If the model is missing or the run fails, no cover is sent** (an uncensored image is never posted); a clean run still sends the cover |
| Censor style (`censor_style`) | string | mosaic | Mosaic / Gaussian blur / black bar |
| Mosaic block size (`censor_block`) | int | 14 | Larger blocks are blurrier; the blur radius is derived from it |
| Censor padding (`censor_padding`) | int | 12 | Expand every box so nothing leaks at the edges |
| Full-cover area threshold (`full_censor_area`) | float | 0.6 | Censor the whole cover once the union of boxes covers this share; 0 never does |

**Model and detection**

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| Model preset (`model.preset`) | string | m_fp32 | One of six, see below; default medium + fp32 |
| Auto-download weights (`model.auto_download`) | bool | false | Fetch into `models/` in the background at load time; skipped when present, failures are logged and retried on the next reload |
| Custom download URL (`model.download_url`) | string | empty | Empty uses the plugin release (tag `weights-erax-onnx-v1`) and falls back to accelerator mirrors; multiple lines and `{filename}` supported |
| Score the cover (`model.score`) | bool | true | Reports how explicit the cover is; skipped when no weights are available |
| Detection threshold (`model.threshold`) | float | 0.03 | Confidence floor shared by scoring and censoring; lower is stricter and censors more |
| Inference size (`model.imgsz`) | int | 960 | 960 is the sweet spot: 640 emits a whole-frame box (censoring everything), 1280 loses recall |

**Preset reference**

| Preset | Full precision | int8 | Notes |
|--------|----------------|------|-------|
| nano (`n_*`) | 10MB | 3MB | Fastest, but coarser boxes and more whole-cover censoring |
| small (`s_*`) | 36MB | 10MB | About twice as fast as medium, slightly lower agreement |
| medium (`m_*`) | 77MB | 20MB | Most accurate (default); its int8 build is actually slower on CPU |

</details>

### Probe before download

<details>
<summary>Probe before download (4)</summary>

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| Enable probing (`enable`) | bool | true | Probes every candidate domain / image host at once (response time plus a small download sample) and starts from the fastest; when off, candidates are used in the order you typed them |
| Probe timeout per candidate (`timeout`) | int | 10 | Probes run concurrently and a candidate that misses the deadline counts as slow. Too short kills "slow but usable" sites (cross-border links often need more than 5 seconds); too long wastes time |
| Slow node threshold (`min_speed`) | int | 150 | Candidates measured below this speed (KB/s) are ranked last but never dropped; 0 ranks by response time only |
| Ranking cache lifetime (`cache_ttl`) | int | 600 | Ranks are reused for the same candidate set within this window instead of being re-measured on every comic; 0 re-measures every time |

</details>

### Search

<details>
<summary>Search (8)</summary>

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| Enable search (`enable`) | bool | true | When off, `/jm sou` and `/comic sou` report that search is disabled; downloading is unaffected |
| Per-channel search timeout (`timeout`) | int | 10 | Each channel is timed on its own; in an all-channel search a channel that misses this deadline is skipped while the other results are still shown |
| Results per page, single channel (`page_size`) | int | 5 | Hits shown per page when searching a single channel; paging continues at this same page size |
| Results per page, all channels (`aggregate_limit`) | int | 8 | Maximum hits shown per page by an all-channel search, taken round-robin so one channel cannot fill the message; paging continues at the same size |
| Result cache lifetime (`cache_ttl`) | int | 60 | Reuses the previous result for the same channel and keyword within this window; 0 disables caching |
| Search cooldown (`cooldown`) | int | 3 | Minimum seconds between two searches in the same session; 0 disables the limit |
| Auto-proxy retry on timeout (`auto_proxy`) | bool | true | When any channel misses the per-channel search timeout, retry once through the global proxy configured in the plugin; has no effect without a global proxy |
| Extra time for auto-proxy retry (`auto_proxy_timeout`) | int | 15 | The retry has its own time budget and does not consume the per-channel search timeout; only used when auto-proxy is on and a global proxy is configured |

</details>

## Others

### How the cover features are powered

Cover scoring and censoring rely on a local YOLO detection model. The weights are based on [EraX-NSFW-V1.0](https://huggingface.co/erax-ai/EraX-NSFW-V1.0) (Apache-2.0, classes anus / make_love / nipple / penis / vagina), nano / small / medium variants.

**The plugin uses an ONNX build of those weights**: we export them from the upstream `.pt` (fixed 960 input, opset 17) and quantize the int8 variants separately, so they are not the same files as upstream. Upstream only ships `.pt`, and **the plugin does not accept `.pt`, so do not download from upstream and drop it in**.

To get the weights, turn on "Auto-download weights" under "Cover and model → Model and detection" (fetched from the plugin release), or place the matching `.onnx` into the plugin's `models/` directory yourself. At runtime only `onnxruntime` (~200M) is required.

Without any model the plugin still downloads and still delivers the file, but it will **not send the cover** (censoring is on by default, and no cover is sent unless it was actually censored). Put the weights into `models/` first, or turn on "Auto-download weights"; if you just want to see covers without a model, turn "Censor the cover" off.

### Troubleshooting

**Why did I still receive the file in the chat with "upload to group files" turned on?** Two cases fall back to sending the file directly: the upload failed this time (the reason is shown in the message), or the download did not happen in a group / the protocol backend has no group file support. A failed upload is usually an environment problem — when the plugin and the protocol backend do not share the same machine or the same mounted directory, the backend cannot read the plugin's temp path; a bot account without upload permission also fails.

**The download stopped halfway and the message says `/comic retry` continues it — what does that mean?** A request that keeps downloading too long (continuous download per request, 15 minutes by default) pauses first and keeps the images in the temporary directory, so nothing is packed or sent this time; `/comic retry` resumes it without refetching the pages already saved, then packs and delivers as usual (re-sending the original command works the same way). `/comic retry` only knows the last download of this session, so if you downloaded something else in between, re-send the original command instead. The images expire after the breakpoint lifetime (2 hours by default), and a request after that starts from scratch; if "clean temp on startup" is on, reloading the plugin clears breakpoints too. If the message says "plugin reloaded, download interrupted", the plugin was reloaded or unloaded mid-download: the message also states how many pages and how many MB were saved before the stop, and whether those images are still there — with "clean temp on startup" off the breakpoint survives and `/comic retry` resumes, with it on those images are wiped and the download starts over.

**What does "PDF packing failed" or "N pages skipped because they could not be decoded" mean?** The first means the whole book was not packed: the images are kept in the breakpoint directory, so `/comic retry` only repacks them instead of downloading the book again. The second means a few individual pages could not be read: those pages are skipped, the PDF is missing them (the message says how many), and the remaining pages are packed and delivered as usual. If it keeps failing, send the error line mentioning "packing failed" from the log to the author.

**Why do some channels show no results or a timeout when searching?** An all-channel search times each channel separately (per-channel search timeout, 10 seconds by default); a channel that is slow or blocked is skipped with a note under the results while the others are still shown. Raise the timeout, or search one channel with `/jm sou` instead. The PicaACG channel also needs an account and password in its settings, and reports that explicitly when they are missing.

**Why does a JM search keep timing out?** A direct JM search can take 10–20 seconds, while the per-channel search timeout defaults to 10 seconds. If a global proxy is already configured, turn on "use proxy" in the JM channel settings; with a proxy the search takes about 1–3 seconds in testing. You can also raise the per-channel search timeout to 15–20 seconds.

After a search timeout, the plugin automatically retries through the global proxy configured in the plugin and notes "搜索超时后已用代理重试" in the results. This does not change the proxy setting used for downloads. The retry is on by default and can be disabled with "auto-proxy retry on timeout" in Search, or tuned with its extra-time setting.

**Why did I get "封面未发送：已开启给封面打码，但没有可用的 NSFW 模型"?** Censoring is enabled but no weights are available, and the plugin would rather skip the cover than post an uncensored image. Either drop the `.onnx` into the plugin's `models/` directory, or turn on "Auto-download weights" and reload; if you do not want the model yet, just turn "Censor the cover" off and covers will be sent as-is without an explicitness score.

**What if the weights will not download?** The plugin tries GitHub directly, then falls back to built-in accelerator mirrors. If all of them fail, drop the `.onnx` into the plugin's `models/` directory manually (file names map to the preset list above), or put your own mirror / object-storage link under "Custom download URL" — one per line, `{filename}` is supported.

**Why is one cover fully mosaic-ed while another is left untouched?** A whole-cover mosaic means the detected area really does cover most of the image — raise "Full-cover area threshold" to 0.8 to be gentler, or set it to 0 to never censor the whole cover. An untouched cover means nothing was detected: manga confidences are low, so try lowering "Detection threshold" to 0.02 or raising "Inference size" to 1280; if it keeps missing, this weight set simply does not fit your image source well.

**The cover is censored too much, or not enough?** Censoring is driven by "Detection threshold" and "Full-cover area threshold": lower the threshold, raise "Censor padding", or lower the area threshold (more likely to censor the whole cover) when something still shows; adjust the other way when too much is covered. You can also switch the style to blur or a black bar.

**Why did the file end up in the group file root instead of the configured folder?** That happens when the group has no such folder and the bot could not create it: creating a group file folder requires the bot to be a group admin or owner, so a regular member cannot do it, and the plugin still uploads to the root instead of wasting the download. Make the bot an admin, or create the folder yourself in the group files.
