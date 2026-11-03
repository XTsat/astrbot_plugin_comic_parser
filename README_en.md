<div align="center">
<h1>Comic Parser</h1>
<p><strong>Multi-channel comic downloader: download a whole comic by ID, then pack and encrypt it in one unified pipeline</strong></p>
<p><sub>Comic download &nbsp;&nbsp; Encrypted packing &nbsp;&nbsp; Multi-channel</sub></p>
<p><a href="README.md">中文</a> &nbsp;/&nbsp; <strong>English</strong></p>
</div>

## Features

- **Download by ID**: hand over a comic ID or link and get the whole comic
- **One command per channel**: `/jm 123456`; each channel keeps its own command, so they never get in each other's way
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
| `/jm <ID or link>` | Everyone (subject to whitelist/blacklist) | Download a whole JM comic |
| `/漫画帮助` | Everyone | Show available channels, pack format, password and progress settings |

Examples:

```
/jm 123456
/jm https://18comic.vip/album/123456/
```

## Configuration

### Packing and delivery

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| Pack format (`pack_format`) | string | zip | One of zip / pdf / long_img / none (keep locally, do not send) |
| Pack password (`pack_password`) | string | empty | Global encryption password; empty means no encryption |
| Filename template (`filename_template`) | string | id_title | Naming pattern for the output file. Presets: [id]title / [id]title+password / title[id] / title[id]+password / ID only / title only / custom (the first four bracket the ID). Presets marked "+password" write the password into the filename (visible in chat) |
| Custom filename template (`filename_custom`) | string | empty | Only used when the template is "custom", e.g. `{id}-{title}`; placeholders `{title}` `{id}` `{author}` `{password}`; the `(password)` part disappears when no password is set |
| Local save directory (`download_dir`) | string | downloads | Only used by the "keep locally" mode; relative paths are based on the plugin data directory |
| Delete local file after sending (`auto_delete_after_send`) | bool | true | Removes temporary files once sent |
| Clean temp on startup (`clean_temp_on_start`) | bool | true | Empties the temp directory when the plugin loads, clearing leftovers from crashes or forced restarts |

### Download control

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| Concurrent download tasks (`max_concurrent_tasks`) | int | 1 | Global task limit; extra requests queue up |
| Per-comic timeout (`download_timeout`) | int | 1800 | Seconds allowed for one whole comic before it counts as failed |
| Download retries (`download_retry_times`) | int | 2 | Retries after a failed download |
| Send result summary (`show_result_summary`) | bool | true | When off, only the file is sent without the title / author / image count / elapsed block; the keep-locally mode still reports the path |
| Progress style (`progress_style`) | string | emoji | `emoji` (default) / `text` / `none`; the emoji mode reacts ❤️ on the command message, then swaps to OK or 🔨. Private chats and clients without reaction support fall back to text progress automatically |
| Proxy address (`proxy`) | string | empty | Such as `http://host:port`; the channel must also enable "use proxy" |

### Sessions and limits

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| Whitelist sessions (`whitelist`) | list | empty | When non-empty, only these sessions may download; takes priority over the blacklist |
| Blacklist sessions (`blacklist`) | list | empty | These sessions cannot use the download commands |
| Debounce seconds (`debounce_interval`) | int | 60 | Ignores repeated requests for the same comic within this window; 0 disables it |

### Download channels

Channels are added on demand; a channel that is not added stays disabled. The JM comic channel is available today.

<details>
<summary>JM comic channel options (10)</summary>

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| Enable (`enable`) | bool | true | When off, `/jm` reports the channel as disabled |
| Client type (`client_type`) | string | api | `api` is more compatible; `html` is faster but region-restricted |
| API domains (`api_domains`) | text | 4 common API domains | One per line, **tried top to bottom** (next one on failure); clear it to let jmcomic fetch the latest domains |
| Web domains (`html_domains`) | text | 7 common web domains | One per line, tried top to bottom; clear it to let jmcomic fetch the latest domains |
| Request timeout (`request_timeout`) | int | 30 | Timeout per request; a smaller value gives up on bad nodes faster |
| Image format (`image_suffix`) | string | .jpg | Extension used when saving images |
| Per-request retries (`retry_times`) | int | 0 | 0 means the library default |
| Concurrent chapters (`max_concurrent_photos`) | int | 3 | Chapters downloaded in parallel |
| Concurrent images (`max_concurrent_images`) | int | 5 | Images downloaded in parallel per chapter |
| Use proxy (`use_proxy`) | bool | false | Uses the global proxy; when off, connections are explicitly direct |

</details>

## Other

### Requirements

Install the plugin from the plugin marketplace or the WebUI install page; AstrBot installs its dependencies automatically. PDF and long-image packing additionally need `pymupdf` and `Pillow`, and encrypted ZIP needs `pyzipper`.

### FAQ

- **"Dependency not installed" when downloading**: make sure the plugin dependencies finished installing, then reload the plugin
- **Encryption error after setting a password**: `pyzipper` is missing; install it and reload. The plugin will never fall back to sending an unencrypted file
- **Reactions show up in groups but not in private chats**: QQ private chats do not support emoji reactions, so the plugin falls back to text progress. Set the progress style to text to use it everywhere
- **The sent file is empty or tiny**: the upstream resource probably failed to load; retry later, or switch the progress style to text to watch the download advance
