"""
SSH SOCKS5-туннель к удалённому WARP-демону.

Схема: локальный Mac → ssh -L 127.0.0.1:1085 → REMOTE_HOST:127.0.0.1:40000 (warp-svc) → Cloudflare → интернет.

Зачем нужно:
  - Gemini API недоступен из ряда регионов; через WARP exit-IP Cloudflare запросы проходят.
  - MangaDex CDN при некоторых VPN/proxy-конфигурациях клиента отдаёт страницы крайне медленно;
    через прямой WARP-туннель скорость нормализуется.

Настройка:
  Переменные окружения (либо .env):
    WARP_SSH_HOST       = user@your-remote-host  (обязательно)
    WARP_SSH_PORT       = 22                     (опционально, default 22)
    WARP_LOCAL_PORT     = 1085                   (опционально)
    WARP_REMOTE_PORT    = 40000                  (опционально, порт warp-svc на удалённом хосте)

На удалённом хосте должен быть запущен Cloudflare WARP с включённым
proxy-mode: `warp-cli mode proxy && warp-cli connect`. Порт по умолчанию 40000.
"""

from __future__ import annotations

import os
import subprocess
import time


WARP_HOST = os.environ.get("WARP_SSH_HOST", "")
WARP_SSH_PORT = int(os.environ.get("WARP_SSH_PORT", "22"))
WARP_LOCAL_PORT = int(os.environ.get("WARP_LOCAL_PORT", "1085"))
WARP_REMOTE_PORT = int(os.environ.get("WARP_REMOTE_PORT", "40000"))
WARP_PROXY_URL = f"socks5h://127.0.0.1:{WARP_LOCAL_PORT}"


def is_alive() -> bool:
    """Проверяет, висит ли уже SSH local-forward на нужном порту."""
    if not WARP_HOST:
        return False
    host_part = WARP_HOST.split("@", 1)[-1]
    r = subprocess.run(
        ["pgrep", "-f", f"ssh.*-L 127.0.0.1:{WARP_LOCAL_PORT}.*{host_part}"],
        capture_output=True,
        text=True,
    )
    return bool(r.stdout.strip())


def ensure_up() -> None:
    """Поднимает SSH-туннель, если ещё не запущен."""
    if not WARP_HOST:
        raise RuntimeError(
            "WARP_SSH_HOST не задан. Укажи user@host в .env — это удалённая машина с warp-cli proxy."
        )
    if is_alive():
        return
    print(
        f"[tunnel] поднимаю ssh -L {WARP_LOCAL_PORT}:127.0.0.1:{WARP_REMOTE_PORT} {WARP_HOST}"
    )
    subprocess.run(
        [
            "ssh",
            "-fN",
            "-p",
            str(WARP_SSH_PORT),
            "-L",
            f"127.0.0.1:{WARP_LOCAL_PORT}:127.0.0.1:{WARP_REMOTE_PORT}",
            "-o",
            "ServerAliveInterval=30",
            "-o",
            "ServerAliveCountMax=3",
            "-o",
            "ExitOnForwardFailure=yes",
            "-o",
            "StrictHostKeyChecking=no",
            WARP_HOST,
        ],
        check=True,
        timeout=15,
    )
    time.sleep(2)
    if not is_alive():
        raise RuntimeError("SSH-туннель не поднялся — проверь доступ к WARP_SSH_HOST")
    print("[tunnel] OK")


def enable_proxy_env() -> None:
    """
    Поднимает туннель и выставляет HTTPS_PROXY/HTTP_PROXY/ALL_PROXY на process-level.
    requests + httpx + большинство библиотек уважают эти переменные через PySocks/socksio.
    """
    ensure_up()
    os.environ["HTTPS_PROXY"] = WARP_PROXY_URL
    os.environ["HTTP_PROXY"] = WARP_PROXY_URL
    os.environ["ALL_PROXY"] = WARP_PROXY_URL
