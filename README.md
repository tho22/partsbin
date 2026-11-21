# Partsbin

Self-hosted, mobile-first inventory for electronic components — from resistors through ESP boards to
Raspberry Pis. Runs as a single Docker container and is used from the phone as an installable PWA.

## Features

- **Capture by camera**: live view with automatic QR/barcode detection (on-device via the browser's
  `BarcodeDetector`), plus a shutter button that sends the photo to Claude for identification
  (part type, value, colour codes, IC markings, board variant, bag labels). Results can create a new part
  or add stock to a matching existing part.
- **Storage locations**: nested boxes/drawers/compartments, printable QR labels; scanning a label opens
  the location with its contents.
- **Stock**: quantities with +/− buttons, minimum quantities with a low-stock list, a movement history.
- **Datasheets & links** per part.
- **Projects**: reserve parts for a project, see shortages, take reserved parts out of stock.
- **Bluetooth label printer**: prints location and part labels directly on a SUPVAN / Katasymbol E11
  via Web Bluetooth (Chrome on Android or desktop), see below.

## Architecture

| Part | Tech |
|------|------|
| Backend | Python 3.12, FastAPI, SQLite (`/data/partsbin.db`), images in `/data/images` |
| Frontend | Vanilla JS PWA without build step (`app/static`) |
| Recognition | Claude via Anthropic API (`claude-opus-5-5`) **or** a local vision model via Ollama; both with schema-constrained JSON output |
| QR codes | `segno`, payloads `PB:L:<id>` (location) and `PB:P:<id>` (part) |

## Running

```bash
cp .env.example .env   # add ANTHROPIC_API_KEY
docker compose up -d --build
```

Without an API key (and without Ollama) everything works except the AI identification (photos are then
just attached to a new part).

### Local recognition with Ollama

Set `PARTSBIN_PROVIDER=ollama` and point `OLLAMA_URL` at an Ollama instance that has a **vision** model:

```bash
ollama pull qwen2.5vl:7b     # default; alternatives: qwen2.5vl:3b (faster), gemma3:12b, llama3.2-vision
```

- Ollama on the Docker host: `OLLAMA_URL=http://host.docker.internal:11434` (default). Ollama must listen
  on all interfaces (`OLLAMA_HOST=0.0.0.0`) for the container to reach it.
- Bundled container: `docker compose --profile ollama up -d`, `OLLAMA_URL=http://ollama:11434`, then
  `docker exec partsbin-ollama ollama pull qwen2.5vl:7b`.
- Ollama elsewhere (e.g. a machine with a GPU): its LAN or tailnet address.

Expectations: a 7B vision model needs about 6–8 GB RAM/VRAM. With a GPU an answer takes a few seconds,
CPU-only often one to several minutes (raise `OLLAMA_TIMEOUT` if needed). Local models read board names
and printed labels reasonably well, but are noticeably weaker than Claude at resistor colour bands and
tiny SMD/IC markings.

### Configuration

| Variable | Default | Meaning |
|----------|---------|---------|
| `PARTSBIN_PROVIDER` | `anthropic` | `anthropic` or `ollama` |
| `ANTHROPIC_API_KEY` | – | Enables photo identification with Claude |
| `PARTSBIN_MODEL` | `claude-opus-5-5` | Model used for identification |
| `PARTSBIN_EFFORT` | `medium` | `low` is cheaper/faster, `high` reads tricky markings better |
| `PARTSBIN_LANGUAGE` | `English` | Language of the generated descriptions |
| `OLLAMA_URL` | `http://host.docker.internal:11434` | Ollama endpoint |
| `OLLAMA_MODEL` | `qwen2.5vl:7b` | Ollama vision model |
| `OLLAMA_TIMEOUT` | `300` | Seconds to wait for an answer |
| `OLLAMA_THINK` | `false` | Let thinking-capable models (e.g. qwen3.6) reason before answering; slower |
| `PARTSBIN_MAX_EDGE` | `1568` / `1024` | Longest image edge sent (Claude / Ollama) |
| `PARTSBIN_DATA_DIR` | `/data` | Database and image directory |

### HTTPS is required for the camera

Browsers only allow camera access on secure origins. In a Tailscale setup the simplest option is:

```bash
tailscale serve --bg 8000        # on the Docker host
# -> https://<host>.<tailnet>.ts.net
```

Any reverse proxy with a valid certificate (Caddy, Traefik, Nginx Proxy Manager) works as well.
Open the URL on the phone and use "Add to home screen" / "Install app".

The app has **no authentication**. Only expose it inside a trusted network or tailnet, or put it behind
an authenticating proxy.

### Label printer (SUPVAN / Katasymbol E11)

The printer page (Labels → ⚙️ Label printer) connects to the E11 over Web Bluetooth; afterwards every
location/part label has a "Print on label printer" button and the labels page prints a selection in
one go. The server renders the label (`/api/printer/supvan/{kind}/{id}`, preview: `….png`) and the
browser only relays the bytes.

- Close the Katasymbol app first; the printer accepts one BLE connection at a time.
- Label size, density and orientation are set on the printer page (default 12 × 40 mm).
- Protocol: frame formats from [heeen/supvan-cups](https://github.com/heeen/supvan-cups) (MIT); the print
  flow (4000-byte buffers, one LZMA1 block without end marker per buffer, centring margins, 0xBB
  acknowledgement per data frame, `BUF_FULL` with parameter 0) follows the E11 path of the vendor
  Android app. LZMA without end marker needs liblzma ≥ 5.4 (called via `ctypes`).
- Other SUPVAN models use different flows and are not supported.

### Backup

Everything lives in the `partsbin-data` volume (`partsbin.db` + `images/`). Use
`sqlite3 partsbin.db ".backup backup.db"` for a consistent copy of the database while running.

**Proxmox host backup script:** `deploy/backup/partsbin-backup.sh` is meant for a setup where Docker
runs in an LXC container on Proxmox VE. Run nightly by `partsbin-backup.timer`, it takes a consistent,
integrity-checked copy of the database inside the running container, streams it with the photos as
`partsbin-YYYY-MM-DD.tar.gz` to a PVE backup storage (default `/mnt/pve/backup/partsbin/`) and keeps
14 days plus the first archive of each of the last 6 months. Settings (`CT`, `CONTAINER`,
`STORAGE_MOUNT`, `HA_WEBHOOK_URL`, …) can be overridden in `/etc/default/partsbin-backup`; on failure a
JSON message `{"status": "failed", "message": …, "host": …}` is POSTed to `HA_WEBHOOK_URL`, e.g. a Home
Assistant webhook automation.

Install or update on the Proxmox host:

```bash
scp deploy/backup/partsbin-backup.sh root@<pve-host>:/usr/local/sbin/
scp deploy/backup/partsbin-backup.{service,timer} root@<pve-host>:/etc/systemd/system/
ssh root@<pve-host> 'systemctl daemon-reload && systemctl enable --now partsbin-backup.timer'
```

Restore (stops the app briefly; replaces the current data; `<ct>` is the docker LXC):

```bash
f=/mnt/pve/backup/partsbin/partsbin-YYYY-MM-DD.tar.gz
pct exec <ct> -- docker stop partsbin
pct exec <ct> -- docker run --rm -i -v partsbin_partsbin-data:/data alpine \
  sh -c 'rm -rf /data/images /data/partsbin.db* && tar -xzf - -C /data && chown -R 1000:1000 /data' < "$f"
pct exec <ct> -- docker start partsbin
```

## Deployment with Portainer

`deploy/deploy.sh` builds the committed `HEAD` directly on the docker host (no registry) and redeploys
an existing Portainer stack that uses `deploy/portainer-stack.yml`. Configuration (provider, API key,
listening address `PARTSBIN_BIND`/`PARTSBIN_PORT`, …) lives in the stack's environment in Portainer and
is kept on every deploy.

```bash
cp deploy/deploy.env.example deploy/deploy.env   # SSH target, Portainer URL, stack id, API token file
deploy/deploy.sh                                 # build HEAD on the host and redeploy the stack
```

Data is kept in the Docker volume `partsbin_partsbin-data`.

## Development

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
PARTSBIN_DATA_DIR=./data .venv/bin/uvicorn app.main:app --reload
.venv/bin/python -m pytest
```

On-device barcode detection needs Chrome/Edge on Android or Safari 17+; elsewhere codes can be typed
in via the keyboard button on the capture screen.

## Costs

With Ollama there are no API costs. With Claude, one identification is a single API request with one downscaled image (max. 1568 px). With
`claude-opus-5-5` at medium effort that is typically a few cents per photo.

## License

MIT, see [LICENSE](LICENSE). The SUPVAN frame format follows
[heeen/supvan-cups](https://github.com/heeen/supvan-cups) (MIT).
