<div align="center">
<img src="logo.png" alt="Comic Parser" width="160" />
<h1>Comic Parser</h1>
<p><strong>Multi-channel comic downloader with built-in search: find a work, download the whole comic, then pack and send it in one pipeline — optional password encryption</strong></p>
<p><sub>jmcomic &nbsp;&nbsp; nhentai &nbsp;&nbsp; search &nbsp;&nbsp; wnacg</sub></p>
<p><a href="README.md">中文</a> &nbsp;/&nbsp; <strong>English</strong></p>
</div>

## Features

- **Just paste the link**: `/comic <link>` figures out the site on its own and downloads the whole comic, so you do not have to remember a command per platform
- **Download by ID**: use a channel command (`/jm`, `/nh`, `/pica`, `/hm`, `/ssmh`, `/eh`) when you already have the ID
- **Search right in the chat**: `/comic sou <keyword>` searches every channel that supports search in one go, `/jm sou` searches a single channel; reply with the number of a hit to download it, and page through longer result sets
- **Multiple channels**: JM, nhentai, PicaACG, Hanime1, WNACG and E-Hentai; E-Hentai needs no cookie for the 1280px resampled images, and originals are used once a cookie is added (charged against your site quota)
- **Unified packing**: every download becomes a ZIP archive, a PDF or a long vertical image, or can simply stay on disk; a format word at the end of the command overrides it for that one download
- **Encryption**: set one pack password and it applies, ZIP with AES and PDF with AES-256
- **Visible progress**: the bot reacts on your command message by default (downloading → done / failed), and you can switch to text progress or turn it off
- **Pause at the deadline, resume later**: a request that runs too long stops and keeps the images it already fetched, and `/comic retry` continues from there
- **Queueing and limits**: a global concurrency limit plus per-comic timeout and retry, so several users cannot overload the host
- **Session control**: whitelist / blacklist decide who may download, and debounce stops the same comic being requested twice
- **Fastest domain first**: with several site domains or image hosts, the plugin picks the fastest one before downloading
- **Cover image after each download** (off by default): every finished download sends one cover image; turn on "Censor the cover" if you want the explicit parts blurred. With censoring on, a cover rejected by the platform (invisible in the group, a send failure in the log) is re-censored across the whole image and sent once more
- **Censoring lab** (plugin details page → "Cover censoring / scoring lab"): drop a few images in to see the explicitness score and the detected boxes, then try masking styles, block size, padding and the whole-image threshold on the spot — no need to download a whole comic just to tune cover censoring. All model presets (nano / small / medium × fp32 / int8) are listed there too: pick a missing one, hit "download", and it is fetched with the plugin's own sources and proxy
- **Automatic cleanup**: temporary files are deleted right after sending

## Commands

| Command | Permission | Description |
|---------|------------|-------------|
| `/comic <link>` | Everyone (subject to whitelist/blacklist) | Paste the full link of a work page; the site is detected automatically and the whole comic is downloaded; an optional pack format may follow the link |
| `<channel command> <ID or link>` | Everyone (subject to whitelist/blacklist) | Downloads one comic from that channel only. The channel commands are `/jm`, `/nh`, `/pica`, `/hm`, `/ssmh`, `/eh` (full names `/jmcomic`, `/nhentai`, `/picacg`, `/hanime1`, `/wnacg`, `/ehentai`; `/ha`, `/hanime` and `/wn` work too); `/pica` needs a PicaACG account in the channel settings |
| `/eh <gid:token or link>` | Everyone (subject to whitelist/blacklist) | An E-Hentai ID has two parts, gid and token, so a bare number is rejected with a hint (anonymous 1280px; originals need a cookie) |
| `/comic sou <keyword>` | Everyone (subject to whitelist/blacklist) | Search every channel that supports search at once; hits are numbered continuously; type prefixes such as `tag:` and `author:` plus `-` exclusions are supported |
| `<channel command> sou <keyword>` | Everyone (subject to whitelist/blacklist) | Search that channel only; `sou` can also be written as `search`, `搜索` or `搜` |
| `/comic <number>` (same for `/<channel command> <number>`) | Everyone (subject to whitelist/blacklist) | Download the search hit with that number, from the channel the hit belongs to |
| `/comic 下一页` (also `/comic next`; go back with `上一页` or `prev`) | Everyone (subject to whitelist/blacklist) | Turn the page of the latest search in this session; numbers continue across pages |
| `<download command> <format word>` | Everyone (subject to whitelist/blacklist) | Sets the pack format for this one download, overriding the "Pack format" setting; the values are `zip` / `pdf` / `long_img` / `none` (save locally only), and the Chinese words `压缩包` / `文档` / `长图` / `本地` work too |
| `/comic stop` (same for `/jm stop` and other channel commands; also `取消`, `停止`) | Everyone | Cancels the downloads of your own session, including requests still queueing; images already fetched are kept, so `/comic retry` resumes. Admins can use `/comic stop 全部` to cancel every session |
| `/comic status` (also `状态`, `队列`) | Everyone (admins can use `/comic status 全部` for every session) | Shows the downloads running and queueing in this session: live progress, bytes fetched and elapsed time for running ones, queue position for waiting ones; read-only, it never interrupts a download |
| `/comic retry` (also `重试`, `继续`) | Everyone (subject to whitelist/blacklist) | Continues the last comic this session downloaded, so you do not have to scroll back after a pause, an interruption or a failure; an added format word (`/comic retry zip`) changes the format for this retry only. Only works under `/comic`. The record is stored on disk, so it survives a plugin reload |
| `/漫画帮助` | Everyone | Show available channels, pack format, password and progress settings |
| `/漫画网站` (also `/comicsites`) | Everyone | List every supported site's homepage plus a sample work-page link with `<ID>` as a placeholder, so you can browse for comics and find IDs |

- `/comic` only accepts full links (a work page address with a domain) or a number from the latest search: a bare ID or a PicaACG `pica://` share cannot tell which site it belongs to, so use the matching channel command instead. Channel commands do not pin the site either: a nhentai link after `/jm` downloads that nhentai gallery, and the download summary states which site was actually used.
- The short commands (`/jm`, `/nh`, `/pica`, `/hm`, `/ssmh`, `/eh`) are what you will use day to day, and they are what the bot shows in its replies; the full site names work exactly the same way.
- To use a different format for one download, put the format word at the very end of the command: `/jm 123456 pdf`, `/comic <link> long_img`, `/jm 1 none`. It only affects that download and never changes the setting; while searching with `sou`, the trailing word still counts as part of the keyword.
- Search hits show only the title, author/page count, a continuous number and the ID; the download example is shown once at the end. Send `/comic 1` (or `/jm 1`) to download the first hit. A new search replaces the numbers, and they stay valid for 10 minutes. When there is more than one page, the footer shows a hint such as `下一页：/jm 下一页`; paging first uses results already fetched and only asks the site for its next page when that buffer runs out, and numbers continue across pages.
- An E-Hentai ID has two parts, gid and token (e.g. `4306312:0123456789`). `gid:token`, `gid/token` and `gid-token` are all accepted, with or without a trailing slash, and copying the gallery link from your browser's address bar is the easiest way. Do not know where to find the ID? Send `/漫画网站` to see each site's sample work-page link (`<ID>` placeholder). A search hit is not a promise that it can be downloaded (it may need a login, be delisted, or be region-locked).

Examples:

```
/comic https://nhentai.net/g/123456/      # pasting a link is the quickest way; the site is detected
/jm https://18comic.vip/album/123456/     # channel commands accept links too
/jm 123456                                # a bare ID works as well
/comic sou tentacles                      # search every channel that supports search
/jm sou tag:"big breasts" -tag:NTR        # search one channel with a type filter
/comic 1                                  # download hit 1 by its number
/jm 下一页                                # turn the page of the results
/jm 123456 pdf                            # export this one as PDF; the setting stays unchanged
```

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
<summary>Packing and delivery (11 options)</summary>

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| Pack format (`pack_format`) | string | pdf | ZIP archive / PDF / long vertical image / keep on disk only |
| Pack password (`pack_password`) | string | empty | One global password: ZIP uses AES, PDF uses AES-256; empty means no encryption. A missing crypto dependency fails loudly instead of sending an unencrypted file |
| Filename template (`filename_template`) | string | id_short_title | Naming rule for the output; choices are "[short ID]title", "[short ID]title + password", "[ID]title", "[ID]title + password", "title[ID]", "title[ID] + password", "ID only", "title only" and "custom". Templates with "+password" put the password into the filename (it then shows up in chat history); with an empty pack password that segment is dropped |
| Filename length limit (`filename_max_length`) | int | 100 | Maximum characters in the output filename; longer titles are shortened (the trailing password segment and extension are kept). CJK titles are additionally capped by UTF-8 bytes so a name never exceeds the filesystem limit |
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
<summary>Cover and model (2 + 6 + 5 options)</summary>

**Cover**

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| Send cover after download (`send`) | bool | false | Send one cover image before the packed file after every successful download |
| Send cover caption separately (`caption`) | bool | false | Send one extra message after the cover image with this run's detection results only (explicitness, boxes found, share covered, threshold, whole-cover censoring / re-censored after the platform rejected it); title, author and pages are already in the download summary. Independent of "Send download summary" |

**Censoring**

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| Censor the cover (`censor.enable`) | bool | false | The local model **automatically locates exposed areas** (nipples, genitals, sex scenes) and blurs only those, keeping the rest of the image intact. **If the model is missing or the run fails, no cover is sent** (an uncensored image is never posted); a clean run still sends the cover. Censoring also reports the explicitness it computed into the caption from the same run. Only does anything with "Send cover after download" on |
| Censor style (`censor.style`) | string | mosaic | Mosaic / Gaussian blur / black bar |
| Mosaic block size (`censor.block`) | int | 14 | Larger blocks are blurrier; the blur radius is derived from it |
| Censor padding (`censor.padding`) | int | 12 | Expand every box so nothing leaks at the edges |
| Full-cover area threshold (`censor.full_area`) | float | 0.3 | Censor the whole cover once the union of boxes covers this share; 0 never does |
| Re-censor and resend when rejected (`censor.retry_strong`) | bool | true | When the platform treats the cover as explicit and drops it (upload succeeds, nothing appears in the group), censor the whole image again and resend once, only skipping if that fails too; the retry is blurrier than "Mosaic block size", so the cover turns out fairly unreadable. Off: skip on failure, no resend |

**Model and detection**

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| Model preset (`model.preset`) | string | m_fp32 | One of eight, see the two tables below; default realist medium + fp32. For manga / doujin covers pick one of the two anime presets: far better recall and much smaller weights |
| Auto-download weights (`model.auto_download`) | bool | true | Fetch into `models/` in the background at load time; skipped when present, failures are logged and retried on the next reload. Only downloads when both "Send cover after download" and "Censor the cover" are on |
| Custom download URL (`model.download_url`) | string | empty | Empty uses the plugin release (tag `weights-erax-onnx-v1`) and falls back to accelerator mirrors; multiple lines and `{filename}` supported |
| Detection threshold (`model.threshold`) | float | 0.03 | Confidence floor for censoring; lower is stricter and censors more |
| Inference size (`model.imgsz`) | int | 960 | Usually leave it alone: the shipped weights take a fixed 960 input, so this only matters after swapping in dynamic-size weights; any other value is treated as 960 with a log line |

**Preset reference**

| Preset | Full precision | int8 | Notes |
|--------|----------------|------|-------|
| nano (`n_*`) | 10MB | 3MB | Fastest, but coarser boxes and more whole-cover censoring |
| small (`s_*`) | 36MB | 10MB | About twice as fast as medium, slightly lower agreement |
| medium (`m_*`) | 77MB | 20MB | Most accurate (default); its int8 build is actually slower on CPU |

**Model families**

| Family | Presets | Classes | Best for | Source / licence |
|--------|---------|---------|----------|------------------|
| Realist (EraX) | `m_*` / `s_*` / `n_*` | anus, make_love, nipple, penis, vagina | Real photos, realistic art | EraX-NSFW-V1.0 (Apache-2.0), ONNX exported by this plugin |
| Anime (deepghs) | `anime_n` (12MB), `anime_s` (45MB) | nipple_f, penis, pussy | Manga pages, doujin covers | [anime_censor_detection](https://huggingface.co/deepghs/anime_censor_detection) (MIT) |

The anime presets are trained on anime / doujin art only: on the same set of manga covers they detected 10 of 11 images, against 6 of 11 for realist medium, and they run more than ten times faster. The trade-off is that they have **no anus or make_love classes**, so sex scenes and anal close-ups are missed. Pick whichever matches the covers you actually get.

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

Cover censoring and the explicitness figure rely on a local YOLO detection model, with weights from two sources:

- the realist presets are based on [EraX-NSFW-V1.0](https://huggingface.co/erax-ai/EraX-NSFW-V1.0) (Apache-2.0, classes anus / make_love / nipple / penis / vagina), nano / small / medium variants;
- the anime presets are based on [deepghs/anime_censor_detection](https://huggingface.co/deepghs/anime_censor_detection) (MIT per the repository, classes nipple_f / penis / pussy), trained purely on anime / doujin art, with noticeably better recall on manga covers.

**The plugin uses an ONNX build of those weights**: we export them from the upstream `.pt` (fixed 960 input, opset 17) and quantize the int8 variants separately, so they are not the same files as upstream. Upstream only ships `.pt`, and **the plugin does not accept `.pt`, so do not download from upstream and drop it in**.

You can tune it straight from the plugin details page: the "Cover censoring / scoring lab" view scores any image you drop in, shows where the boxes land and how each style looks, with the threshold and masking parameters adjustable on the spot. Missing model presets can be downloaded from that page too, using the same sources and proxy configured here.

Note that the weights live in the **plugin directory** under `models/`: uninstalling the plugin, or updating it from the marketplace, wipes them along with the plugin folder, so you would have to download them again.

To get the weights, turn on "Auto-download weights" under "Cover and model → Model and detection" (fetched from the plugin release), or download the matching `.onnx` from the plugin's [releases page](https://github.com/XTsat/astrbot_plugin_comic_parser/releases) and place it into the plugin's `models/` directory yourself. The automatic download tries GitHub first and falls back to built-in accelerator mirrors; you can also put your own mirror or object-storage links under "Custom download URL" (one per line, `{filename}` is supported). Detection only needs `onnxruntime` (~100MB), and it is **not installed with the plugin**: while both "Send cover after download" and "Censor the cover" are on, every load/reload checks for it and installs it in the background when missing — reload afterwards and it is ready. If the install cannot run (no network, no pip), the plugin still downloads and sends files, it just skips the cover; you can also run `pip install onnxruntime` yourself.

Censoring is off by default, so you can receive covers without any model (sent as-is, with no detection run and no explicitness figure). With "Censor the cover" on, a model is mandatory: when the weights are missing or the run fails, **that cover is not sent** and you get a notice — an uncensored image is never posted. "Auto-download weights" is on by default and fetches in the background at load time once both "Send cover after download" and "Censor the cover" are on, or you can drop the `.onnx` into `models/` yourself.

### Troubleshooting

**Why did I still receive the file in chat with "upload to group files" on, or why did it land in the group file root?** A failed upload shows its error in the message, but two causes are beyond what the plugin can tell: the protocol backend cannot read the plugin's temp file (when the plugin and the backend are not on the same machine or do not share the same mounted directory, that path simply does not exist for the backend), or the bot account has no upload permission / the group has no target folder and one cannot be created (creating a group file folder requires the bot to be a group admin or owner). In those cases the file is sent in chat or uploaded to the root instead, so the download is not wasted. Let the backend read the plugin's temp directory, make the bot an admin, or create the "漫画" folder yourself in the group files.

**Why does a JM search keep timing out?** A direct JM search can take 10–20 seconds, while the per-channel search timeout defaults to 10 seconds. If a global proxy is already configured, turn on "use proxy" in the JM channel settings; with a proxy it took about 1–3 seconds in testing. You can also raise the per-channel search timeout to 15–20 seconds, or search just that channel with `/jm sou`.

**Is the cover censored too much, or does something still show?** The area is driven by "Detection threshold" and "Full-cover area threshold": when something still shows, lower the threshold, raise "Censor padding", or lower the full-cover area threshold (more likely to censor the whole cover); when too much is covered, adjust the other way, and you can switch the style to blur or a black bar. A fully censored cover usually means the detected area really does cover most of the image (0.3 by default; 0.6 or 0.8 is gentler, 0 hides the full-cover fallback). An untouched cover usually means nothing was detected — manga confidences are low, so try lowering the threshold to 0.02. With "Send cover caption separately" on, every cover caption reports that run's explicitness, the number of boxes found, the share they cover and the threshold used — tune against those numbers. **If the cover never shows up in the group but the log reports a send failure**, the platform classified the image as explicit and dropped it: the plugin re-censors the whole image and sends it once more, and if that still fails it skips the cover (the file itself is unaffected).
