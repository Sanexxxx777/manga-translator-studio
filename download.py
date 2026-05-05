"""
Скачивание главы манги с MangaDex.

API: https://api.mangadex.org/docs/
Workflow:
  1. /chapter/{id}            -> метаданные (volume, chapter, title)
  2. /at-home/server/{id}     -> baseUrl + hash + список страниц
  3. {baseUrl}/data/{hash}/{filename}  -> сами картинки

MangaDex API rate limit: 5 req/sec, мы держим 4 req/sec с запасом.
Скачивание изображений с at-home узла лимитим 0.25 сек между запросами.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path
from typing import Any, Iterable

import requests
from tqdm import tqdm

API_BASE = "https://api.mangadex.org"
USER_AGENT = "manga-tr/0.1 (personal pipeline)"
PAGE_DELAY_SEC = 0.25
API_MIN_DELAY_SEC = 0.25  # 4 запроса/сек, ниже официального лимита 5
HTTP_TIMEOUT = (15, 90)  # (connect, read) — read большой, MangaDex CDN иногда медленный
MAX_RETRIES = 4

CHAPTER_UUID_RE = re.compile(
    r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
    re.IGNORECASE,
)


class MangaDexError(RuntimeError):
    pass


def parse_chapter_id(value: str) -> str:
    """
    Принимает либо чистый UUID, либо ссылку вида
    https://mangadex.org/chapter/<uuid>[/page]. Возвращает UUID нижним регистром.
    """
    match = CHAPTER_UUID_RE.search(value.strip())
    if not match:
        raise ValueError(f"Не распознал chapter UUID в строке: {value!r}")
    return match.group(1).lower()


class MangaDexClient:
    """Минимальный клиент с глобальным rate-limit для всех вызовов."""

    def __init__(self) -> None:
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT})
        self._last_request_ts = 0.0

    def _throttle(self, min_delay: float) -> None:
        elapsed = time.monotonic() - self._last_request_ts
        if elapsed < min_delay:
            time.sleep(min_delay - elapsed)
        self._last_request_ts = time.monotonic()

    def _get(self, url: str, *, min_delay: float, stream: bool = False) -> requests.Response:
        last_exc: Exception | None = None
        for attempt in range(MAX_RETRIES):
            self._throttle(min_delay)
            try:
                resp = self.session.get(url, timeout=HTTP_TIMEOUT, stream=stream)
            except requests.RequestException as exc:
                last_exc = exc
                time.sleep(1.5 * (attempt + 1))
                continue
            if resp.status_code == 429:
                # MangaDex явно говорит подождать через Retry-After
                wait = int(resp.headers.get("Retry-After", "5"))
                time.sleep(wait + 1)
                continue
            if 500 <= resp.status_code < 600:
                time.sleep(1.5 * (attempt + 1))
                continue
            resp.raise_for_status()
            return resp
        raise MangaDexError(f"Не удалось загрузить {url}: {last_exc}")

    def chapter_meta(self, chapter_id: str) -> dict:
        # includes[]=manga чтобы сразу получить и manga.attributes.title
        url = f"{API_BASE}/chapter/{chapter_id}?includes[]=manga"
        return self._get(url, min_delay=API_MIN_DELAY_SEC).json()

    def at_home_server(self, chapter_id: str) -> dict:
        url = f"{API_BASE}/at-home/server/{chapter_id}"
        return self._get(url, min_delay=API_MIN_DELAY_SEC).json()

    def download_image(self, url: str, dest: Path) -> None:
        last_exc: Exception | None = None
        tmp = dest.with_suffix(dest.suffix + ".part")
        # Гарантируем что родительская папка есть (mkdir уже сделан в
        # download_chapter, но на случай гонки — повторим).
        dest.parent.mkdir(parents=True, exist_ok=True)
        for attempt in range(MAX_RETRIES):
            # ловим ВСЁ — иначе PySocks/SSL/OSError/etc. могут вылететь
            # выше и убить pipeline на пустом месте, не дав retry.
            try:
                resp = self._get(url, min_delay=PAGE_DELAY_SEC, stream=True)
                with tmp.open("wb") as fh:
                    for chunk in resp.iter_content(chunk_size=64 * 1024):
                        if chunk:
                            fh.write(chunk)
                # Файл .part мог не появиться (диск, race с очисткой),
                # либо быть пустым (chunked-EOF). Оба = неудача → retry.
                if not tmp.exists() or tmp.stat().st_size == 0:
                    tmp.unlink(missing_ok=True)
                    raise MangaDexError(f"empty/missing .part for {url}")
                tmp.replace(dest)
                return
            except Exception as exc:
                last_exc = exc
                # подчистим частичный .part чтоб не мешал следующей попытке
                try:
                    if tmp.exists():
                        tmp.unlink()
                except OSError:
                    pass
                # лог в формате который понятен в pipeline-логе
                print(
                    f"[download] retry {attempt + 1}/{MAX_RETRIES} "
                    f"для {url.rsplit('/', 1)[-1]}: {type(exc).__name__}: {exc}",
                    flush=True,
                )
                time.sleep(2 * (attempt + 1))
        raise MangaDexError(f"image download failed after {MAX_RETRIES} retries: {last_exc}")


def _extract_meta(chapter_meta_raw: dict) -> dict[str, Any]:
    """
    Из ответа /chapter/{id}?includes[]=manga вытаскиваем чистые поля:
      volume, chapter, chapter_title, manga_title, manga_id.
    Все строки. Если данных нет — None / "".
    """
    out: dict[str, Any] = {
        "volume": None, "chapter": None, "chapter_title": None,
        "manga_title": None, "manga_id": None,
    }
    data = chapter_meta_raw.get("data") or {}
    attrs = data.get("attributes") or {}
    out["volume"] = attrs.get("volume") or None
    out["chapter"] = attrs.get("chapter") or None
    out["chapter_title"] = (attrs.get("title") or "").strip() or None

    for rel in data.get("relationships") or []:
        if rel.get("type") == "manga":
            out["manga_id"] = rel.get("id")
            mattrs = (rel.get("attributes") or {})
            titles = mattrs.get("title") or {}
            alt_titles = mattrs.get("altTitles") or []
            # приоритет языков: en → ja-ro → ja → первое доступное
            for lang in ("en", "ja-ro", "ja"):
                if lang in titles and titles[lang]:
                    out["manga_title"] = titles[lang]
                    break
            if not out["manga_title"] and titles:
                out["manga_title"] = next(iter(titles.values()))
            # если в основном пусто — попробуем altTitles
            if not out["manga_title"]:
                for at in alt_titles:
                    for lang in ("en", "ja-ro", "ja"):
                        if at.get(lang):
                            out["manga_title"] = at[lang]
                            break
                    if out["manga_title"]:
                        break
            break
    return out


def download_chapter(chapter_input: str, output_root: Path) -> Path:
    """
    Скачивает главу в output_root/<chapter_id>/.
    Возвращает путь к папке главы.
    """
    chapter_id = parse_chapter_id(chapter_input)
    client = MangaDexClient()

    print(f"[download] chapter id: {chapter_id}")

    chapter_dir = output_root / chapter_id
    chapter_dir.mkdir(parents=True, exist_ok=True)
    # подчистить пустые stub'ы от прошлых попыток
    for p in chapter_dir.glob("page_*"):
        if p.stat().st_size == 0:
            p.unlink()

    # Тащим метаданные главы (volume, chapter, manga title) и пишем .meta.json
    try:
        meta = _extract_meta(client.chapter_meta(chapter_id))
        meta_label = []
        if meta.get("volume"):  meta_label.append(f"Vol.{meta['volume']}")
        if meta.get("chapter"): meta_label.append(f"Ch.{meta['chapter']}")
        if meta.get("manga_title"): meta_label.append(meta["manga_title"])
        print(f"[download] meta: {' · '.join(meta_label) or '(нет данных)'}")
        (chapter_dir / ".meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2))
    except Exception as e:
        print(f"[download] meta пропущена: {e}")

    def _refresh_pages() -> tuple[str, str, list[str]]:
        s = client.at_home_server(chapter_id)
        ch = s["chapter"]
        return s["baseUrl"], ch["hash"], ch["data"]

    base_url, page_hash, pages = _refresh_pages()
    if not pages:
        raise MangaDexError("В главе нет страниц (поле data пустое)")

    digits = max(2, len(str(len(pages))))
    bar = tqdm(pages, desc=f"pages [{chapter_id[:8]}]", unit="img")
    for idx, filename in enumerate(bar, start=1):
        ext = Path(filename).suffix.lower() or ".png"
        out_path = chapter_dir / f"page_{idx:0{digits}d}{ext}"
        if out_path.exists() and out_path.stat().st_size > 0:
            continue
        url = f"{base_url}/data/{page_hash}/{filename}"
        try:
            client.download_image(url, out_path)
        except MangaDexError:
            # at-home узел мог сдохнуть — берём свежий и пробуем ещё раз
            print(f"[download] узел {base_url} протух, прошу новый...")
            base_url, page_hash, pages = _refresh_pages()
            url = f"{base_url}/data/{page_hash}/{filename}"
            client.download_image(url, out_path)

    print(f"[download] готово: {chapter_dir} ({len(pages)} страниц)")
    return chapter_dir


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Скачать главу с MangaDex")
    parser.add_argument(
        "chapter",
        help="UUID главы или ссылка вида https://mangadex.org/chapter/<uuid>",
    )
    parser.add_argument(
        "--input-dir",
        default="input",
        help="Корень для скачанных страниц (по умолчанию ./input)",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    try:
        download_chapter(args.chapter, Path(args.input_dir))
    except (MangaDexError, ValueError) as exc:
        print(f"[download] ошибка: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
