# anki-sync-mcp

**A headless, sync-native Anki MCP sidecar for always-on AI agents.**

`anki-sync-mcp` lets an MCP client safely manage a real Anki collection without Anki
Desktop or AnkiConnect. It runs as an authenticated Streamable HTTP service, uses Anki's
collection and sync APIs, and synchronizes with AnkiWeb or a self-hosted Anki sync server.

Unlike desktop bridges, it is designed to run unattended in Docker. Writes are serialized,
idempotent, persisted across restarts, and synchronized before and after mutation. Destructive
and schema-changing tools are disabled by default and use preview tokens plus verified backups
when enabled.

It supports decks, notes, cards, tags, note types, templates, media, scheduling controls,
review history and analytics, backups, and explicit full-sync recovery.

## Quick start with Docker Compose

Requirements: Docker Compose, an HTTPS reverse proxy for the server's public hostname, and an
AnkiWeb or self-hosted sync account for each user.

```sh
git clone https://github.com/raslab/anki-sync-mcp.git
cd anki-sync-mcp

export MCP_PUBLIC_URL="https://anki.example.com"
export MCP_ALLOWED_HOSTS="anki.example.com"
docker compose up -d --build
docker compose exec anki-mcp anki-mcp-user add owner --sync-username "you@example.com"
docker compose exec anki-mcp anki-mcp-healthcheck
```

`anki-mcp-user add` prompts for the password you will sign in with. Point the reverse proxy at
port 8000 of the `anki-mcp` container, forwarding `/mcp`, `/oauth/login`, `/authorize`,
`/token`, `/register`, `/revoke`, `/setup` and `/.well-known/`. Then add `https://anki.example.com/mcp`
as a remote MCP server in your client. The client opens a sign-in page; enter your user id and
password. Once connected, call `anki_sync_login` once; the sync host key is stored in the
`anki_data` volume.

For local development without a proxy, publish the port with `ports: ["127.0.0.1:8000:8000"]`
and use `MCP_PUBLIC_URL=http://localhost:8000`; HTTP is accepted only for loopback hosts.

Do not run two servers, Anki Desktop, or any other process against the same data directory.
Keep destructive, schema, and full-sync feature flags disabled except during deliberate
maintenance.

## Configuration

Configuration is read from environment variables when the service starts. `compose.yaml`
supplies safe defaults. Variables written as `${NAME:-default}` can be overridden through the
shell or a Compose `.env` file. To change a fixed value, or use one of the settings not present
in the Compose file, add it under `services.anki-mcp.environment`.

### Server and access

| Variable | Default | Description |
| --- | --- | --- |
| `MCP_PUBLIC_URL` | required | Public HTTPS origin of the server, such as `https://anki.example.com`. It is the OAuth issuer and the base of every sign-in link. HTTP is accepted only for loopback hosts. |
| `MCP_HOST` | `0.0.0.0` | Address on which the HTTP server listens. |
| `MCP_PORT` | `8000` | HTTP listen port (`1`-`65535`). Keep this consistent with `expose`, any `ports` mapping, and the client URL. |
| `MCP_PATH` | `/mcp` | MCP endpoint path. It must be an absolute, static, non-root path and must not overlap `/health`. |
| `MCP_SCOPES` | `read,write,admin` | Comma-separated tool groups to register. Allowed values are `read`, `write`, `admin`, and `destructive`. Destructive tools require both the `destructive` scope and their feature flag. |
| `MCP_ALLOWED_HOSTS` | `testserver,localhost,localhost:*,127.0.0.1,127.0.0.1:*,anki-mcp,anki-mcp:*` | Comma-separated HTTP `Host` patterns accepted by MCP DNS-rebinding protection. Add the service name or hostname used by remote clients. |
| `MCP_ALLOWED_ORIGINS` | `http://localhost:*,http://127.0.0.1:*` | Comma-separated browser origins accepted by MCP transport security. Add explicit trusted origins when using a browser-based client. |
| `LOG_LEVEL` | `INFO` | Log level for the server and Uvicorn, such as `debug`, `info`, `warning`, or `error`. `debug` also logs diagnostics such as redirect URIs refused during OAuth client registration, and the media sync lifecycle (start, progress, wait, abort) under the `anki_mcp.media_sync` logger. Media sync completion is logged at `info` and failures at `warning` with an error code and exception type, never the backend message. |
| `MCP_OAUTH_REDIRECT_URIS` | `https://claude.ai/api/mcp/auth_callback,https://claude.com/api/mcp/auth_callback` | Comma-separated redirect URIs that clients may register. Loopback redirect URIs (`http://localhost`, `http://127.0.0.1` or `http://[::1]` with a port) are always accepted, so local clients such as Claude Code connect without being listed: the authorization code goes only to the machine that signed in, and PKCE stops anyone else from redeeming it. A client asking for any other redirect URI is refused, so a stranger cannot register a client that sends your authorization code to their own site. |

The `/health/live` and `/health/ready` endpoints need no sign-in. `/health/ready` reports how
many collections are open, never user names or paths.

### Users and sign-in

Every person is a user in `<ANKI_DATA_DIR>/users.json`, with their own collection at
`<ANKI_DATA_DIR>/users/<user id>/collection.anki2`. A personal install is just one with a
single user. Every tool, receipt, backup and confirmation token is scoped to the signed-in user.

Clients connect over OAuth: the server is its own authorization server, with dynamic client
registration, authorization code with PKCE, rotating refresh tokens and revocation. Signing in
means entering a user id and password on the server's `/oauth/login` page. Access tokens last an
hour and refresh tokens 30 days, renewed on every refresh, so an always-on agent that refreshes
at least monthly stays signed in after one browser sign-in. Tokens are stored only as SHA-256
hashes, in `<ANKI_DATA_DIR>/oauth.json`.

Manage accounts with `anki-mcp-user` (it reads `ANKI_DATA_DIR`, or takes `--file`):

- `invite <id> --email <address>` creates the user and emails them a setup link (see
  [Invitations](#invitations)). `--sync-host` points them at a self-hosted sync server instead
  of AnkiWeb; `--resend` sends a fresh link to someone who hasn't finished.
- `reset <id>` emails a link that lets the user choose a new password. Their old password keeps
  working until they use it.
- `add <id>` creates a user directly and prompts for their password. `--sync-username`, `--sync-host` (empty for
  AnkiWeb) and `--sync-password` set their sync account; the sync password can be removed from
  the file once `anki_sync_login` has succeeded.
- `passwd <id>` sets a new password. With `--hash`, it takes a line printed by
  `anki-mcp-hash-password`, so a user can choose a password on their own machine and send only
  its hash.
- `remove <id>` deletes the account at once, signing out its clients; the data directory is
  kept.
- `list` prints the user ids.

Changes take effect without a restart. User ids use `a-z`, `0-9`, `_` and `-`.

A collection opens on its user's first call and closes after `ANKI_COLLECTION_IDLE_SECONDS`
without one, so memory follows the number of active users, not registered ones.

### Invitations

`anki-mcp-user invite` sends the person a link to `/setup` on the server. It walks them through
three pages:

1. **Password.** They choose the password they will sign in with (at least 12 characters).
2. **AnkiWeb.** They sign in to AnkiWeb, or the sync server named in the invite. The password is
   used once and not stored; the server keeps only the login key AnkiWeb returns.
3. **Copy.** Their collection downloads with a progress bar (a percentage while cards download,
   then a running file count for media, whose total Anki does not know in advance). The page
   refreshes itself; they can close it and return with the same link.

They cannot sign in to an MCP client until the cards and all media have finished downloading.
If the copy fails, the page explains why and offers a retry that resumes where it stopped.

Links are single-use, stored only as hashes, and expire after `MCP_SETUP_LINK_HOURS`. A new
link replaces the emailed one after the password step, so the email stops working once used.
Email goes through your own mail server. Without `MCP_SMTP_HOST`, the command prints the link
for you to send yourself.

| Variable | Default | Description |
| --- | --- | --- |
| `MCP_SMTP_HOST` | empty | Mail server for setup and reset links. Empty prints links instead of sending them. |
| `MCP_SMTP_PORT` | `587` | Mail server port. |
| `MCP_SMTP_SECURITY` | `starttls` | `starttls`, `tls` (implicit TLS, usually port 465), or `none`. `none` is refused unless the host is loopback, because a setup link is as good as a password. |
| `MCP_SMTP_USERNAME` | empty | Mail server login, if it needs one. |
| `MCP_SMTP_PASSWORD_FILE` | unset | Path to a file holding the mail server password. |
| `MCP_SMTP_FROM` | required with `MCP_SMTP_HOST` | Sender address, such as `Anki MCP <anki@example.com>`. |
| `MCP_SETUP_LINK_HOURS` | `72` | How long a setup or reset link stays valid (up to `720`). A copy that has started keeps its link until it finishes. |

### Collection, storage and synchronization

Collections are SQLite databases with random IO, so keep `ANKI_DATA_DIR` on SSD.
`ANKI_MEDIA_ROOT` and `ANKI_BACKUP_ROOT` move the bulky media files and backups to slower,
cheaper storage such as an HDD. The server never moves existing media: if a collection's
`.media` folder already exists as a directory, move its contents yourself.

| Variable | Default | Description |
| --- | --- | --- |
| `ANKI_DATA_DIR` | `/data` | Holds `users.json`, `oauth.json` and one directory per user. Compose persists it in the `anki_data` volume. |
| `ANKI_MAX_OPEN_COLLECTIONS` | `100` | Most user collections open at once (`1`-`10000`). At the limit the least recently used idle one is closed; when none is idle, the call fails with `RESOURCE_LIMIT_EXCEEDED` and can be retried. Size it from memory: each open collection costs a few MB plus its SQLite cache. |
| `ANKI_COLLECTION_IDLE_SECONDS` | `900` | Close a user's collection after this many seconds without a call (`10`-`86400`). A collection with a media sync running stays open. |
| `ANKI_MEDIA_ROOT` | unset | Directory for media files, one subdirectory per user. The collection's `.media` folder becomes a symlink to it. |
| `ANKI_BACKUP_ROOT` | unset | Directory for verified backups, one subdirectory per user, instead of `backups/` next to the collection. |
| `ANKI_SYNC_ON_READ` | `false` | Synchronize before coordinated reads. Individual read tools can still request a sync with `sync_before=true`. |
| `ANKI_SYNC_ON_WRITE` | `true` | Synchronize before and after coordinated mutations. Disabling this permits local changes to remain unsynchronized. |
| `ANKI_SYNC_TIMEOUT_SECONDS` | `300` | Seconds a background media sync may go without reporting progress before it is aborted (`> 0`, up to `3600`). A sync that keeps advancing is never cut off; an aborted one resumes from the files already transferred on the next sync. |
| `ANKI_MEDIA_SYNC_WAIT_SECONDS` | `10` | How long `anki_sync` waits for media synchronization before returning `media_sync.state = "in_progress"` (`0`-`60` seconds; `0` returns immediately). Media sync continues in the background; follow it with `anki_status`. |
| `ANKI_INITIAL_MEDIA_SYNC` | `true` | Once authenticated, start a media sync on startup and keep retrying with backoff until one completes. Until then `anki_status` reports `ready: false` (`initial_media_sync_in_progress` or `initial_media_sync_failed`) and `anki_media_*` tools return `MEDIA_SYNC_IN_PROGRESS`; other tools and `/health/ready` are unaffected. A media write made while a later media sync is still running returns `MEDIA_SYNC_IN_PROGRESS`; retry it with the same idempotency key once `anki_status` shows the sync finished. Set `false` for deployments with no media. |
| `ANKI_BOOTSTRAP_MODE` | `disabled` | `disabled`, or `download_if_empty` to sign in to the user's sync account and download their collection the first time it opens while empty. A non-empty collection is left untouched, so the setting can stay on. The download fails rather than uploading if the server requests a full upload. |

Preserve the `anki_data` volume across container recreation: it holds every user's
collection, sync login, mutation receipts and backups.

### Upgrading from a single-collection install

Earlier versions kept one collection at `ANKI_COLLECTION_PATH` and took credentials from
environment variables. On first start, the server moves that collection, its `.media` folder,
`state/` and `backups/` into `users/owner/`, and writes an `owner` user from
`MCP_OAUTH_PASSWORD_HASH` and the `ANKI_SYNC_*` settings. OAuth clients signed in before the
upgrade keep working as `owner`. Bearer-token clients do not: if the install used a bearer
token, set a password with `anki-mcp-user passwd owner` and sign the client in over OAuth.

The old settings (`MCP_AUTH_MODE`, `MCP_AUTH_TOKEN`, `MCP_OAUTH_PASSWORD_HASH`,
`ANKI_COLLECTION_PATH` and the `ANKI_SYNC_*` account settings) are then ignored with a warning;
remove them. If a migration is interrupted, the server refuses to start and names the folder to
finish by hand.

### Safety and resource limits

| Variable | Default | Description |
| --- | --- | --- |
| `ANKI_ALLOW_DESTRUCTIVE` | `false` | Registers delete and FSRS rescheduling preview/apply tools when `MCP_SCOPES` also includes `destructive`. Applying destructive changes requires a preview token and verified fresh backup. |
| `ANKI_ALLOW_FULL_SYNC` | `false` | Registers explicit full-download and full-upload recovery tools. It must also be enabled for `anki_note_type_fields_update` and `anki_templates_update`, which can force a full sync. |
| `ANKI_ALLOW_SCHEMA_CHANGES` | `false` | Registers `anki_note_types_create` and `anki_note_types_update`, which never force a full sync. The field and template mapping tools also need `ANKI_ALLOW_FULL_SYNC`. |
| `ANKI_ALLOW_REVIEW_ANSWERS` | `false` | Registers the administrative card-answering tool, which changes scheduling state. |
| `ANKI_CONFIRMATION_TTL_SECONDS` | `300` | Lifetime of destructive-operation preview tokens (`30`-`3600` seconds). Tokens are single-use. |
| `ANKI_CONFIRMATION_MODE` | `token` | How destructive tools get approval. `token`: the preview token alone. `elicit`: also asks the person through MCP elicitation when the client supports it, otherwise falls back to `token`. `elicit_required`: refuses destructive calls from clients that cannot elicit. A prompt left unanswered for `ANKI_CONFIRMATION_TTL_SECONDS` counts as a decline. Full-sync tools ignore this setting and always ask (see below). Both elicit modes, and `ANKI_ALLOW_FULL_SYNC=true`, switch the MCP endpoint to SSE responses. |
| `ANKI_MAX_BATCH_SIZE` | `50` | Maximum notes accepted by one atomic batch-create operation (`1`-`500`). |
| `MCP_MAX_PAGE_SIZE` | `100` | Maximum `limit` accepted by paginated tools (`1`-`1000`). |
| `MCP_MAX_SEARCH_SCAN` | `10000` | Maximum collection items that bounded list/search and tag operations may scan (`1`-`1000000`). `anki_media_list` pages the whole media folder and is not bounded by it. |
| `MCP_MAX_RENDERED_FIELD_BYTES` | `262144` | UTF-8 byte limit for each rendered text field in responses (`1`-`4194304`); oversized values are truncated and marked. |
| `MCP_MAX_CARD_FIELDS` | `100` | Maximum fields or templates returned for a note or note type (`1`-`1000`); omitted counts are reported. |
| `ANKI_MAX_MEDIA_BYTES` | `1048576` | Maximum decoded size of a media file read, written, or renamed (`1`-`16777216` bytes). |
| `ANKI_MEDIA_URL_HOSTS` | `cdn.jsdelivr.net,commons.wikimedia.org,fonts.gstatic.com,raw.githubusercontent.com,unpkg.com,upload.wikimedia.org` | Comma-separated hosts that `anki_media_store` may download a `url` from, replacing the default list. The default covers npm and GitHub CDNs (Fontsource fonts), Google Fonts files and Wikipedia/Wikimedia Commons media, including Commons `Special:FilePath` URLs (`https://commons.wikimedia.org/wiki/Special:FilePath/<File name>?width=960`), which name a file by its title and redirect to `upload.wikimedia.org`. Only HTTPS is accepted, every redirect must stay on a listed host, and the download stops at `ANKI_MAX_MEDIA_BYTES`. Only the default HTTPS port is accepted, and each download must finish within 30 seconds. On `cdn.jsdelivr.net` and `unpkg.com` the URL must name an exact package version (`pkg@1.2.3`); on `raw.githubusercontent.com` and jsdelivr's `/gh/` it must name a full 40-character commit hash, so the content behind a URL cannot change after it was checked. Other hosts, including Wikimedia and any you add, are not checked for pinning. The stored filename must have an image (including SVG), audio, video or font extension. The tool's description lists the hosts and extensions in effect. Failures return `MEDIA_URL_HOST_NOT_ALLOWED`, `MEDIA_URL_NOT_PINNED`, `MEDIA_TYPE_NOT_ALLOWED`, `MEDIA_URL_NOT_FOUND`, `MEDIA_TOO_LARGE` or `MEDIA_DOWNLOAD_FAILED`. Downloads honor `HTTPS_PROXY` and `NO_PROXY`; set them to keep the server's address from the listed hosts. Anki's sync client honors the same variables, so `HTTPS_PROXY` also routes sync traffic unless the sync host is listed in `NO_PROXY`. The idempotency key covers the URL, not the downloaded bytes: retrying under a key that already has a receipt returns that receipt without downloading again, even if the file behind an unpinned URL has since changed. Use a new key to fetch it again. Set this variable empty to refuse every URL. |
| `ANKI_MEDIA_URL_HOSTS_FILE` | unset | Path to a file of allowed hosts, one per line; blank lines and `#` comments are ignored. Use instead of `ANKI_MEDIA_URL_HOSTS`, not with it. |
| `MCP_MAX_RESPONSE_BYTES` | `1048576` | Maximum serialized tool-result size (`1024`-`16777216` bytes). It must be at least `MCP_MAX_RENDERED_FIELD_BYTES + 4096`. |
| `MCP_MAX_REQUEST_BYTES` | `1048576` | Maximum MCP request-body size (`1024`-`16777216` bytes); larger requests receive HTTP 413. |

Boolean values accept the forms supported by Pydantic settings, including `true`/`false`,
`1`/`0`, and `yes`/`no`. Invalid or unknown configuration causes startup to fail instead of
silently falling back to a default.

SVG files stored by URL can contain scripts. A script in an SVG stays inert only when a card
loads the file through `<img src>`; inline SVG, `<object>`, `<embed>` and `<iframe>` run it in
the reviewer. This server does not check card HTML, so a client that writes cards referencing
SVG media is responsible for loading it only through `<img>`.

### Human confirmation

Every tool declares how it affects the collection, through its annotations and the `anki-mcp/sync` key in its `_meta`:

| Kind | `anki-mcp/sync` | Annotations | Approval |
| --- | --- | --- | --- |
| Read and preview | `none` | `readOnlyHint`, `idempotentHint` | None |
| Write that syncs incrementally, such as adding notes or repositioning cards | `incremental` | `destructiveHint` on deletes, deck option and scheduler changes, and FSRS apply | Preview token, plus a prompt when `ANKI_CONFIRMATION_MODE` asks for one |
| Write that may force a full sync: `anki_note_type_fields_update`, `anki_templates_update` | `conditional` | `destructiveHint` | The preview's `full_sync_required` says whether this call forces one. Renames and text or CSS edits sync normally. Adding, removing or reordering a field or template forces a full sync: the person is asked to approve it after syncing their other devices. Clients that cannot ask get `MANUAL_CHANGE_REQUIRED` with steps to make the change in Anki desktop instead. |
| Write that forces a full sync: full upload and download | `full` | `destructiveHint` | A prompt to the person on every call. Clients that cannot show one are refused. A retry that replays a stored receipt changes nothing and does not ask. |

As of November 2026 only Claude Code is known to render MCP elicitation prompts; claude.ai, Claude Desktop and the mobile apps do not, and some desktop sessions accept the request without ever showing it. For those clients the main safeguard is not registering destructive tools: leave `destructive` out of `MCP_SCOPES` and keep `ANKI_ALLOW_DESTRUCTIVE`, `ANKI_ALLOW_SCHEMA_CHANGES` and `ANKI_ALLOW_FULL_SYNC` off.

When AnkiWeb asks for a full sync because another device changed a note type's structure, the server downloads AnkiWeb's copy automatically if it has nothing unsynced: Anki's last-modified time is not newer than its last sync, no content row is waiting to be sent, and none of its own writes failed to sync. It takes a backup first. Otherwise sync fails with `FULL_SYNC_REQUIRED`, `anki_operations_list` shows what would be lost, and `anki_sync_full_download` asks the person before discarding it. A collection that has never synced is left to `ANKI_BOOTSTRAP_MODE`.

Theming a note type (name, CSS, template text) with `anki_note_types_create` or `anki_note_types_update` needs the `admin` scope and `ANKI_ALLOW_SCHEMA_CHANGES=true`. It syncs normally and works in every client. `anki_note_types_update` and its `update` preview change only the parts they are sent: send just `css` to restyle, and the name, fields and templates stay as they are. At least one of `name`, `fields`, `templates` or `css` is required, and the preview's confirmation token covers exactly the values sent. Structural changes through `anki_note_type_fields_update` or `anki_templates_update` also need `ANKI_ALLOW_FULL_SYNC=true` and, when they force a full sync, either a client that can show a prompt or the person making the change in Anki desktop from the returned steps. Plugins should treat theming as optional when those tools are not listed.

## Development

Requires Python 3.12 and [uv](https://docs.astral.sh/uv/).

```sh
uv sync --frozen --all-groups
uv run pytest
uv run ruff check .
uv run pyright
```

Pushing a `v*` tag publishes a multi-architecture image (`linux/amd64`, `linux/arm64`) to
`ghcr.io/<owner>/<repo>`, tagged with the version and the commit SHA. Pull requests that touch
the image's inputs build it without publishing.

## License

[MIT](LICENSE) — free to use, modify, and distribute under the license terms.

## Trademark disclaimer

This is an independent, unofficial project. It is not affiliated with, endorsed by, or
sponsored by Anki, AnkiWeb, Ankitects Pty Ltd, or their maintainers. Anki and AnkiWeb are
trademarks of their respective owners.