"""
Перевод страниц через manga-image-translator + Gemini.

Что делает:
  1. Подкладывает локальный клон `manga-image-translator/` в sys.path.
  2. Регистрирует наш кастомный OtakuGalGeminiTranslator поверх дефолтного `gemini`.
  3. Гонит папку input/<chapter_id>/ через MangaTranslatorLocal в output/<chapter_id>/.

Кастомный translator:
  - Шлёт ВЕСЬ список фраз страницы одним JSON-вызовом (batch).
  - Использует response_mime_type=application/json + response_schema.
  - Кеш в SQLite: ключ = sha256(оригинал + системный промпт), значение = перевод.
  - System prompt включает контекст тайтла, персонажей, правила про SFX и длину.

Apple Silicon: device='mps' если доступен, иначе 'cpu'.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import List

from dotenv import load_dotenv

ROOT = Path(__file__).parent
MIT_ROOT = ROOT / "manga-image-translator"
if str(MIT_ROOT) not in sys.path:
    sys.path.insert(0, str(MIT_ROOT))


from tunnel import enable_proxy_env as _enable_warp_for_gemini  # noqa: E402

load_dotenv(ROOT / ".env")


# --------------------------------------------------------------------------------------
# Системный промпт. Контекст тайтла + поведение модели.
# Отдельная константа, чтобы её хеш стабильно входил в ключ кеша.
# --------------------------------------------------------------------------------------

SYSTEM_PROMPT = """\
Ты профессиональный переводчик манги на русский. На вход приходит ТЕКСТ ИЗ
ОБЛАЧКОВ МАНГИ — обычно английский сканлейт (с японского), иногда оригинальный
японский, иногда смесь. OCR может слегка искажать буквы (HOURE вместо YOU'RE,
SEATTOBE вместо SEAT TO BE, IM вместо I'M). Восстанавливай очевидный смысл
и переводи.

ТАЙТЛ: "Otaku ni Yasashii Gal wa Inai!?" — школьный ромком про отаку и гяру.

ГЛАВНЫЕ ПЕРСОНАЖИ:
- Такуя Сэо — ПАРЕНЬ. Стеснительный отаку. Говорит нервно, часто запинается,
  вежливые формы. Глаголы прош. вр. в МУЖСКОМ роде ("я подумал", "я сделал").
  Робкие фразы: "э-э", "ну...", "извини...".
- Кэй Аманэ — ДЕВУШКА. Тихая отаку. Мягкая речь, легко смущается. Глаголы прош.
  вр. в ЖЕНСКОМ роде ("я подумала", "я сделала"). Спокойный тон, уменьшительные.
- Котоко Идзити — ДЕВУШКА. Энергичная гяру. Разговорный сленг, восклицания.
  Глаголы прош. вр. в ЖЕНСКОМ роде. Сленг: "блин!", "ваще!", "офигеть!", "чё?".

РОД ГЛАГОЛОВ:
1. Стиль Такуи (робкие "э-э", "ну...", вежливые конструкции) → мужской род.
2. Все остальные реплики от первого лица → ЖЕНСКИЙ род (две главные героини).
3. Если неясно — ЖЕНСКИЙ род по умолчанию.

ПРАВИЛА ПЕРЕВОДА:
1. Сохраняй характер речи персонажа — это важнее буквальной точности.
2. SFX (звуковые эффекты) — ТРАНСЛИТЕРИРУЙ кириллицей: "ドキドキ" → "доки-доки",
   "ガタッ" → "гата". Английские SFX тоже: "BLUSH" → "румянец/тык", "STARE" →
   "взгляд", "NOD" → "кивок", "POP" → "хлоп".
3. Длина перевода — приближай к длине оригинала, чтобы влезло в облачко.
4. Не добавляй пояснений в скобках. Сохраняй пунктуацию-эмоцию: "!?", "...".
5. Имена собственные (Tokyo, Nagoya, Akagi) — переводи стандартно (Токио,
   Нагоя, Акаги). НЕ транслитерируй слогами ("Токьо" — плохо).
6. Технические маркеры страницы ("#220 END", "Period 56", "Vol.8 Ch.56") —
   переводи буквально ("№220 КОНЕЦ", "Период 56").

ВАЖНО — НИКОГДА НЕ ВОЗВРАЩАЙ ПУСТУЮ СТРОКУ:
- Даже если на входе OCR-каша (RVNH, Yeaun, #220|END, meoun, tlmosanata) —
  попробуй угадать смысл из контекста других фраз и дай хоть какой-то перевод
  (например "..." или транслитерацию).
- Если совсем непонятно — верни ОРИГИНАЛ как есть (не пустую строку).
- Пустая строка стирает облачко в манге → читатель видит дыру. ЛУЧШЕ оставить
  оригинал чем пустоту.

ФОРМАТ ВВОДА: JSON-массив строк (фразы одной страницы по порядку).
ФОРМАТ ВЫВОДА: JSON-массив строк той же длины, в том же порядке.
"""


# --------------------------------------------------------------------------------------
# SQLite-кеш переводов. Один файл cache.db, потокобезопасный через lock.
# --------------------------------------------------------------------------------------

class TranslationCache:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS tr (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        self._conn.commit()

    @staticmethod
    def make_key(original: str, system_prompt: str) -> str:
        h = hashlib.sha256()
        h.update(system_prompt.encode("utf-8"))
        h.update(b"\x00")
        h.update(original.encode("utf-8"))
        return h.hexdigest()

    def get(self, key: str) -> str | None:
        with self._lock:
            cur = self._conn.execute("SELECT value FROM tr WHERE key=?", (key,))
            row = cur.fetchone()
        return row[0] if row else None

    def put_many(self, items: list[tuple[str, str]]) -> None:
        if not items:
            return
        with self._lock:
            self._conn.executemany(
                "INSERT OR REPLACE INTO tr (key, value) VALUES (?, ?)", items
            )
            self._conn.commit()


# --------------------------------------------------------------------------------------
# Кастомный translator. Наследуется от CommonTranslator из manga-image-translator.
# --------------------------------------------------------------------------------------

def _build_translator_class():
    """
    Импорт делаем лениво, потому что manga_translator подтягивает torch и кучу
    тяжёлых зависимостей. Не хотим платить за это при импорте translate.py
    (например когда pipeline.py запущен с --skip-translate).
    """
    from manga_translator.translators.common import CommonTranslator
    from google import genai
    from google.genai import types

    GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
    GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
    if not GEMINI_API_KEY:
        raise RuntimeError(
            "GEMINI_API_KEY не задан. Положи его в .env или в окружение."
        )

    cache_path = ROOT / "cache.db"

    class OtakuGalGeminiTranslator(CommonTranslator):
        # CommonTranslator дергает _LANGUAGE_CODE_MAP[to_lang], чтобы передать
        # человеко-читаемое имя языка. Мы просим только русский, но прописываем
        # карту шире — на случай если manga-image-translator решит передать
        # ещё и from_lang='auto'.
        _LANGUAGE_CODE_MAP = {
            "CHS": "Chinese (Simplified)",
            "CHT": "Chinese (Traditional)",
            "JPN": "Japanese",
            "ENG": "English",
            "RUS": "Russian",
            "KOR": "Korean",
        }
        _MAX_REQUESTS_PER_MINUTE = 60  # 1 запрос/сек в среднем — щадящий режим

        def __init__(self) -> None:
            super().__init__()
            self.client = genai.Client(api_key=GEMINI_API_KEY)
            self.model = GEMINI_MODEL
            self.cache = TranslationCache(cache_path)
            self.logger.info(
                f"OtakuGalGeminiTranslator: model={self.model}, cache={cache_path}"
            )

        async def _translate(
            self, from_lang: str, to_lang: str, queries: List[str]
        ) -> List[str]:
            if not queries:
                return []

            # 1. Заглядываем в кеш. Соберём, что нужно отправить в Gemini.
            # Пустые записи из кеша игнорируем — они могут быть последствием
            # предыдущих сбоев Gemini; при следующем запуске даём шанс
            # переспросить.
            results: list[str | None] = [None] * len(queries)
            to_request_idx: list[int] = []
            for i, q in enumerate(queries):
                cached = self.cache.get(TranslationCache.make_key(q, SYSTEM_PROMPT))
                if cached is not None and cached.strip():
                    results[i] = cached
                else:
                    to_request_idx.append(i)

            if to_request_idx:
                batch = [queries[i] for i in to_request_idx]
                self.logger.info(
                    f"Gemini batch: {len(batch)} новых, {len(queries) - len(batch)} из кеша"
                )
                translated = await self._call_gemini_batch(batch)

                # Защита от молчаливых пропусков: если ВЕСЬ батч пустой
                # (Gemini блокнул safety/упал/вернул мусор), переспрашиваем
                # каждую фразу отдельно — обычно блокирует одна, остальные ОК.
                empty_count = sum(1 for x in translated if not x or not x.strip())
                if empty_count == len(batch) and len(batch) > 1:
                    self.logger.warning(
                        f"Gemini вернул {empty_count}/{len(batch)} пустых — "
                        f"переспрашиваю по одной фразе"
                    )
                    translated = []
                    for q in batch:
                        single = await self._call_gemini_batch([q])
                        translated.append(single[0] if single else "")

                # КЛЮЧЕВОЙ FALLBACK: если перевод всё ещё пустой — подставляем
                # ОРИГИНАЛ. Иначе MIT инпаинтит облачко (стирает фон) и не
                # вписывает новый текст → дыра в облачке. Лучше оставить
                # английский (или OCR-кашу) чем пустоту: читатель хотя бы
                # видит что-то, и проще понять что место спорное.
                final_translations: list[str] = []
                fallback_count = 0
                for src, dst in zip(batch, translated):
                    if dst and dst.strip():
                        final_translations.append(dst)
                    else:
                        final_translations.append(src)  # ← оригинал как fallback
                        fallback_count += 1
                translated = final_translations

                if fallback_count:
                    self.logger.warning(
                        f"⚠ {fallback_count}/{len(batch)} фраз — fallback на оригинал "
                        f"(Gemini не справился): {[b[:40] for b, t in zip(batch, translated) if b == t][:3]}"
                    )

                # В кеш сохраняем ВСЕ записи (включая fallback на оригинал) —
                # это валидный результат, не нужно повторять при следующем
                # запуске. Если хочется обновить — удаляй cache.db вручную.
                cache_items = [
                    (TranslationCache.make_key(src, SYSTEM_PROMPT), dst)
                    for src, dst in zip(batch, translated)
                ]
                self.cache.put_many(cache_items)

                for i, dst in zip(to_request_idx, translated):
                    results[i] = dst

            # Никогда не возвращаем пустые — заменяем на оригинал из queries.
            return [
                r if (r and str(r).strip()) else q
                for r, q in zip(results, queries)
            ]

        async def _call_gemini_batch(self, batch: List[str]) -> List[str]:
            """
            Шлём в Gemini батч в виде JSON-массива, ждём JSON-массив той же длины.
            Retry на 503/UNAVAILABLE/429 — модель часто перегружена в пиковые часы.
            Если на выходе мусор — fallback: пустые строки на места провалов.
            """
            user_payload = json.dumps(batch, ensure_ascii=False)

            config = types.GenerateContentConfig(
                system_instruction=SYSTEM_PROMPT,
                response_mime_type="application/json",
                response_schema={
                    "type": "ARRAY",
                    "items": {"type": "STRING"},
                },
                temperature=0.3,
                safety_settings=[
                    {"category": c, "threshold": "BLOCK_ONLY_HIGH"}
                    for c in (
                        "HARM_CATEGORY_HARASSMENT",
                        "HARM_CATEGORY_HATE_SPEECH",
                        "HARM_CATEGORY_SEXUALLY_EXPLICIT",
                        "HARM_CATEGORY_DANGEROUS_CONTENT",
                    )
                ],
            )

            from google.genai import errors as genai_errors

            # 6 попыток с растущим backoff: 2/5/12/25/45/90 сек = до ~3 мин на батч.
            # Раньше было 4 попытки 3/8/20/45 — на пиковые 5xx этого не хватало,
            # страница теряла перевод. Дополнительные 2 попытки покрывают
            # типичные «оранжевые» окна Gemini (отсутствие capacity 1-2 минуты).
            backoffs = [2, 5, 12, 25, 45, 90]
            response = None
            last_exc: Exception | None = None
            for attempt, delay in enumerate(backoffs, start=1):
                try:
                    response = await self.client.aio.models.generate_content(
                        model=self.model,
                        contents=user_payload,
                        config=config,
                    )
                    break
                except genai_errors.ServerError as e:
                    last_exc = e
                    code = getattr(e, "status_code", None) or getattr(e, "code", None)
                    self.logger.warning(
                        f"Gemini {code} (attempt {attempt}/{len(backoffs)}): {str(e)[:200]}; "
                        f"retry in {delay}s"
                    )
                    if attempt == len(backoffs):
                        break
                    await asyncio.sleep(delay)
                except genai_errors.ClientError as e:
                    code = getattr(e, "status_code", None) or getattr(e, "code", None)
                    if code == 429:
                        last_exc = e
                        self.logger.warning(
                            f"Gemini 429 rate limit (attempt {attempt}/{len(backoffs)}); "
                            f"retry in {delay}s"
                        )
                        if attempt == len(backoffs):
                            break
                        await asyncio.sleep(delay)
                    else:
                        self.logger.error(f"Gemini client error {code}: {str(e)[:200]}")
                        return [""] * len(batch)

            if response is None:
                self.logger.error(
                    f"Gemini failed after {len(backoffs)} retries: {last_exc}; "
                    "возвращаю пустые переводы для батча"
                )
                return [""] * len(batch)

            text = (response.text or "").strip()
            # Логируем когда Gemini ответил пустым: либо safety filter,
            # либо модель не нашла что сказать. Без лога это просто исчезает.
            if not text:
                # response.candidates[0].finish_reason: "SAFETY", "MAX_TOKENS" и т.п.
                fr = "?"
                try:
                    fr = str(response.candidates[0].finish_reason)
                except Exception:
                    pass
                self.logger.warning(
                    f"Gemini ответил пустым телом (finish_reason={fr}); "
                    f"батч из {len(batch)} фраз потерян, первая фраза: {batch[0][:50]!r}"
                )
                return [""] * len(batch)
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError as exc:
                self.logger.error(
                    f"Gemini вернул не-JSON: {exc}; raw={text[:200]!r}"
                )
                return [""] * len(batch)

            if not isinstance(parsed, list):
                self.logger.error(f"Ожидал JSON-массив, получил {type(parsed)}")
                return [""] * len(batch)

            # Подгоняем длину под батч
            parsed = [str(x) if x is not None else "" for x in parsed]
            if len(parsed) < len(batch):
                parsed.extend([""] * (len(batch) - len(parsed)))
            elif len(parsed) > len(batch):
                parsed = parsed[: len(batch)]
            return parsed

    return OtakuGalGeminiTranslator


def _register_custom_translator() -> None:
    """
    Подменяем дефолтный gemini-translator в manga-image-translator на наш.
    Делается через словарь TRANSLATORS и translator_cache, чтобы dispatch()
    в их пайплайне взял именно наш класс.

    Заодно глушим OfflineTranslator.download — MIT в `prepare_translation`
    обходит всю цепочку и может зацепить sugoi/jparacrawl (1.5 ГБ моделей),
    которые нам с Gemini не нужны.
    """
    from manga_translator import translators as mit_translators
    from manga_translator.config import Translator
    from manga_translator.translators.common import OfflineTranslator

    cls = _build_translator_class()
    mit_translators.TRANSLATORS[Translator.gemini] = cls
    mit_translators.GPT_TRANSLATORS[Translator.gemini] = cls
    mit_translators.translator_cache.pop(Translator.gemini, None)

    async def _noop_download(self, *args, **kwargs):
        return

    OfflineTranslator.download = _noop_download


# --------------------------------------------------------------------------------------
# Запуск manga-image-translator на папке.
# --------------------------------------------------------------------------------------

def _select_device() -> str:
    """MPS если доступен, иначе CPU. Никаких CUDA на Mac."""
    try:
        import torch

        if torch.backends.mps.is_available():
            return "mps"
        return "cpu"
    except Exception:
        return "cpu"


async def _translate_folder_async(input_dir: Path, output_dir: Path, target_lang: str) -> None:
    _enable_warp_for_gemini()
    _register_custom_translator()

    # Импорты тяжёлые — делаем после регистрации translator'а
    from manga_translator.mode.local import MangaTranslatorLocal
    from manga_translator import Config
    from manga_translator.config import (
        TranslatorConfig,
        Translator,
        Renderer,
        RenderConfig,
        InpaintPrecision,
    )

    device = _select_device()
    print(f"[translate] device={device}")

    params = {
        "input": [str(input_dir)],
        "dest": str(output_dir),
        "use_mtpe": False,
        "verbose": False,
        # ignore_errors=true — если на одной странице упал детектор/inpaint/Gemini,
        # MIT не валит весь batch, а пропускает её и продолжает следующие.
        # Для нашего сценария (22 страницы за прогон) это критично.
        "ignore_errors": True,
        "overwrite": False,
        "format": "png",
        "save_quality": 100,
        "attempts": 0,
        "model_dir": str(ROOT / "manga-image-translator" / "models"),
        # MangaTranslator при __init__ читает 'use_gpu' / 'device'
        "use_gpu": device != "cpu",
        "use_gpu_limited": False,
        "device": device,
        "batch_size": 1,
        # обязательные init-параметры с CLI-default'ами (см. args.py)
        "kernel_size": 3,
        "context_size": 0,
        "batch_concurrent": False,
        "disable_memory_optimization": False,
        # Кириллический шрифт для рендера в облачках. PT Sans Narrow Bold —
        # компактный (входит в облачко), жирный (читается), полная поддержка
        # русского. По умолчанию MIT берёт comic shanns 2 / anime_ace, без кириллицы.
        "font_path": str(ROOT / "fonts" / "PTSansNarrow-Bold.ttf"),
        "pre_dict": None,
        "post_dict": None,
        "skip_no_text": False,
        "save_text": False,
        "load_text": False,
        "save_text_file": "",
        "prep_manual": False,
        "text_regions": None,
    }

    config = Config(
        translator=TranslatorConfig(
            translator=Translator.gemini,
            target_lang=target_lang,
        ),
        render=RenderConfig(
            renderer=Renderer.manga2Eng if target_lang == "ENG" else Renderer.default,
            font_size_offset=0,
        ),
    )

    # ──────── ускорение: уменьшаем разрешение inpainting ─────────────────
    # Default 2048 — медленно (~6с/страница на M3 Pro). 1024 даёт x3-4 ускорение
    # с минимальной потерей качества: текстовые облачка манги мелкие, full-2K
    # переoverkill. На MPS bf16 не работает, fp16 норм.
    inpaint_size = int(os.environ.get("MIT_INPAINTING_SIZE", "1024"))
    if hasattr(config, "inpainter"):
        try:
            config.inpainter.inpainting_size = inpaint_size
            config.inpainter.inpainting_precision = (
                InpaintPrecision.fp16 if device == "mps" else InpaintPrecision.fp32
            )
        except Exception:
            pass

    # detection_size: default 2048; уменьшаем чтобы детектор был быстрее на крупных страницах
    detect_size = int(os.environ.get("MIT_DETECTION_SIZE", "1536"))
    if hasattr(config, "detector"):
        try:
            config.detector.detection_size = detect_size
        except Exception:
            pass

    # MangaTranslatorLocal.translate_path игнорирует наш Python-объект Config и
    # читает его из файла через params['config_file']. Сериализуем туда JSON.
    config_path = ROOT / ".mit_runtime_config.json"
    config_path.write_text(config.model_dump_json())
    params["config_file"] = str(config_path)

    translator = MangaTranslatorLocal(params)

    output_dir.mkdir(parents=True, exist_ok=True)
    await translator.translate_path(str(input_dir), str(output_dir), params)


def translate_folder(input_dir: Path, output_dir: Path, target_lang: str = "RUS") -> None:
    """Синхронная обёртка для pipeline.py."""
    asyncio.run(_translate_folder_async(input_dir, output_dir, target_lang))


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Перевести папку страниц через MIT+Gemini")
    parser.add_argument("input_dir", help="Папка со страницами (input/<chapter_id>)")
    parser.add_argument("output_dir", help="Куда писать (output/<chapter_id>)")
    parser.add_argument(
        "--target-lang",
        default=os.environ.get("TARGET_LANG", "RUS"),
        help="Код языка-цели (RUS по умолчанию)",
    )
    args = parser.parse_args()

    translate_folder(Path(args.input_dir), Path(args.output_dir), args.target_lang)
    return 0


if __name__ == "__main__":
    sys.exit(main())
