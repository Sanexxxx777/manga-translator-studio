"""
Главный entrypoint.

Шаги:
  1. Скачать главу с MangaDex в input/<chapter_id>/
  2. Прогнать manga-image-translator + Gemini -> output/<chapter_id>/
  3. Упаковать output/<chapter_id>/*.png в output/<chapter_id>.cbz

Примеры:
  python pipeline.py https://mangadex.org/chapter/<uuid>
  python pipeline.py <uuid> --skip-download           # уже скачано
  python pipeline.py <uuid> --skip-translate          # просто пересобрать CBZ
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import zipfile
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).parent
load_dotenv(ROOT / ".env")

from download import download_chapter, parse_chapter_id  # noqa: E402
from tunnel import enable_proxy_env  # noqa: E402

INPUT_ROOT = ROOT / "input"
OUTPUT_ROOT = ROOT / "output"
MANGA_ROOT = ROOT / "Manga"

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp"}


# ─── Читаемые имена для Manga/ ──────────────────────────────────────────────
_FS_INVALID = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _safe_fs_name(s: str, max_len: int = 120) -> str:
    """Sanitize строку под имя файла/папки на macOS/Windows. Сохраняем читаемость."""
    if not s:
        return ""
    s = _FS_INVALID.sub("", s).strip()
    s = re.sub(r"\s+", " ", s)
    s = s.rstrip(". ")
    return s[:max_len] if len(s) > max_len else s


def _readable_chapter_name(meta: dict | None) -> str | None:
    """
    Из meta строит "Vol.7 Ch.52 — Otaku & Gyaru & Love Chocolate".
    Если volume/chapter отсутствуют — возвращает None (нет смысла нечитаемый UUID).
    """
    if not meta:
        return None
    parts = []
    if meta.get("volume"):
        parts.append(f"Vol.{meta['volume']}")
    if meta.get("chapter"):
        parts.append(f"Ch.{meta['chapter']}")
    head = " ".join(parts)
    title = meta.get("chapter_title") or ""
    name = f"{head} — {title}" if head and title else (head or title)
    return _safe_fs_name(name) or None


def publish_to_manga_folder(
    chapter_id: str,
    output_dir: Path,
    cbz_src: Path | None,
) -> tuple[Path | None, Path | None]:
    """
    Копирует CBZ + страницы в читаемую структуру:
        Manga/<manga_title>/Vol.X Ch.Y — <chapter_title>.cbz
        Manga/<manga_title>/Vol.X Ch.Y — <chapter_title>/page_*.png

    Возвращает (manga_dir, cbz_dst) или (None, None) если meta нет.
    """
    meta_path = output_dir / ".meta.json"
    if not meta_path.exists():
        meta_path = INPUT_ROOT / chapter_id / ".meta.json"
    if not meta_path.exists():
        print("[publish] meta нет — пропускаю Manga/ (только UUID-вариант)")
        return None, None

    try:
        meta = json.loads(meta_path.read_text("utf-8"))
    except Exception as e:
        print(f"[publish] не смог прочитать meta: {e}")
        return None, None

    manga_title = _safe_fs_name(meta.get("manga_title") or "")
    chapter_name = _readable_chapter_name(meta)
    if not manga_title or not chapter_name:
        print("[publish] meta неполная (нет manga_title или volume/chapter)")
        return None, None

    manga_dir = MANGA_ROOT / manga_title
    manga_dir.mkdir(parents=True, exist_ok=True)

    # Копируем CBZ
    cbz_dst = None
    if cbz_src and cbz_src.exists():
        cbz_dst = manga_dir / f"{chapter_name}.cbz"
        cbz_dst.write_bytes(cbz_src.read_bytes())
        print(f"[publish] CBZ → {cbz_dst}")

    # И папку со страницами рядом (на случай если хочется браузить файлы)
    pages_dir = manga_dir / chapter_name
    pages_dir.mkdir(exist_ok=True)
    copied = 0
    for img in sorted(output_dir.iterdir()):
        if img.suffix.lower() in IMAGE_EXTS:
            dst = pages_dir / img.name
            if not dst.exists() or dst.stat().st_size != img.stat().st_size:
                dst.write_bytes(img.read_bytes())
                copied += 1
    if copied:
        print(f"[publish] страниц → {pages_dir} ({copied} новых)")

    return manga_dir, cbz_dst


def build_cbz(image_dir: Path, cbz_path: Path) -> None:
    """
    Собирает CBZ из всех картинок в image_dir (без подпапок).
    Имена внутри архива нумеруются так же, как файлы — это даёт натуральный порядок.
    """
    images = sorted(
        [p for p in image_dir.iterdir() if p.suffix.lower() in IMAGE_EXTS]
    )
    if not images:
        raise RuntimeError(f"В {image_dir} нет картинок для CBZ")

    cbz_path.parent.mkdir(parents=True, exist_ok=True)
    # ZIP_STORED — без сжатия, png/jpg уже сжаты, лишний CPU не нужен
    with zipfile.ZipFile(cbz_path, "w", zipfile.ZIP_STORED) as zf:
        for img in images:
            zf.write(img, arcname=img.name)
    print(f"[cbz] {cbz_path} ({len(images)} страниц)")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="MangaDex -> Gemini перевод -> CBZ pipeline"
    )
    parser.add_argument(
        "chapter",
        help="UUID главы или ссылка https://mangadex.org/chapter/<uuid>",
    )
    parser.add_argument("--skip-download", action="store_true")
    parser.add_argument("--skip-translate", action="store_true")
    parser.add_argument("--skip-cbz", action="store_true")
    parser.add_argument(
        "--target-lang",
        default=os.environ.get("TARGET_LANG", "RUS"),
    )
    args = parser.parse_args(argv)

    chapter_id = parse_chapter_id(args.chapter)
    input_dir = INPUT_ROOT / chapter_id
    output_dir = OUTPUT_ROOT / chapter_id

    # 0. Поднимаем SSH-туннель к WARP — обходит геоблок Gemini API
    #    и стабилизирует загрузку MangaDex CDN, если местный VPN/прокси-клиент
    #    конфликтует с её доменом.
    enable_proxy_env()

    # 1. Download
    if args.skip_download:
        if not input_dir.exists():
            print(f"[pipeline] --skip-download, но {input_dir} не существует", file=sys.stderr)
            return 1
        print(f"[pipeline] skip download, использую {input_dir}")
    else:
        download_chapter(args.chapter, INPUT_ROOT)

    # 2. Translate (импорт здесь — тяжёлый, тащит torch)
    if args.skip_translate:
        if not output_dir.exists():
            print(f"[pipeline] --skip-translate, но {output_dir} не существует", file=sys.stderr)
            return 1
        print(f"[pipeline] skip translate, использую {output_dir}")
    else:
        from translate import translate_folder

        translate_folder(input_dir, output_dir, args.target_lang)

    # 2.5 Скопировать .meta.json из input/ в output/ (нужно для Manga/-публикации
    # и веб-UI; раньше это делал только server.js)
    meta_src = input_dir / ".meta.json"
    meta_dst = output_dir / ".meta.json"
    if meta_src.exists() and output_dir.exists() and not meta_dst.exists():
        try:
            meta_dst.write_bytes(meta_src.read_bytes())
        except Exception as e:
            print(f"[pipeline] не смог скопировать meta: {e}", file=sys.stderr)

    # 3. CBZ
    cbz_path = OUTPUT_ROOT / f"{chapter_id}.cbz"
    if args.skip_cbz:
        print("[pipeline] skip cbz")
    else:
        build_cbz(output_dir, cbz_path)

    # 4. Publish — копия в Manga/<title>/Vol.X Ch.Y — <chapter_title>.cbz
    publish_to_manga_folder(chapter_id, output_dir, cbz_path if cbz_path.exists() else None)

    print("[pipeline] готово")
    return 0


if __name__ == "__main__":
    sys.exit(main())
