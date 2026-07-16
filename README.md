# Manga Translation Studio

By [Aleksandr Shulgin](https://github.com/Sanexxxx777) ([@Aleksandr_NFA](https://t.me/Aleksandr_NFA)).

A local pipeline that takes a MangaDex chapter URL, translates the text into your target language with Google Gemini, and packages the result as a `.cbz` archive. Includes a small Express-based web UI for one-click runs and live progress.

Everything in this repo — the pipeline, the Gemini translator, the SQLite cache, the SSH/WARP tunnel, the web UI — is my own code. The heavy lifting for OCR, text detection and inpainting comes from [`manga-image-translator`](https://github.com/zyddnys/manga-image-translator) (MIT, by zyddnys) — it's a separate project you clone alongside this one (see Setup below), not vendored or claimed as mine. Gemini does the language work on top of it, with a domain-specific system prompt and a SQLite cache so re-runs are nearly free.

## Why it exists

Existing manga MTL tools either pipe raw OCR through generic translators (loses character voice and pacing) or require running a heavy desktop app per chapter. This project chains a high-quality OCR stack with an LLM that has been prompted for manga-style speech, and exposes a one-input web form so a chapter URL → reading-ready `.cbz` is a single click.

## Requirements

- macOS (tested on M-series with MPS) or Linux with CUDA. Should run on CPU but slowly.
- Python 3.11+ with a virtualenv
- Node.js 18+
- A Google AI Studio API key for Gemini
- A remote machine reachable over SSH where Cloudflare WARP runs in proxy mode (used as a SOCKS5 exit). This is needed if Gemini is geo-blocked from your network or your MangaDex CDN traffic is throttled by a local VPN/proxy client. See [Setting up the WARP host](#setting-up-the-warp-host) below; you can skip it on networks where neither problem applies.

## Setup

```bash
git clone https://github.com/Sanexxxx777/manga-translator-studio.git
cd manga-translator-studio

# 1. Clone manga-image-translator as a sibling directory
git clone https://github.com/zyddnys/manga-image-translator.git
# Models (~720 MB) will be downloaded on first run into manga-image-translator/models/

# 2. Python venv + dependencies
python3.11 -m venv venv
source venv/bin/activate
pip install -r manga-image-translator/requirements.txt
pip install -r requirements.txt
# On macOS with Apple Silicon you may also need:
#   pip install --upgrade torch torchvision   # latest with MPS support
#   pip install httpx[socks] PySocks          # for SOCKS5 over both async and sync clients

# 3. Node.js dependencies for the web UI
npm install

# 4. Configure
cp .env.example .env
$EDITOR .env   # paste your GEMINI_API_KEY and WARP_SSH_HOST
```

### Setting up the WARP host

On any always-on remote machine you can SSH into:

```bash
# Install Cloudflare WARP
curl -fsSL https://pkg.cloudflareclient.com/pubkey.gpg | sudo gpg --yes --dearmor -o /usr/share/keyrings/cloudflare-warp-archive-keyring.gpg
echo "deb [signed-by=/usr/share/keyrings/cloudflare-warp-archive-keyring.gpg] https://pkg.cloudflareclient.com/ $(lsb_release -cs) main" | sudo tee /etc/apt/sources.list.d/cloudflare-client.list
sudo apt update && sudo apt install -y cloudflare-warp

# Switch to proxy mode so warp-svc listens on 127.0.0.1:40000
warp-cli registration new
warp-cli mode proxy
warp-cli connect
warp-cli status   # should print Connected
```

Then on the local machine just run any of the commands below — `tunnel.py` opens an SSH local-forward `127.0.0.1:1085 → REMOTE:127.0.0.1:40000` automatically.

## Usage

### Web UI

```bash
node server.js
```

Open <http://127.0.0.1:3017>. Paste a chapter URL (`https://mangadex.org/chapter/<uuid>` or `https://mangadex.org/chapter/<uuid>/<page>`), press **Перевести**, watch progress over Server-Sent Events. When the run finishes, the page exposes a **Скачать CBZ** button and a thumbnail strip with the translated pages.

### CLI

```bash
source venv/bin/activate
python pipeline.py "https://mangadex.org/chapter/<uuid>"

# Skip stages when iterating:
python pipeline.py <url> --skip-download
python pipeline.py <url> --skip-translate
python pipeline.py <url> --skip-cbz
```

Outputs end up in `output/<chapter_id>/` (PNG pages) and `output/<chapter_id>.cbz` (archive).

## How it works

```
MangaDex URL
    │
    ▼
download.py        ← MangaDex API: chapter manifest → page CDN → input/<id>/*.png
    │
    ▼
translate.py       ← manga-image-translator: text-detection + OCR + inpainting
    │                custom CommonTranslator → Gemini batch JSON → cache.db
    ▼
output/<id>/*.png  ← MIT renders translated text back onto inpainted pages
    │
    ▼
pipeline.py        ← zip → output/<id>.cbz
```

Key implementation choices:

- **One Gemini batch per page**, schema-validated JSON output. Cuts latency vs. one call per balloon, lets the model see surrounding context.
- **Persistent cache** keyed by `(source_text, source_lang, target_lang, model)`. Re-translating the same chapter or a re-uploaded scan is ~free.
- **Custom translator class** registered into MIT's `TRANSLATORS` registry instead of forking it. The downloader for offline translators is monkey-patched to no-op so MIT doesn't pull 1.5 GB of jparacrawl when we only need OCR + inpaint.
- **SSH SOCKS5 tunnel** opened on demand; `socks5h://` so DNS resolution happens at the WARP exit.

## Opening the .cbz

A `.cbz` is just a ZIP of images. On macOS:

- **YACReader** — open source, has a library mode and double-page spreads: `brew install --cask yacreader`
- **Simple Comic** — native, free on the App Store
- **Chunky Reader** — paid (~$5), gesture-driven, very polished

In a pinch, rename to `.zip` and Finder will preview the pages.

## Project layout

```
manga-translator-studio/
├── server.js              Express + SSE web server
├── pipeline.py            CLI entry point: download → translate → cbz
├── download.py            MangaDex API client
├── translate.py           Custom Gemini translator wired into MIT
├── tunnel.py              SSH SOCKS5 tunnel manager
├── public/                Web UI (vanilla HTML/CSS/JS)
├── fonts/                 PT Sans Narrow (SIL Open Font License)
└── requirements.txt       Project-specific Python deps (on top of MIT's)
```

## About — NFA Trading Suite

This project is part of **NFA Trading Suite**, a personal collection of small tools and dashboards. The same design system (Liquid Glass tokens, Geist + Playfair, warm-gold accent) lives across:

- [Setup Manager](https://setupmanager.dpdns.org) — utility for one-shot project bootstraps
- [Sector Map](https://sectormap.dpdns.org) — crypto sector heatmap

Open to suggestions and PRs.

## License

MIT — see [LICENSE](LICENSE).

`manga-image-translator` is a separate project under its own license; clone it independently.

PT Sans Narrow is © ParaType, distributed under the SIL Open Font License 1.1.
