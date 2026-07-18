#!/usr/bin/env python3
"""
Media Cleaner: a local, safe toolkit for a personal media archive.

Covers four jobs, all writing into a separate folder and never touching the
originals: compressing photos and videos, finding exact duplicates by SHA256,
finding visually similar photos and videos by perceptual hash, and remuxing or
re-encoding movies and TV series into MKV with explicit audio/subtitle track
selection. Run without arguments to open the interactive wizard.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import html
import json
import os
import platform
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote


VIDEO_EXTENSIONS = {".mov", ".mp4", ".m4v", ".avi", ".mkv", ".mts", ".m2ts", ".3gp"}
JPEG_EXTENSIONS = {".jpg", ".jpeg"}
COPY_IMAGE_EXTENSIONS = {".png", ".heic", ".heif"}
SIMILAR_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".heic", ".webp"}
RAW_EXTENSIONS = {
    ".3fr", ".arw", ".cr2", ".cr3", ".dng", ".erf", ".kdc", ".mef",
    ".mos", ".mrw", ".nef", ".nrw", ".orf", ".pef", ".raf", ".raw",
    ".rw2", ".sr2", ".srf", ".x3f",
}
SUMMARY_FIELDS = [
    "source", "destination", "category", "action", "status",
    "original_bytes", "output_bytes", "saved_bytes", "note",
]
REPORT_NAMES = ("summary.csv", "duplicates_report.csv", "errors.log")

# --fast-duplicates pre-filter: how much of a file is read for the partial hash,
# and the size below which reading the file twice costs more than hashing it once.
PARTIAL_HASH_CHUNK_BYTES = 1024 * 1024
PARTIAL_HASH_MIN_FILE_BYTES = 2 * 1024 * 1024


@dataclass
class Counters:
    total: int = 0
    videos: int = 0
    photos: int = 0
    copied: int = 0
    duplicates: int = 0
    errors: int = 0
    original_bytes: int = 0
    output_bytes: int = 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Локальный инструмент для медиаархива: поиск точных дубликатов, "
            "поиск визуально похожих фото и видео, сжатие фото и видео, "
            "пересборка фильмов и сериалов в MKV с выбором дорожек. "
            "Оригиналы никогда не меняются и не удаляются."
        ),
        epilog=(
            "Примеры:\n"
            "  mc --wizard\n"
            "  mc INPUT OUTPUT --dry-run\n"
            "  mc INPUT OUTPUT --duplicates-only\n"
            "  mc INPUT OUTPUT --move-duplicates --dry-run\n"
            "  mc INPUT OUTPUT --review-duplicates\n"
            "  mc INPUT OUTPUT --find-similar-photos --similar-threshold 5\n"
            "  mc INPUT OUTPUT --trash-similar-from-report --dry-run\n"
            "  mc INPUT OUTPUT --find-similar-videos\n"
            "  mc INPUT OUTPUT --trash-similar-videos-from-report --dry-run\n"
            "  mc --quick-file /path/to/video.mov --video-crf 31 --video-preset fast\n"
            "\n"
            "Запуск без аргументов открывает пошаговый мастер: mc"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("input", nargs="?", type=Path, help="Папка с оригиналами")
    parser.add_argument("output", nargs="?", type=Path, help="Отдельная папка для результата")
    parser.add_argument(
        "--wizard", action="store_true",
        help="Запустить пошаговый интерактивный мастер",
    )
    parser.add_argument(
        "--quick-file", type=Path,
        help="Быстро сжать один видеофайл и сохранить рядом с оригиналом",
    )
    parser.add_argument(
        "--move-duplicates", action="store_true",
        help="Безопасно переместить лишние точные дубликаты в Duplicates_To_Delete",
    )
    parser.add_argument(
        "--review-duplicates", action="store_true",
        help="Создать удобный HTML-обзор и Finder-ссылки для точных дубликатов",
    )
    parser.add_argument(
        "--find-similar-photos", action="store_true",
        help="Найти визуально похожие фото для ручной проверки",
    )
    parser.add_argument(
        "--review-similar-photos", action="store_true",
        help=(
            "Построить HTML-обзор похожих фото по готовому "
            "reports/similar_photos_report.csv: группы в один ряд, крупные миниатюры"
        ),
    )
    parser.add_argument(
        "--best-shot", action="store_true",
        help=(
            "Вместе с --find-similar-photos: выбирать keep_candidate по резкости, "
            "разрешению и размеру, а не по порядку"
        ),
    )
    parser.add_argument(
        "--trash-similar-from-report", action="store_true",
        help="Переместить review_duplicate из отчёта похожих фото в корзину macOS",
    )
    parser.add_argument(
        "--similar-threshold", type=int, default=5, choices=range(0, 65), metavar="0..64",
        help="Чувствительность похожих фото: меньше = строже (по умолчанию: 5)",
    )
    parser.add_argument(
        "--find-similar-videos", action="store_true",
        help="Найти визуально похожие видео для ручной проверки",
    )
    parser.add_argument(
        "--trash-similar-videos-from-report", action="store_true",
        help="Переместить review_duplicate из отчёта похожих видео в корзину macOS",
    )
    parser.add_argument(
        "--similar-video-threshold", type=int, default=8, choices=range(0, 65), metavar="0..64",
        help="Чувствительность похожих видео: меньше = строже (по умолчанию: 8)",
    )
    parser.add_argument(
        "--similar-video-samples", type=int, default=8, choices=range(2, 25), metavar="2..24",
        help="Кол-во sample-кадров для подписи видео (по умолчанию: 8)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Только показать план; ничего не создавать и не изменять",
    )
    parser.add_argument(
        "--duplicates-only", action="store_true",
        help="Только найти точные дубликаты и создать отчёты; файлы не менять",
    )
    parser.add_argument(
        "--video-crf", type=int, default=31, choices=range(18, 36), metavar="18..35",
        help="Качество видео: 18 = выше, 35 = меньше файл (по умолчанию: 31)",
    )
    parser.add_argument(
        "--video-preset",
        choices=("ultrafast", "superfast", "veryfast", "faster", "fast", "medium", "slow"),
        default="fast",
        help="Скорость сжатия ffmpeg (по умолчанию: fast)",
    )
    parser.add_argument(
        "--show-ffmpeg-progress", action="store_true",
        help="Показывать в Terminal обычный прогресс ffmpeg для каждого видео",
    )
    parser.add_argument(
        "--disable-hw-video", action="store_true",
        help="Отключить аппаратное ускорение VideoToolbox (macOS); всегда использовать libx265 (CPU)",
    )
    parser.add_argument(
        "--video-codec",
        choices=("x265", "hevc_videotoolbox"),
        default="x265",
        help=(
            "Видеокодек: x265 (по умолчанию) — автовыбор: на macOS будет выбран "
            "аппаратный HEVC (hevc_videotoolbox), если ffmpeg его поддерживает, "
            "иначе libx265 + CRF; для гарантированного libx265 добавьте "
            "--disable-hw-video. Либо hevc_videotoolbox — явно аппаратное HEVC "
            "(только macOS, использует --video-qp)"
        ),
    )
    parser.add_argument(
        "--video-qp", type=int, default=60, metavar="20..90",
        help=(
            "QP-качество для hevc_videotoolbox (20–90, по умолчанию: 60). "
            "Игнорируется при --video-codec x265."
        ),
    )
    parser.add_argument(
        "--list-encoders", action="store_true",
        help="Показать таблицу видеокодировщиков и результат их проверки на этой машине",
    )
    parser.add_argument(
        "--refresh-hw-cache", action="store_true",
        help="Перепроверить аппаратные кодировщики заново, не доверяя сохранённому кэшу",
    )
    parser.add_argument(
        "--fast-duplicates", action="store_true",
        help=(
            "Ускорить поиск точных дубликатов: сначала сравнить первый и последний "
            "мегабайт файла, полный SHA256 считать только для совпавших"
        ),
    )
    parser.add_argument(
        "--skip-already-compressed", action="store_true",
        help=(
            "Не пережимать то, что уже эффективно сжато (HEVC/AV1 с низким битрейтом, "
            "экономные JPEG); такие файлы копируются без изменений"
        ),
    )
    parser.add_argument(
        "--undo-move-duplicates", action="store_true",
        help="Вернуть файлы из Duplicates_To_Delete на прежние места по reports/moved_duplicates.csv",
    )
    parser.add_argument(
        "--image-quality", type=int, default=85, choices=range(1, 101), metavar="1..100",
        help="Качество JPEG от 1 до 100 (по умолчанию: 85)",
    )
    args = parser.parse_args()
    args.wizard_cancelled = False
    args.movie_mkv = None  # populated by run_wizard_movie_mkv if chosen
    # Duration-diff attr is set by wizard; provide a default for CLI path.
    args._similar_video_dur_diff = 0.15
    # Diagnostic listing is a standalone action: it needs no INPUT/OUTPUT and
    # must not be dragged into wizard or mode validation below.
    if args.list_encoders:
        return args
    # A bare invocation with no arguments at all starts the interactive wizard,
    # so the installed `mc` command works without remembering any flags.
    if len(sys.argv) == 1:
        args.wizard = True
    if args.quick_file and (args.input is not None or args.output is not None):
        parser.error("с --quick-file не нужно указывать INPUT и OUTPUT")
    special_modes = [
        args.move_duplicates, args.duplicates_only, args.review_duplicates,
        args.find_similar_photos, args.trash_similar_from_report,
        args.find_similar_videos, args.trash_similar_videos_from_report,
        args.undo_move_duplicates, args.review_similar_photos,
    ]
    if args.best_shot and not args.find_similar_photos:
        parser.error("--best-shot работает только вместе с --find-similar-photos")
    if args.quick_file and (args.wizard or any(special_modes)):
        parser.error("--quick-file нельзя сочетать с другими режимами")
    if sum(bool(mode) for mode in special_modes) > 1:
        parser.error("выберите только один режим работы с дубликатами или похожими файлами")
    if args.dry_run and (
        args.review_duplicates or args.find_similar_photos
        or args.find_similar_videos or args.review_similar_photos
    ):
        parser.error("--dry-run не нужен для отчётных режимов: они и так не меняют оригиналы")
    if args.wizard and (args.input is not None or args.output is not None):
        parser.error("с --wizard не нужно указывать INPUT и OUTPUT")
    if args.wizard:
        try:
            wizard_args = run_wizard(args)
        except (EOFError, KeyboardInterrupt):
            print("\nЗапуск отменён.")
            args.wizard_cancelled = True
            return args
        if wizard_args is None:
            args.wizard_cancelled = True
            return args
        return wizard_args
    if any(special_modes) and (args.input is None or args.output is None):
        parser.error("для выбранного режима нужны INPUT и OUTPUT")
    if args.quick_file is None and (args.input is None or args.output is None):
        parser.error("укажите INPUT и OUTPUT или используйте --wizard")
    return args


def _strip_surrounding_quotes(token: str) -> str:
    token = token.strip()
    if len(token) >= 2 and token[0] == token[-1] and token[0] in {"'", '"'}:
        return token[1:-1].strip()
    return token


def _split_terminal_line(value: str) -> list[str]:
    """
    Split ONE terminal input line into raw path tokens.

    The split rules differ per platform and MUST NOT be mixed up:

    * POSIX (macOS, Linux): Terminal drag-and-drop backslash-escapes spaces
      (`/Users/me/My\\ Folder`), so POSIX-mode shlex is exactly right — it
      un-escapes the backslashes and yields one token per dragged file.
    * Windows (os.name == "nt"): the backslash is the PATH SEPARATOR, so POSIX
      shlex would silently eat it and turn C:\\Users\\Ilya\\Video.mkv into
      C:UsersIlyaVideo.mkv — a wrong path with no error at all. Non-POSIX mode
      keeps backslashes intact, but it also keeps the surrounding quotes on
      each token, so they are stripped by hand afterwards.

    Raises ValueError on an unbalanced quote (callers fall back to the raw line).
    """
    cleaned = value.strip()
    if not cleaned:
        return []
    if os.name == "nt":
        return [_strip_surrounding_quotes(part) for part in shlex.split(cleaned, posix=False)]
    return shlex.split(cleaned)


def clean_terminal_path(value: str) -> Path:
    cleaned = value.strip()
    if len(cleaned) >= 2 and cleaned[0] == cleaned[-1] and cleaned[0] in {"'", '"'}:
        cleaned = cleaned[1:-1].strip()
    else:
        try:
            parts = _split_terminal_line(cleaned)
            if len(parts) == 1:
                cleaned = parts[0]
        except ValueError:
            cleaned = cleaned.strip("'\"")
    return Path(cleaned).expanduser()


def clean_terminal_paths(value: str) -> list[Path]:
    """
    Parse ONE terminal line that may contain SEVERAL dragged paths.

    macOS Terminal drops multiple files as a single space-separated line,
    quoting or backslash-escaping any path that contains spaces, so each token
    becomes one path. Falls back to treating the whole line as a single path if
    it can't be parsed, or if the line looks like one unquoted path that simply
    contains spaces (a hand-pasted `/Users/a/My Folder`).
    """
    cleaned = value.strip()
    if not cleaned:
        return []
    try:
        parts = _split_terminal_line(cleaned)
    except ValueError:
        return [clean_terminal_path(cleaned)]
    if not parts:
        return []
    if len(parts) > 1:
        # A hand-pasted path with spaces and no quoting splits into several
        # tokens that exist nowhere. Only then fall back to the whole line —
        # requiring that NO token exists keeps genuine multi-file drops intact.
        if not any(Path(part).expanduser().exists() for part in parts):
            whole = Path(_strip_surrounding_quotes(cleaned)).expanduser()
            if whole.exists():
                return [whole]
    return [Path(part).expanduser() for part in parts]


def prompt_menu(prompt: str, choices: set[str], default: str | None = None) -> str:
    while True:
        answer = input(prompt).strip()
        if not answer and default is not None:
            return default
        if answer in choices:
            return answer
        print("Введите один из предложенных номеров.")


def prompt_number(prompt: str, minimum: int, maximum: int) -> int:
    while True:
        answer = input(prompt).strip()
        try:
            value = int(answer)
        except ValueError:
            print(f"Введите целое число от {minimum} до {maximum}.")
            continue
        if minimum <= value <= maximum:
            return value
        print(f"Введите число от {minimum} до {maximum}.")


def prompt_yes_no(prompt: str, default: str | None = None) -> bool:
    while True:
        answer = input(prompt).strip().lower()
        if not answer and default in {"yes", "no"}:
            return default == "yes"
        if answer in {"yes", "y"}:
            return True
        if answer in {"no", "n"}:
            return False
        print("Ответьте yes или no.")


def run_wizard(args: argparse.Namespace) -> argparse.Namespace | None:
    print("\nMedia Cleaner: пошаговый безопасный запуск\n")
    print("Что сделать?")
    print("  1. Только найти дубликаты")
    print("  2. Dry-run сжатия")
    print("  3. Реальное сжатие")
    print("  4. Быстро сжать один видеофайл")
    print("  5. Безопасно убрать лишние дубликаты")
    print("  6. Просмотреть точные дубликаты удобно")
    print("  7. Найти визуально похожие фото")
    print("  8. Удалить похожие фото из отчёта в корзину")
    print("  9. Найти визуально похожие видео")
    print(" 10. Удалить похожие видео из отчёта в корзину")
    print(" 11. Пересобрать фильмы/сериалы в MKV: выбор дорожек, со сжатием или без")
    print(" 12. Выйти")
    mode = prompt_menu(
        "Выберите 1-12: ",
        {"1","2","3","4","5","6","7","8","9","10","11","12"},
    )
    if mode == "12":
        print("Запуск отменён.")
        return None

    if mode == "4":
        return run_wizard_quick_file(args)
    if mode == "5":
        return run_wizard_move_duplicates(args)
    if mode == "6":
        return run_wizard_review_duplicates(args)
    if mode == "7":
        return run_wizard_similar_photos(args)
    if mode == "8":
        return run_wizard_trash_similar(args)
    if mode == "9":
        return run_wizard_similar_videos(args)
    if mode == "10":
        return run_wizard_trash_similar_videos(args)
    if mode == "11":
        return run_wizard_movie_mkv(args)

    while True:
        input_value = input("\nВставьте или перетащите input-папку в Terminal: ")
        if not input_value.strip():
            print("Укажите путь к input-папке.")
            continue
        input_dir = clean_terminal_path(input_value).resolve()
        if input_dir.is_dir():
            break
        print(f"Папка не найдена: {input_dir}")

    suggested_output = input_dir.parent / f"{input_dir.name} compressed"
    while True:
        output_value = input(
            f"Output-папка [Enter = {suggested_output}]: "
        )
        output_dir = (
            suggested_output if not output_value.strip()
            else clean_terminal_path(output_value)
        ).resolve()
        try:
            validate_paths(input_dir, output_dir)
        except ValueError as exc:
            print(f"Нельзя использовать этот output: {exc}")
            continue
        break

    mode_name = {
        "1": "только поиск дубликатов",
        "2": "dry-run сжатия",
        "3": "реальное сжатие",
    }[mode]

    if mode == "1":
        video_crf = args.video_crf
        image_quality = args.image_quality
        video_preset = args.video_preset
        show_progress = False
    else:
        show_progress = prompt_yes_no("Показывать подробный прогресс ffmpeg? yes/no [no]: ", "no")
        print("\nКачество:")
        print("  1. Осторожно: video_crf=28, image_quality=90")
        print("  2. Нормально: video_crf=30, image_quality=85")
        print("  3. Сильно: video_crf=31, image_quality=85")
        print("  4. Очень сильно: video_crf=32, image_quality=80")
        print("  5. Ввести вручную")
        quality_choice = prompt_menu("Выберите 1-5 [Enter = 3]: ", set("12345"), "3")
        quality_presets = {
            "1": (28, 90), "2": (30, 85), "3": (31, 85), "4": (32, 80),
        }
        if quality_choice == "5":
            video_crf = prompt_number("Video CRF (18-35): ", 18, 35)
            image_quality = prompt_number("Качество JPEG (1-100): ", 1, 100)
        else:
            video_crf, image_quality = quality_presets[quality_choice]

        print("\nСкорость сжатия видео:")
        print("  1. fast (по умолчанию)")
        print("  2. medium")
        print("  3. slow")
        print("  4. faster")
        preset_choice = prompt_menu("Выберите 1-4 [Enter = 1]: ", set("1234"), "1")
        video_preset = {"1": "fast", "2": "medium", "3": "slow", "4": "faster"}[preset_choice]

        # --- codec selection for modes 2-3 ---
        print("\nКодек видео:")
        print("  1. Автоматически: аппаратный кодек, если доступен, иначе libx265 (CPU) [по умолчанию]")
        print("  2. hevc_videotoolbox (аппаратный HEVC, только macOS, использует QP)")
        print("  3. libx265 (только процессор, медленно, точный CRF)")
        codec_choice_w = prompt_menu("Выберите 1-3 [Enter = 1]: ", {"1", "2", "3"}, "1")
        wizard_video_codec = "hevc_videotoolbox" if codec_choice_w == "2" else "x265"
        # Option 3 is exactly --disable-hw-video: same auto path, HW forbidden.
        wizard_disable_hw = codec_choice_w == "3"
        if wizard_video_codec == "hevc_videotoolbox":
            if not is_macos():
                print("Ошибка: hevc_videotoolbox доступен только на macOS. Выберите x265.",
                      file=sys.stderr)
                return None
            if not ffmpeg_supports_encoder("hevc_videotoolbox"):
                print("Ошибка: ваш ffmpeg не поддерживает hevc_videotoolbox. "
                      "Установите: brew install ffmpeg", file=sys.stderr)
                return None
            print("\nКачество (QP для hevc_videotoolbox):")
            print("  1. Осторожно (лучше качество): QP 50")
            print("  2. Нормально (баланс): QP 60")
            print("  3. Сильно (меньше файл): QP 70")
            print("  4. Ввести вручную")
            qp_choice_w = prompt_menu("Выберите 1-4 [Enter = 2]: ", {"1", "2", "3", "4"}, "2")
            qp_presets_w = {"1": 50, "2": 60, "3": 70}
            if qp_choice_w == "4":
                wizard_video_qp = prompt_number("QP (20-90): ", 20, 90)
            else:
                wizard_video_qp = qp_presets_w[qp_choice_w]
        else:
            wizard_video_qp = 0

    print("\nИтоговые настройки:")
    print(f"  Input: {input_dir}")
    print(f"  Output: {output_dir}")
    print(f"  Режим: {mode_name}")
    if mode == "1":
        print("  video_crf: не используется")
        print("  video_preset: не используется")
        print("  image_quality: не используется")
    else:
        if wizard_video_codec == "hevc_videotoolbox":
            print(f"  Кодек: hevc_videotoolbox, q:v={wizard_video_qp}")
            print(f"  video_crf: {video_crf} (используется только для имён файлов)")
        else:
            # Resolve the encoder NOW so the real codec is visible before YES,
            # not only after the run starts.
            wizard_encoder = select_video_encoder(disable_hw=wizard_disable_hw)
            for line in quality_mode_display(
                wizard_encoder, video_crf, video_preset, explicit_qp=None
            ):
                print(line)
            print(f"  video_crf: {video_crf}")
            print(f"  video_preset: {video_preset}")
        print(f"  image_quality: {image_quality}")
        print(f"  show_ffmpeg_progress: {'да' if show_progress else 'нет'}")
    print(f"  Dry-run: {'да' if mode == '2' else 'нет'}")
    print("  Удаление файлов: выключено")
    confirmation = input("\nНапишите YES, чтобы начать: ").strip()
    if confirmation != "YES":
        print("Запуск отменён. Ничего не изменено.")
        return None

    args.input = input_dir
    args.output = output_dir
    args.dry_run = mode == "2"
    args.duplicates_only = mode == "1"
    args.video_crf = video_crf
    args.video_preset = video_preset
    args.image_quality = image_quality
    args.show_ffmpeg_progress = show_progress
    if mode != "1":
        args.video_codec = wizard_video_codec
        args.video_qp = wizard_video_qp
        args.disable_hw_video = wizard_disable_hw
    return args


def is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def validate_paths(input_dir: Path, output_dir: Path) -> tuple[Path, Path]:
    input_dir = input_dir.expanduser().resolve()
    output_dir = output_dir.expanduser().resolve()
    if not input_dir.is_dir():
        raise ValueError(f"Input-папка не существует или не является папкой: {input_dir}")
    if input_dir == output_dir:
        raise ValueError("Input и output должны быть разными папками.")
    if is_relative_to(output_dir, input_dir) or is_relative_to(input_dir, output_dir):
        raise ValueError("Для безопасности input и output не должны находиться друг внутри друга.")
    return input_dir, output_dir


def collect_files(input_dir: Path) -> list[Path]:
    files: list[Path] = []
    for root, directories, names in os.walk(input_dir, followlinks=False):
        directories.sort()
        names.sort()
        root_path = Path(root)
        files.extend(root_path / name for name in names)
    return files


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def partial_hash_file(path: Path, size: int) -> str:
    """
    Cheap pre-filter hash: file size plus the first and last megabyte.

    Two byte-identical files ALWAYS produce the same partial hash, so this can
    only ever narrow a same-size group down — it never splits real duplicates
    apart.  A partial-hash match is never trusted on its own: the caller still
    computes the full SHA256 for every survivor.
    """
    digest = hashlib.sha256()
    digest.update(str(size).encode("ascii"))
    with path.open("rb") as source:
        digest.update(source.read(PARTIAL_HASH_CHUNK_BYTES))
        if size > PARTIAL_HASH_CHUNK_BYTES:
            source.seek(max(0, size - PARTIAL_HASH_CHUNK_BYTES))
            digest.update(source.read(PARTIAL_HASH_CHUNK_BYTES))
    return digest.hexdigest()


def find_duplicates(
    files: list[Path], input_dir: Path, fast: bool = False,
) -> tuple[list[dict[str, object]], int, list[str]]:
    by_size: dict[int, list[Path]] = defaultdict(list)
    errors: list[str] = []
    for path in files:
        try:
            if path.is_symlink():
                continue
            by_size[path.stat().st_size].append(path)
        except OSError as exc:
            errors.append(f"Не удалось прочитать размер {path}: {exc}")

    rows: list[dict[str, object]] = []
    duplicate_count = 0
    group_number = 0
    for size, same_size_files in sorted(by_size.items()):
        if len(same_size_files) < 2:
            continue
        by_hash: dict[str, list[Path]] = defaultdict(list)
        if fast and size >= PARTIAL_HASH_MIN_FILE_BYTES:
            # Pre-filter: read 2 MB per file instead of the whole file, and only
            # hash in full those that still look alike.  A lone survivor of a
            # partial-hash bucket cannot have a duplicate, so it is never read
            # again.  The duplicate verdict below still rests on full SHA256.
            by_partial: dict[str, list[Path]] = defaultdict(list)
            for path in same_size_files:
                try:
                    by_partial[partial_hash_file(path, size)].append(path)
                except OSError as exc:
                    errors.append(f"Не удалось вычислить частичный хеш {path}: {exc}")
            candidates = [
                path for group in by_partial.values() if len(group) >= 2 for path in group
            ]
        else:
            candidates = same_size_files
        for path in candidates:
            try:
                by_hash[sha256_file(path)].append(path)
            except OSError as exc:
                errors.append(f"Не удалось вычислить SHA256 {path}: {exc}")
        for digest, matches in sorted(by_hash.items()):
            if len(matches) < 2:
                continue
            group_number += 1
            duplicate_count += len(matches) - 1
            for path in matches:
                rows.append({
                    "group": group_number,
                    "sha256": digest,
                    "size_bytes": size,
                    "path": str(path.relative_to(input_dir)),
                })
    return rows, duplicate_count, errors


def duplicate_only_rows(
    duplicate_rows: list[dict[str, object]],
) -> tuple[list[dict[str, object]], int, int]:
    rows: list[dict[str, object]] = []
    group_sizes: dict[int, tuple[int, int]] = {}
    for row in duplicate_rows:
        group_id = int(row["group"])
        file_size = int(row["size_bytes"])
        copies, _ = group_sizes.get(group_id, (0, file_size))
        group_sizes[group_id] = (copies + 1, file_size)
        rows.append({
            "duplicate_group_id": group_id,
            "file_path": row["path"],
            "file_size": file_size,
            "sha256": row["sha256"],
            "is_first_copy": "yes" if copies == 0 else "no",
        })
    reclaimable_bytes = sum((copies - 1) * size for copies, size in group_sizes.values())
    return rows, len(group_sizes), reclaimable_bytes


def write_duplicates_only_reports(
    output_dir: Path, duplicate_rows: list[dict[str, object]],
    files_scanned: int, duplicate_groups: int, duplicate_copies: int,
    reclaimable_bytes: int,
) -> None:
    reports_dir = output_dir / "reports"
    report_paths = [reports_dir / "duplicates_report.csv", reports_dir / "summary.csv"]
    existing = [path for path in report_paths if path.exists()]
    if existing:
        raise FileExistsError(
            "Отчёты уже существуют и не будут перезаписаны: "
            + ", ".join(str(path) for path in existing)
            + ". Выберите новую output-папку или переместите старые отчёты."
        )

    reports_dir.mkdir(parents=True, exist_ok=True)
    with report_paths[0].open("x", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=[
            "duplicate_group_id", "file_path", "file_size", "sha256", "is_first_copy",
        ])
        writer.writeheader()
        writer.writerows(duplicate_rows)
    with report_paths[1].open("x", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=["metric", "value"])
        writer.writeheader()
        writer.writerows([
            {"metric": "files_scanned", "value": files_scanned},
            {"metric": "duplicate_groups", "value": duplicate_groups},
            {"metric": "extra_copies", "value": duplicate_copies},
            {"metric": "potential_reclaimable_bytes", "value": reclaimable_bytes},
        ])


def run_duplicates_only(
    files: list[Path], input_dir: Path, output_dir: Path, dry_run: bool,
    fast: bool = False,
) -> int:
    raw_rows, duplicate_copies, errors = find_duplicates(files, input_dir, fast=fast)
    report_rows, duplicate_groups, reclaimable_bytes = duplicate_only_rows(raw_rows)

    print("Режим: только поиск точных дубликатов")
    print(f"Input:  {input_dir}")
    print(f"Output: {output_dir}")
    if dry_run:
        print("\nDRY-RUN: папки, файлы и отчёты не создавались.")
    else:
        try:
            write_duplicates_only_reports(
                output_dir, report_rows, len(files), duplicate_groups,
                duplicate_copies, reclaimable_bytes,
            )
        except OSError as exc:
            print(f"Ошибка записи отчётов: {exc}", file=sys.stderr)
            return 1

    print("\nИтог поиска дубликатов:")
    print(f"  Файлов просканировано: {len(files)}")
    print(f"  Групп дубликатов найдено: {duplicate_groups}")
    print(f"  Лишних копий найдено: {duplicate_copies}")
    print(f"  Потенциально можно освободить: {format_bytes(reclaimable_bytes)}")
    print("  Файлы не удалялись.")
    if errors:
        print(f"  Ошибок чтения: {len(errors)}")
        for error in errors:
            print(f"ОШИБКА: {error}", file=sys.stderr)
    return 1 if errors else 0


def duplicate_groups_from_rows(
    duplicate_rows: list[dict[str, object]], input_dir: Path,
) -> list[dict[str, object]]:
    groups: list[dict[str, object]] = []
    by_group_id: dict[int, dict[str, object]] = {}
    for row in duplicate_rows:
        group_id = int(row["group"])
        group = by_group_id.get(group_id)
        if group is None:
            group = {
                "group_id": group_id,
                "sha256": str(row["sha256"]),
                "size_bytes": int(row["size_bytes"]),
                "paths": [],
            }
            by_group_id[group_id] = group
            groups.append(group)
        group["paths"].append(input_dir / str(row["path"]))
    return groups


def split_suffix(path: Path) -> tuple[str, str]:
    return path.stem, path.suffix


def add_numeric_suffix(path: Path, number: int) -> Path:
    stem, suffix = split_suffix(path)
    return path.with_name(f"{stem}_{number}{suffix}")


def unique_path_for_existing_target(base_path: Path, occupied: set[Path] | None = None) -> Path:
    if occupied is None:
        occupied = set()
    candidate = base_path
    counter = 2
    while candidate.exists() or candidate in occupied:
        candidate = add_numeric_suffix(base_path, counter)
        counter += 1
    return candidate


def quick_file_output_path(source: Path, crf: int, preset: str) -> Path:
    base_name = f"{source.stem}_compressed_crf{crf}_{preset}.mp4"
    candidate = source.with_name(base_name)
    counter = 2
    while candidate.exists():
        candidate = source.with_name(f"{source.stem}_compressed_crf{crf}_{preset}_{counter}.mp4")
        counter += 1
    return candidate


def ensure_report_files_absent(output_dir: Path, report_names: list[str]) -> None:
    existing = [output_dir / "reports" / name for name in report_names if (output_dir / "reports" / name).exists()]
    if existing:
        raise FileExistsError(
            "Отчёты уже существуют и не будут перезаписаны: "
            + ", ".join(str(path) for path in existing)
            + ". Выберите другую output-папку."
        )


def _validate_hevc_videotoolbox() -> int:
    """
    Check that hevc_videotoolbox is usable on the current system.
    Returns 0 on success, 2 on failure (prints an error message).
    """
    if not is_macos():
        print(
            "Ошибка: --video-codec hevc_videotoolbox доступен только на macOS.",
            file=sys.stderr,
        )
        return 2
    if not ffmpeg_supports_encoder("hevc_videotoolbox"):
        print(
            "Ошибка: ваш ffmpeg не поддерживает hevc_videotoolbox. "
            "Установите ffmpeg с поддержкой VideoToolbox: brew install ffmpeg",
            file=sys.stderr,
        )
        return 2
    return 0


def run_quick_file_mode(
    source: Path, video_crf: int, video_preset: str, dry_run: bool = False,
    disable_hw: bool = False, video_codec: str = "x265", video_qp: int = 60,
) -> int:
    source = source.expanduser().resolve()
    if not source.is_file() or source.is_symlink():
        print(f"Ошибка: файл не найден или это не обычный файл: {source}", file=sys.stderr)
        return 2

    # --- explicit codec selection ---
    if video_codec == "hevc_videotoolbox":
        rc = _validate_hevc_videotoolbox()
        if rc != 0:
            return rc
        encoder = "hevc_videotoolbox"
        # --video-crf is meaningless with hevc_videotoolbox; warn if non-default.
        if video_crf != 31:
            print(
                f"Предупреждение: --video-crf={video_crf} игнорируется при использовании hevc_videotoolbox. "
                f"Используется q:v={video_qp} (из --video-qp).",
            )
        explicit_qp: int | None = video_qp
    else:
        encoder = select_video_encoder(disable_hw=disable_hw)
        explicit_qp = None
        video_qp = 0  # signal to compress_video: derive from CRF

    output_path = quick_file_output_path(source, video_crf, video_preset)
    print("Режим: быстрое сжатие одного видеофайла")
    print(f"Исходный файл: {source}")
    print(f"Будущий выходной файл: {output_path}")
    for line in quality_mode_display(encoder, video_crf, video_preset, explicit_qp=explicit_qp):
        print(line)
    print("Оригинал не будет изменён.")

    if dry_run:
        print("DRY-RUN: файл не создавался.")
        return 0

    if not reserve_destination(output_path):
        counter = 2
        while True:
            candidate = source.with_name(f"{source.stem}_compressed_crf{video_crf}_{video_preset}_{counter}.mp4")
            if reserve_destination(candidate):
                output_path = candidate
                break
            counter += 1

    try:
        original_size = source.stat().st_size
        output_size, _note = compress_video(
            source, output_path, video_crf, video_preset, True, False, encoder,
            qp=video_qp,
        )
        print(f"Оригинал: {format_bytes(original_size)}")
        print(f"Сжатый файл: {format_bytes(output_size)}")
        print(f"Экономия: {format_bytes(original_size - output_size)}")
        if output_size > original_size:
            print("Предупреждение: сжатый файл оказался больше оригинала.")
        return 0
    except Exception as exc:
        output_path.unlink(missing_ok=True)
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 1


def write_move_duplicates_reports(
    output_dir: Path,
    duplicate_rows: list[dict[str, object]],
    moved_rows: list[dict[str, object]],
    summary_row: dict[str, object],
) -> None:
    reports_dir = output_dir / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    with (reports_dir / "duplicates_report.csv").open("x", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "duplicate_group_id", "file_path", "file_size", "sha256",
                "is_first_copy", "planned_action",
            ],
        )
        writer.writeheader()
        writer.writerows(duplicate_rows)
    with (reports_dir / "moved_duplicates.csv").open("x", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["duplicate_group_id", "original_path", "moved_to", "file_size", "sha256"],
        )
        writer.writeheader()
        writer.writerows(moved_rows)
    with (reports_dir / "summary.csv").open("x", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "total_files_scanned", "duplicate_groups_found",
                "duplicate_extra_copies_found", "bytes_moved_to_duplicates_folder",
                "human_readable_moved_size", "errors_count",
            ],
        )
        writer.writeheader()
        writer.writerow(summary_row)


def run_move_duplicates_mode(
    input_dir: Path, output_dir: Path, dry_run: bool, fast: bool = False,
) -> int:
    input_dir, output_dir = validate_paths(input_dir, output_dir)
    try:
        ensure_report_files_absent(output_dir, ["duplicates_report.csv", "moved_duplicates.csv", "summary.csv"])
    except FileExistsError as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 2

    files = collect_files(input_dir)
    duplicate_rows_flat, extra_copies, duplicate_errors = find_duplicates(files, input_dir, fast=fast)
    duplicate_groups = duplicate_groups_from_rows(duplicate_rows_flat, input_dir)
    planned_rows: list[dict[str, object]] = []
    moved_rows: list[dict[str, object]] = []
    moved_bytes = 0
    errors = list(duplicate_errors)
    occupied_targets: set[Path] = set()
    cleanup_root = output_dir / "Duplicates_To_Delete"

    for group in duplicate_groups:
        first_copy = True
        for source in group["paths"]:
            planned_action = "keep_in_place" if first_copy else (
                "would_move_to_duplicates_folder" if dry_run else "move_to_duplicates_folder"
            )
            planned_rows.append({
                "duplicate_group_id": group["group_id"],
                "file_path": str(source),
                "file_size": group["size_bytes"],
                "sha256": group["sha256"],
                "is_first_copy": "yes" if first_copy else "no",
                "planned_action": planned_action,
            })
            if first_copy:
                first_copy = False
                continue

            planned_destination = unique_path_for_existing_target(
                cleanup_root / source.relative_to(input_dir), occupied_targets
            )
            occupied_targets.add(planned_destination)
            if dry_run:
                moved_rows.append({
                    "duplicate_group_id": group["group_id"],
                    "original_path": str(source),
                    "moved_to": str(planned_destination),
                    "file_size": group["size_bytes"],
                    "sha256": group["sha256"],
                })
                moved_bytes += group["size_bytes"]
                continue

            try:
                planned_destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(source), str(planned_destination))
                moved_rows.append({
                    "duplicate_group_id": group["group_id"],
                    "original_path": str(source),
                    "moved_to": str(planned_destination),
                    "file_size": group["size_bytes"],
                    "sha256": group["sha256"],
                })
                moved_bytes += group["size_bytes"]
            except Exception as exc:
                errors.append(f"{source}: {exc}")

    summary_row = {
        "total_files_scanned": len(files),
        "duplicate_groups_found": len(duplicate_groups),
        "duplicate_extra_copies_found": extra_copies,
        "bytes_moved_to_duplicates_folder": moved_bytes,
        "human_readable_moved_size": format_bytes(moved_bytes),
        "errors_count": len(errors),
    }

    print("Режим: безопасное перемещение лишних дубликатов")
    print(f"Input:  {input_dir}")
    print(f"Output: {output_dir}")
    print(f"Duplicates_To_Delete: {cleanup_root}")
    print(f"Файлов просканировано: {len(files)}")
    print(f"Групп дубликатов найдено: {len(duplicate_groups)}")
    print(f"Лишних копий найдено: {extra_copies}")
    print(f"Можно освободить: {format_bytes(moved_bytes)}")
    print("Минимум одна копия каждого файла останется на месте.")
    print("Ничего не будет удалено навсегда.")

    try:
        write_move_duplicates_reports(output_dir, planned_rows, moved_rows, summary_row)
    except OSError as exc:
        print(f"Ошибка записи отчётов: {exc}", file=sys.stderr)
        return 1

    if dry_run:
        print("DRY-RUN: ничего не перемещалось.")
    elif errors:
        for error in errors:
            print(f"ОШИБКА: {error}", file=sys.stderr)
    if dry_run and errors:
        for error in errors:
            print(f"ОШИБКА: {error}", file=sys.stderr)

    return 1 if errors else 0


def run_undo_move_duplicates_mode(
    input_dir: Path, output_dir: Path, dry_run: bool,
) -> int:
    """
    Put files moved by --move-duplicates back where they came from.

    Reads reports/moved_duplicates.csv in OUTPUT, which already records
    original_path, moved_to and file_size for every moved copy.  Each row is
    restored only when all three safety checks pass: the moved file is still
    there, its size still matches what was recorded, and the original path is
    free.  An occupied original path is ALWAYS a skip, never an overwrite —
    something else lives there now and it is not ours to replace.
    """
    input_dir, output_dir = validate_paths(input_dir, output_dir)
    report_path = output_dir / "reports" / "moved_duplicates.csv"
    if not report_path.is_file():
        print(
            f"Ошибка: отчёт не найден: {report_path}. "
            "Откат возможен только после запуска с --move-duplicates.",
            file=sys.stderr,
        )
        return 2

    try:
        with report_path.open(newline="", encoding="utf-8-sig") as handle:
            rows = list(csv.DictReader(handle))
    except OSError as exc:
        print(f"Ошибка чтения отчёта: {exc}", file=sys.stderr)
        return 2

    print("Режим: откат перемещения дубликатов")
    print(f"Input:  {input_dir}")
    print(f"Output: {output_dir}")
    print(f"Отчёт:  {report_path}")
    print(f"Записей в отчёте: {len(rows)}")
    if dry_run:
        print("DRY-RUN: ничего не перемещается, только план.")

    restored = 0
    restored_bytes = 0
    skipped: list[str] = []
    errors: list[str] = []

    for number, row in enumerate(rows, start=1):
        original_raw = (row.get("original_path") or "").strip()
        moved_raw = (row.get("moved_to") or "").strip()
        if not original_raw or not moved_raw:
            skipped.append(f"строка {number}: пустые пути в отчёте")
            continue
        original_path = Path(original_raw)
        moved_path = Path(moved_raw)
        try:
            expected_size = int(str(row.get("file_size") or "").strip())
        except ValueError:
            expected_size = -1

        if not moved_path.is_file():
            skipped.append(f"{moved_path}: файла нет на месте перемещения")
            continue
        try:
            actual_size = moved_path.stat().st_size
        except OSError as exc:
            skipped.append(f"{moved_path}: не удалось прочитать размер ({exc})")
            continue
        if expected_size < 0 or actual_size != expected_size:
            skipped.append(
                f"{moved_path}: размер изменился ({actual_size} вместо {expected_size}), "
                "файл трогать небезопасно"
            )
            continue
        if original_path.exists() or original_path.is_symlink():
            skipped.append(
                f"{original_path}: место занято другим файлом, ничего не перезаписываю"
            )
            continue

        if dry_run:
            print(f"[вернул бы] {moved_path} -> {original_path}")
            restored += 1
            restored_bytes += actual_size
            continue
        try:
            original_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(moved_path), str(original_path))
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{moved_path}: {exc}")
            continue
        print(f"[возвращён] {moved_path} -> {original_path}")
        restored += 1
        restored_bytes += actual_size

    print("\nИтог отката:")
    verb = "Было бы возвращено" if dry_run else "Возвращено файлов"
    print(f"  {verb}: {restored} ({format_bytes(restored_bytes)})")
    print(f"  Пропущено: {len(skipped)}")
    for reason in skipped:
        print(f"    - {reason}")
    print(f"  Ошибок: {len(errors)}")
    for error in errors:
        print(f"ОШИБКА: {error}", file=sys.stderr)
    if not dry_run and restored:
        print("  Отчёт moved_duplicates.csv оставлен без изменений для истории.")
    return 1 if errors else 0


def run_wizard_quick_file(args: argparse.Namespace) -> argparse.Namespace | None:
    while True:
        file_value = input("\nВставьте или перетащите видеофайл в Terminal: ")
        if not file_value.strip():
            print("Укажите путь к видеофайлу.")
            continue
        source = clean_terminal_path(file_value).resolve()
        if not (source.is_file() and not source.is_symlink()):
            print(f"Файл не найден: {source}")
            continue
        if source.suffix.lower() not in VIDEO_EXTENSIONS:
            # Don't reject outright — ffmpeg reads plenty of containers this
            # list doesn't name. Just don't let the user find out after
            # answering every question.
            print(
                f"Это не похоже на видеофайл (расширение «{source.suffix or 'без расширения'}»)."
            )
            if not prompt_yes_no("Всё равно попробовать? yes/no [no]: ", "no"):
                continue
        break

    # --- codec selection ---
    print("\nКодек видео:")
    print("  1. Автоматически: аппаратный кодек, если доступен, иначе libx265 (CPU) [по умолчанию]")
    print("  2. hevc_videotoolbox (аппаратный HEVC, только macOS, использует QP)")
    print("  3. libx265 (только процессор, медленно, точный CRF)")
    codec_choice = prompt_menu("Выберите 1-3 [Enter = 1]: ", {"1", "2", "3"}, "1")
    video_codec = "hevc_videotoolbox" if codec_choice == "2" else "x265"
    # Option 3 is exactly --disable-hw-video: same auto path, HW forbidden.
    disable_hw = codec_choice == "3"

    if video_codec == "hevc_videotoolbox":
        # Validate immediately so user knows before proceeding.
        if not is_macos():
            print("Ошибка: hevc_videotoolbox доступен только на macOS. Выберите x265.",
                  file=sys.stderr)
            return None
        if not ffmpeg_supports_encoder("hevc_videotoolbox"):
            print("Ошибка: ваш ffmpeg не поддерживает hevc_videotoolbox. "
                  "Установите: brew install ffmpeg", file=sys.stderr)
            return None

        print("\nКачество (QP для hevc_videotoolbox):")
        print("  1. Осторожно (лучше качество): QP 50")
        print("  2. Нормально (баланс): QP 60")
        print("  3. Сильно (меньше файл): QP 70")
        print("  4. Ввести вручную")
        qp_choice = prompt_menu("Выберите 1-4 [Enter = 2]: ", {"1", "2", "3", "4"}, "2")
        qp_presets = {"1": 50, "2": 60, "3": 70}
        if qp_choice == "4":
            video_qp = prompt_number("QP (20-90): ", 20, 90)
        else:
            video_qp = qp_presets[qp_choice]

        # Preset is irrelevant for videotoolbox but keep it at default.
        video_crf = args.video_crf
        video_preset = args.video_preset
        encoder = "hevc_videotoolbox"
        explicit_qp: int | None = video_qp
    else:
        # x265 path: CRF + preset as before
        video_qp = 0
        explicit_qp = None
        print("\nКачество:")
        print("  1. Осторожно: CRF 28")
        print("  2. Нормально: CRF 30")
        print("  3. Сильно: CRF 31")
        print("  4. Очень сильно: CRF 32")
        print("  5. Ввести вручную")
        quality_choice = prompt_menu("Выберите 1-5 [Enter = 3]: ", set("12345"), "3")
        crf_presets = {"1": 28, "2": 30, "3": 31, "4": 32}
        if quality_choice == "5":
            video_crf = prompt_number("Video CRF (18-35): ", 18, 35)
        else:
            video_crf = crf_presets[quality_choice]

        print("\nPreset видео:")
        print("  1. fast (по умолчанию)")
        print("  2. faster")
        print("  3. veryfast")
        print("  4. medium")
        print("  5. slow")
        preset_choice = prompt_menu("Выберите 1-5 [Enter = 1]: ", set("12345"), "1")
        video_preset = {"1": "fast", "2": "faster", "3": "veryfast", "4": "medium", "5": "slow"}[preset_choice]

        # Encoder must be selected here so wizard summary is honest.
        encoder = select_video_encoder(
            disable_hw=disable_hw or getattr(args, "disable_hw_video", False)
        )

    output_path = quick_file_output_path(source, video_crf, video_preset)
    print("\nИтоговые настройки:")
    print(f"  Исходный файл: {source}")
    print(f"  Будущий выходной файл: {output_path}")
    for line in quality_mode_display(encoder, video_crf, video_preset, explicit_qp=explicit_qp):
        print(line)
    print("  Оригинал не будет изменён.")
    confirmation = input("\nНапишите YES, чтобы начать: ").strip()
    if confirmation != "YES":
        print("Запуск отменён. Ничего не изменено.")
        return None

    args.quick_file = source
    args.video_crf = video_crf
    args.video_preset = video_preset
    args.video_codec = video_codec
    args.video_qp = video_qp
    if disable_hw:
        args.disable_hw_video = True
    return args


def run_wizard_move_duplicates(args: argparse.Namespace) -> argparse.Namespace | None:
    while True:
        input_value = input("\nВставьте или перетащите input-папку в Terminal: ")
        if not input_value.strip():
            print("Укажите путь к input-папке.")
            continue
        input_dir = clean_terminal_path(input_value).resolve()
        if input_dir.is_dir():
            break
        print(f"Папка не найдена: {input_dir}")

    suggested_output = input_dir.parent / f"{input_dir.name} duplicate cleanup"
    while True:
        output_value = input(f"Папка для отчёта и перемещения [Enter = {suggested_output}]: ")
        output_dir = (
            suggested_output if not output_value.strip()
            else clean_terminal_path(output_value)
        ).resolve()
        try:
            validate_paths(input_dir, output_dir)
        except ValueError as exc:
            print(f"Нельзя использовать этот output: {exc}")
            continue
        break

    dry_run = prompt_yes_no("Сначала сделать dry-run? yes/no [no]: ", "no")
    files = collect_files(input_dir)
    duplicate_rows_flat, extra_copies, duplicate_errors = find_duplicates(files, input_dir)
    duplicate_groups = duplicate_groups_from_rows(duplicate_rows_flat, input_dir)
    planned_moves = 0
    for group in duplicate_groups:
        planned_moves += max(0, len(group["paths"]) - 1)
    planned_bytes = sum(
        group["size_bytes"] * max(0, len(group["paths"]) - 1) for group in duplicate_groups
    )

    print("\nИтоговые настройки:")
    print(f"  Input: {input_dir}")
    print(f"  Output: {output_dir}")
    print(f"  Duplicates_To_Delete: {output_dir / 'Duplicates_To_Delete'}")
    print("  Режим: безопасно убрать лишние дубликаты")
    print(f"  Dry-run: {'да' if dry_run else 'нет'}")
    print("  Удаление файлов: выключено")
    print(f"  Файлов просканировано: {len(files)}")
    print(f"  Групп дубликатов найдено: {len(duplicate_groups)}")
    print(f"  Лишних копий найдено: {extra_copies}")
    print(f"  Можно освободить: {format_bytes(planned_bytes)}")
    print("  Минимум одна копия каждого файла останется на месте.")
    print("  Ничего не будет удалено навсегда.")

    confirmation = input("\nНапишите YES MOVE DUPLICATES, чтобы начать: ").strip()
    if confirmation != "YES MOVE DUPLICATES":
        print("Запуск отменён. Ничего не изменено.")
        return None

    args.input = input_dir
    args.output = output_dir
    args.dry_run = dry_run
    args.move_duplicates = True
    args._wizard_move_duplicates = True
    return args


def prompt_input_directory() -> Path:
    while True:
        value = input("\nВставьте или перетащите input-папку в Terminal: ")
        if not value.strip():
            print("Укажите путь к input-папке.")
            continue
        input_dir = clean_terminal_path(value).resolve()
        if input_dir.is_dir():
            return input_dir
        print(f"Папка не найдена: {input_dir}")


def prompt_output_directory(input_dir: Path, suffix: str) -> Path:
    suggested_output = input_dir.parent / f"{input_dir.name} {suffix}"
    while True:
        value = input(f"Output-папка [Enter = {suggested_output}]: ")
        output_dir = (
            suggested_output if not value.strip() else clean_terminal_path(value)
        ).resolve()
        try:
            validate_paths(input_dir, output_dir)
            return output_dir
        except ValueError as exc:
            print(f"Нельзя использовать этот output: {exc}")


def run_wizard_review_duplicates(args: argparse.Namespace) -> argparse.Namespace | None:
    input_dir = prompt_input_directory()
    output_dir = prompt_output_directory(input_dir, "duplicate review")
    print("\nИтоговые настройки:")
    print(f"  Input: {input_dir}")
    print(f"  Output: {output_dir}")
    print("  Режим: удобный просмотр точных дубликатов")
    print("  Файлы не будут перемещены или удалены.")
    if input("\nНапишите YES, чтобы создать отчёт: ").strip() != "YES":
        print("Запуск отменён. Ничего не изменено.")
        return None
    args.input = input_dir
    args.output = output_dir
    args.review_duplicates = True
    return args


def run_wizard_similar_videos(args: argparse.Namespace) -> argparse.Namespace | None:
    """Wizard branch: find visually similar videos."""
    if shutil.which("ffmpeg") is None:
        print(
            "Ошибка: ffmpeg не найден. Установите: brew install ffmpeg",
            file=sys.stderr,
        )
        return None
    input_dir = prompt_input_directory()
    output_dir = prompt_output_directory(input_dir, "similar videos review")

    # Preset selection — controlling threshold, n_samples, duration tolerance
    print("\nЧувствительность сравнения:")
    print("  1. Строгая проверка:  threshold=5, 12 кадров, доп. разница длительн. 10%")
    print("  2. Нормальная проверка: threshold=8, 8 кадров,  доп. разница длительн. 15%")
    print("  3. Дотошная проверка: threshold=12, 16 кадров, доп. разница длительн. 20%")
    print("  4. Ввести вручную")
    choice = prompt_menu("Выберите 1-4 [Enter = 2]: ", set("1234"), "2")
    if choice == "1":
        threshold, n_samples, dur_diff = 5, 12, 0.10
    elif choice == "2":
        threshold, n_samples, dur_diff = 8, 8, 0.15
    elif choice == "3":
        threshold, n_samples, dur_diff = 12, 16, 0.20
    else:
        threshold = prompt_number("Порог hash-distance (0-64, меньше = строже): ", 0, 64)
        n_samples = prompt_number("Количество sample-кадров (2-24): ", 2, 24)
        dur_diff = 0.15

    print("\nИтоговые настройки:")
    print(f"  Input: {input_dir}")
    print(f"  Output: {output_dir}")
    print("Режим: поиск визуально похожих видео")
    print(f"  hash-threshold: {threshold}, sample-кадров: {n_samples}")
    print("  Только отчёт и ручная проверка. Файлы не будут изменены.")
    print("Предупреждение: анализ видео занимает заметно больше времени, чем анализ фото.")
    if input("\nНапишите YES, чтобы создать отчёт: ").strip() != "YES":
        print("Запуск отменён. Ничего не изменено.")
        return None

    args.input = input_dir
    args.output = output_dir
    args.find_similar_videos = True
    args.similar_video_threshold = threshold
    args.similar_video_samples = n_samples
    args._similar_video_dur_diff = dur_diff  # private, passed via main dispatch
    return args


def run_wizard_trash_similar_videos(args: argparse.Namespace) -> argparse.Namespace | None:
    """Wizard branch: trash review_duplicate videos from report."""
    input_dir = prompt_input_directory()
    suggested_output = input_dir.parent / f"{input_dir.name} similar videos review"
    while True:
        value = input(
            f"Папка с отчётом похожих видео [Enter = {suggested_output}]: "
        )
        output_dir = (
            suggested_output if not value.strip() else clean_terminal_path(value)
        ).resolve()
        report_path = output_dir / "reports" / "similar_videos_report.csv"
        try:
            validate_paths(input_dir, output_dir)
        except ValueError as exc:
            print(f"Нельзя использовать этот output: {exc}")
            continue
        if not report_path.is_file():
            print(f"Отчёт не найден: {report_path}")
            continue
        break

    dry_run = prompt_yes_no("Сначала сделать dry-run? yes/no [yes]: ", "yes")
    print("\nИтоговые настройки:")
    print(f"  Input: {input_dir}")
    print(f"  Output с отчётом: {output_dir}")
    print(f"  Dry-run: {'да' if dry_run else 'нет'}")
    print("  Будут выбраны только строки review_duplicate.")
    print("  keep_candidate останутся на месте.")
    print("  Файлы будут перемещаться только в корзину macOS.")

    args.input = input_dir
    args.output = output_dir
    args.dry_run = dry_run
    args.trash_similar_videos_from_report = True
    return args


def run_wizard_similar_photos(args: argparse.Namespace) -> argparse.Namespace | None:
    input_dir = prompt_input_directory()
    output_dir = prompt_output_directory(input_dir, "similar photos review")
    print("\nЧувствительность:")
    print("  1. Строгая проверка: threshold 3")
    print("  2. Обычная проверка: threshold 5")
    print("  3. Мягкая проверка: threshold 8")
    print("  4. Ввести вручную")
    choice = prompt_menu("Выберите 1-4 [Enter = 2]: ", set("1234"), "2")
    if choice == "4":
        threshold = prompt_number("Threshold (0-64): ", 0, 64)
    else:
        threshold = {"1": 3, "2": 5, "3": 8}[choice]

    print("\nИтоговые настройки:")
    print(f"  Input: {input_dir}")
    print(f"  Output: {output_dir}")
    print("  Режим: поиск визуально похожих фото")
    print(f"  Threshold: {threshold}")
    print("  Только отчёт и ручная проверка. Файлы не будут изменены.")
    if input("\nНапишите YES, чтобы создать отчёт: ").strip() != "YES":
        print("Запуск отменён. Ничего не изменено.")
        return None
    args.input = input_dir
    args.output = output_dir
    args.find_similar_photos = True
    args.similar_threshold = threshold
    return args


def run_wizard_trash_similar(args: argparse.Namespace) -> argparse.Namespace | None:
    input_dir = prompt_input_directory()
    suggested_output = input_dir.parent / f"{input_dir.name} similar photos review"
    while True:
        value = input(
            f"Папка с отчётом похожих фото [Enter = {suggested_output}]: "
        )
        output_dir = (
            suggested_output if not value.strip() else clean_terminal_path(value)
        ).resolve()
        report_path = output_dir / "reports" / "similar_photos_report.csv"
        try:
            validate_paths(input_dir, output_dir)
        except ValueError as exc:
            print(f"Нельзя использовать этот output: {exc}")
            continue
        if not report_path.is_file():
            print(f"Отчёт не найден: {report_path}")
            continue
        break

    dry_run = prompt_yes_no("Сначала сделать dry-run? yes/no [yes]: ", "yes")
    print("\nИтоговые настройки:")
    print(f"  Input: {input_dir}")
    print(f"  Output с отчётом: {output_dir}")
    print(f"  Dry-run: {'да' if dry_run else 'нет'}")
    print("  Будут выбраны только строки review_duplicate.")
    print("  keep_candidate останутся на месте.")
    print("  Файлы будут перемещаться только в корзину macOS.")

    args.input = input_dir
    args.output = output_dir
    args.dry_run = dry_run
    args.trash_similar_from_report = True
    return args


def resolve_report_file_path(raw_path: str, input_dir: Path) -> Path:
    path = Path(raw_path).expanduser()
    if not path.is_absolute():
        path = input_dir / path
    return path.resolve()


def load_similar_trash_candidates(
    input_dir: Path, report_path: Path,
) -> tuple[list[dict[str, object]], list[str]]:
    required_fields = {
        "similar_group_id", "file_path", "file_size", "perceptual_hash",
        "distance_from_first", "suggested_action",
    }
    with report_path.open("r", newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        fields = set(reader.fieldnames or [])
        missing = sorted(required_fields - fields)
        if missing:
            raise ValueError(
                "В similar_photos_report.csv отсутствуют поля: " + ", ".join(missing)
            )
        rows = list(reader)

    grouped: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        group_id = (row.get("similar_group_id") or "").strip()
        if group_id:
            grouped[group_id].append(row)

    candidates: list[dict[str, object]] = []
    warnings: list[str] = []
    seen_paths: set[Path] = set()
    for group_id, group_rows in grouped.items():
        keep_paths: set[Path] = set()
        for row in group_rows:
            if (row.get("suggested_action") or "").strip() != "keep_candidate":
                continue
            try:
                keep_path = resolve_report_file_path(row.get("file_path") or "", input_dir)
            except (OSError, RuntimeError, ValueError) as exc:
                warnings.append(f"Группа {group_id}: неверный keep_candidate: {exc}")
                continue
            if is_relative_to(keep_path, input_dir) and keep_path.is_file() and not keep_path.is_symlink():
                keep_paths.add(keep_path)

        if not keep_paths:
            warnings.append(
                f"Группа {group_id} пропущена: существующий keep_candidate не найден"
            )
            continue

        for row in group_rows:
            if (row.get("suggested_action") or "").strip() != "review_duplicate":
                continue
            try:
                source = resolve_report_file_path(row.get("file_path") or "", input_dir)
                reported_size = int(row.get("file_size") or "")
            except (OSError, RuntimeError, ValueError) as exc:
                warnings.append(f"Группа {group_id}: строка пропущена: {exc}")
                continue
            if not is_relative_to(source, input_dir):
                warnings.append(f"Пропущен путь вне INPUT: {source}")
                continue
            if source in keep_paths:
                warnings.append(f"Защищён keep_candidate: {source}")
                continue
            if source in seen_paths:
                warnings.append(f"Повторная строка пропущена: {source}")
                continue
            if source.is_symlink() or not source.is_file():
                warnings.append(f"Файл отсутствует или является ссылкой: {source}")
                continue
            actual_size = source.stat().st_size
            if actual_size != reported_size:
                warnings.append(
                    f"Размер изменился, файл пропущен: {source} "
                    f"(в отчёте {reported_size}, сейчас {actual_size})"
                )
                continue
            seen_paths.add(source)
            candidates.append({
                "similar_group_id": group_id,
                "original_path": source,
                "file_size": actual_size,
                "perceptual_hash": row.get("perceptual_hash") or "",
                "distance_from_first": row.get("distance_from_first") or "",
            })
    return candidates, warnings


def move_file_to_macos_trash(source: Path) -> None:
    if sys.platform != "darwin" or shutil.which("osascript") is None:
        raise RuntimeError("Перемещение в Trash поддерживается только на macOS с osascript")
    command = [
        "osascript",
        "-e", "on run argv",
        "-e", "set targetFile to POSIX file (item 1 of argv)",
        "-e", 'tell application "Finder" to delete targetFile',
        "-e", "end run",
        str(source),
    ]
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode != 0:
        message = result.stderr.strip() or result.stdout.strip()
        if "Finder" in message or "-1743" in message:
            message += (
                " Разрешите Terminal управлять Finder в System Settings > "
                "Privacy & Security > Automation и повторите запуск с новым отчётом результата."
            )
        raise RuntimeError(message or f"osascript завершился с кодом {result.returncode}")


def run_trash_similar_from_report_mode(
    input_dir: Path, output_dir: Path, dry_run: bool,
) -> int:
    input_dir, output_dir = validate_paths(input_dir, output_dir)
    report_path = output_dir / "reports" / "similar_photos_report.csv"
    trashed_report_path = output_dir / "reports" / "trashed_similar_photos.csv"
    if not report_path.is_file():
        print(f"Ошибка: отчёт не найден: {report_path}", file=sys.stderr)
        return 2
    if not dry_run and (trashed_report_path.exists() or trashed_report_path.is_symlink()):
        print(
            f"Ошибка: отчёт уже существует и не будет перезаписан: {trashed_report_path}",
            file=sys.stderr,
        )
        return 2
    if not dry_run and (sys.platform != "darwin" or shutil.which("osascript") is None):
        print(
            "Ошибка: реальное перемещение в Trash доступно только на macOS с osascript.",
            file=sys.stderr,
        )
        return 2

    try:
        candidates, warnings = load_similar_trash_candidates(input_dir, report_path)
    except (OSError, ValueError) as exc:
        print(f"Ошибка чтения отчёта: {exc}", file=sys.stderr)
        return 2

    total_bytes = sum(int(item["file_size"]) for item in candidates)
    affected_groups = {str(item["similar_group_id"]) for item in candidates}
    print("Режим: перемещение похожих фото из отчёта в Trash")
    print(f"Отчёт: {report_path}")
    print(f"Файлов будет отправлено в Trash: {len(candidates)}")
    print(f"Общий размер: {format_bytes(total_bytes)}")
    print(f"Групп похожих фото затронуто: {len(affected_groups)}")
    print("keep_candidate останутся на месте.")
    print("Файлы будут перемещены в корзину macOS, а не удалены безвозвратно.")
    if warnings:
        print(f"Предупреждений и пропущенных строк: {len(warnings)}")
        for warning in warnings:
            print(f"[пропуск] {warning}")
    print("\nФайлы из review_duplicate:")
    for item in candidates:
        print(f"  [would-trash] {item['original_path']}")

    if dry_run:
        print("\nDRY-RUN: файлы не перемещались, отчёт trash не создавался.")
        return 0
    if not candidates:
        print("Подходящих файлов для перемещения нет.")
        return 0

    print("\nПредварительный dry-run завершён. До подтверждения ничего не перемещено.")
    confirmation = input("\nНапишите YES TRASH SIMILAR, чтобы продолжить: ").strip()
    if confirmation != "YES TRASH SIMILAR":
        print("Запуск отменён. Файлы не перемещались.")
        return 0

    trashed_report_path.parent.mkdir(parents=True, exist_ok=True)
    errors = 0
    trashed_count = 0
    trashed_bytes = 0
    try:
        with trashed_report_path.open("x", newline="", encoding="utf-8-sig") as handle:
            writer = csv.DictWriter(handle, fieldnames=[
                "similar_group_id", "original_path", "file_size",
                "perceptual_hash", "distance_from_first", "action",
            ])
            writer.writeheader()
            for item in candidates:
                source = item["original_path"]
                action = "trashed_to_macos_trash"
                try:
                    move_file_to_macos_trash(source)
                    trashed_count += 1
                    trashed_bytes += int(item["file_size"])
                    print(f"[в Trash] {source}")
                except Exception as exc:
                    errors += 1
                    error_text = " ".join(str(exc).splitlines())
                    action = f"trash_failed: {error_text}"
                    print(f"ОШИБКА: {source}: {exc}", file=sys.stderr)
                writer.writerow({
                    "similar_group_id": item["similar_group_id"],
                    "original_path": str(source),
                    "file_size": item["file_size"],
                    "perceptual_hash": item["perceptual_hash"],
                    "distance_from_first": item["distance_from_first"],
                    "action": action,
                })
                handle.flush()
    except OSError as exc:
        print(f"Ошибка записи отчёта: {exc}", file=sys.stderr)
        return 1

    print("\nИтог:")
    print(f"  Перемещено в Trash: {trashed_count}")
    print(f"  Перемещённый размер: {format_bytes(trashed_bytes)}")
    print(f"  Ошибок: {errors}")
    print(f"  Отчёт: {trashed_report_path}")
    return 1 if errors else 0


def ensure_output_artifacts_absent(paths: list[Path]) -> None:
    existing = [path for path in paths if path.exists() or path.is_symlink()]
    if existing:
        raise FileExistsError(
            "Результаты уже существуют и не будут перезаписаны: "
            + ", ".join(str(path) for path in existing)
            + ". Выберите новую output-папку."
        )


def create_unique_symlink(source: Path, directory: Path, link_name: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    link_path = unique_path_for_existing_target(directory / link_name)
    link_path.symlink_to(source)
    return link_path


def make_thumbnail(
    source: Path, destination: Path, max_size: tuple[int, int] = (360, 260),
) -> bool:
    """
    Render a JPEG preview of an image or video frame.

    max_size keeps the historical (360, 260) box by default; the visual review
    of similar photos passes a larger box because tiny previews are useless
    when the whole point is to eyeball two nearly identical shots.
    """
    destination.parent.mkdir(parents=True, exist_ok=True)
    extension = source.suffix.lower()
    if extension in SIMILAR_IMAGE_EXTENSIONS or extension == ".heif":
        try:
            from PIL import Image, ImageOps

            with Image.open(source) as image:
                image = ImageOps.exif_transpose(image)
                image.thumbnail(max_size)
                if image.mode not in {"RGB", "L"}:
                    background = Image.new("RGB", image.size, "white")
                    if "A" in image.getbands():
                        background.paste(image, mask=image.getchannel("A"))
                    else:
                        background.paste(image.convert("RGB"))
                    image = background
                image.convert("RGB").save(destination, "JPEG", quality=82)
            return True
        except Exception:
            destination.unlink(missing_ok=True)
            return False

    if extension in VIDEO_EXTENSIONS and shutil.which("ffmpeg"):
        command = [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
            "-ss", "00:00:01", "-i", str(source), "-frames:v", "1",
            "-vf", f"scale='min({int(max_size[0])},iw)':-2", str(destination),
        ]
        result = subprocess.run(command, capture_output=True, text=True)
        if result.returncode == 0 and destination.is_file():
            return True
        destination.unlink(missing_ok=True)
    return False


def html_document(title: str, body: str) -> str:
    return f"""<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>
<style>
body {{ font-family: -apple-system, BlinkMacSystemFont, sans-serif; margin: 28px; color: #202124; }}
h1 {{ margin-bottom: 8px; }}
.notice {{ padding: 12px 16px; background: #fff7d6; border-radius: 10px; }}
.group {{ margin: 24px 0; padding: 18px; border: 1px solid #d8dce3; border-radius: 12px; }}
.items {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(260px, 1fr)); gap: 14px; }}
.item {{ padding: 12px; background: #f6f7f9; border-radius: 10px; overflow-wrap: anywhere; }}
.keep {{ border-left: 5px solid #248a3d; }}
.review {{ border-left: 5px solid #d67a00; }}
img {{ display: block; max-width: 100%; max-height: 230px; margin: 8px auto; border-radius: 6px; }}
code {{ font-size: 0.88em; }}
a {{ color: #175cd3; }}
</style>
</head>
<body>
<h1>{html.escape(title)}</h1>
<p class="notice">Это только отчёт для ручной проверки. Исходные файлы не удалялись и не перемещались.</p>
{body}
</body>
</html>
"""


def thumbnail_html(thumbnail: Path | None, output_dir: Path, alt: str) -> str:
    if thumbnail is None:
        return ""
    relative_url = quote(thumbnail.relative_to(output_dir).as_posix())
    return f'<img src="{relative_url}" alt="{html.escape(alt)}">'


def open_output_folder(output_dir: Path) -> None:
    if sys.platform == "darwin":
        subprocess.run(["open", str(output_dir)], capture_output=True, check=False)


def run_review_duplicates_mode(input_dir: Path, output_dir: Path) -> int:
    input_dir, output_dir = validate_paths(input_dir, output_dir)
    reports_dir = output_dir / "reports"
    review_dir = output_dir / "Duplicate_Review"
    thumbnails_dir = output_dir / "thumbnails"
    html_path = output_dir / "duplicate_review.html"
    artifacts = [
        reports_dir / "duplicates_report.csv",
        reports_dir / "review_summary.csv",
        review_dir,
        thumbnails_dir,
        html_path,
    ]
    try:
        ensure_output_artifacts_absent(artifacts)
    except FileExistsError as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 2

    files = collect_files(input_dir)
    raw_rows, extra_copies, errors = find_duplicates(files, input_dir)
    groups = duplicate_groups_from_rows(raw_rows, input_dir)
    potential_bytes = sum(
        int(group["size_bytes"]) * (len(group["paths"]) - 1) for group in groups
    )
    report_rows: list[dict[str, object]] = []
    html_groups: list[str] = []

    reports_dir.mkdir(parents=True, exist_ok=True)
    review_dir.mkdir(parents=True, exist_ok=True)
    thumbnails_dir.mkdir(parents=True, exist_ok=True)

    for group in groups:
        group_id = int(group["group_id"])
        group_name = f"group_{group_id:03d}"
        group_dir = review_dir / group_name
        paths = list(group["paths"])
        keeper = paths[0]
        item_html: list[str] = []
        for index, source in enumerate(paths, start=1):
            is_keeper = source == keeper
            action = "keep_candidate" if is_keeper else "duplicate"
            prefix = "KEEP_candidate" if is_keeper else "DUPLICATE"
            try:
                create_unique_symlink(source, group_dir, f"{prefix}__{source.name}")
            except OSError as exc:
                errors.append(f"Не удалось создать ссылку для {source}: {exc}")

            thumbnail_path = thumbnails_dir / f"{group_name}_{index:03d}.jpg"
            thumbnail = thumbnail_path if make_thumbnail(source, thumbnail_path) else None
            report_rows.append({
                "duplicate_group_id": group_id,
                "file_path": str(source),
                "file_size": int(group["size_bytes"]),
                "sha256": group["sha256"],
                "is_first_copy": "yes" if is_keeper else "no",
                "suggested_action": action,
            })
            item_html.append(
                f'<div class="item {"keep" if is_keeper else "review"}">'
                f'<strong>{html.escape(action)}</strong>'
                f'{thumbnail_html(thumbnail, output_dir, source.name)}'
                f'<p><a href="{html.escape(source.as_uri())}">{html.escape(str(source))}</a></p>'
                f'</div>'
            )

        html_groups.append(
            f'<section class="group"><h2>Группа {group_id:03d}</h2>'
            f'<p>Размер каждого файла: {html.escape(format_bytes(int(group["size_bytes"])))}<br>'
            f'SHA256: <code>{html.escape(str(group["sha256"]))}</code><br>'
            f'Предлагается оставить: {html.escape(str(keeper))}</p>'
            f'<div class="items">{"".join(item_html)}</div></section>'
        )

    summary_row = {
        "total_files_scanned": len(files),
        "duplicate_groups_found": len(groups),
        "duplicate_extra_copies_found": extra_copies,
        "potential_bytes_to_move": potential_bytes,
        "human_readable_potential_savings": format_bytes(potential_bytes),
        "html_report_path": str(html_path),
        "duplicate_review_folder_path": str(review_dir),
    }
    try:
        with (reports_dir / "duplicates_report.csv").open("x", newline="", encoding="utf-8-sig") as handle:
            writer = csv.DictWriter(handle, fieldnames=[
                "duplicate_group_id", "file_path", "file_size", "sha256",
                "is_first_copy", "suggested_action",
            ])
            writer.writeheader()
            writer.writerows(report_rows)
        with (reports_dir / "review_summary.csv").open("x", newline="", encoding="utf-8-sig") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(summary_row))
            writer.writeheader()
            writer.writerow(summary_row)
        html_path.write_text(
            html_document("Точные дубликаты", "".join(html_groups) or "<p>Точные дубликаты не найдены.</p>"),
            encoding="utf-8",
        )
    except OSError as exc:
        print(f"Ошибка записи отчёта: {exc}", file=sys.stderr)
        return 1

    print("Режим: удобный просмотр точных дубликатов")
    print(f"Файлов просканировано: {len(files)}")
    print(f"Групп дубликатов найдено: {len(groups)}")
    print(f"Лишних копий найдено: {extra_copies}")
    print(f"Потенциальная экономия: {format_bytes(potential_bytes)}")
    print(f"Открой {html_path} для удобного просмотра.")
    if errors:
        print(f"Предупреждений: {len(errors)}")
    open_output_folder(output_dir)
    return 0


# ---------------------------------------------------------------------------
# Similar-video helpers
# ---------------------------------------------------------------------------

def _video_metadata(path: Path) -> dict[str, object] | None:
    """
    Return {duration, width, height, file_size} for a video via ffprobe.
    Returns None if ffprobe is unavailable or file is unreadable.
    """
    if shutil.which("ffprobe") is None and shutil.which("ffmpeg") is None:
        return None
    probe_tool = "ffprobe" if shutil.which("ffprobe") else "ffmpeg"
    cmd = [
        probe_tool, "-v", "quiet", "-print_format", "json",
        "-show_streams", "-show_format", str(path),
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            return None
        import json as _json
        data = _json.loads(result.stdout)
    except Exception:
        return None
    duration = 0.0
    width = 0
    height = 0
    for stream in data.get("streams", []):
        if stream.get("codec_type") == "video" and width == 0:
            try:
                width = int(stream.get("width", 0))
                height = int(stream.get("height", 0))
            except (TypeError, ValueError):
                pass
    try:
        duration = float(data.get("format", {}).get("duration") or 0)
    except (TypeError, ValueError):
        pass
    if duration == 0:
        # Fallback: try per-stream duration
        for stream in data.get("streams", []):
            try:
                d = float(stream.get("duration") or 0)
                if d > duration:
                    duration = d
            except (TypeError, ValueError):
                pass
    return {
        "duration": duration,
        "width": width,
        "height": height,
        "file_size": path.stat().st_size,
    }


def _sample_timestamps(duration: float, n_samples: int) -> list[float]:
    """
    Return n_samples timestamps spread evenly through the video,
    avoiding first/last 5% to reduce black-frame risk.
    Falls back gracefully for very short videos.
    """
    if duration <= 0:
        return []
    margin = duration * 0.05
    inner_start = margin
    inner_end = duration - margin
    if inner_end <= inner_start:
        # Very short video: take single middle frame
        return [duration / 2]
    if n_samples == 1:
        return [(inner_start + inner_end) / 2]
    step = (inner_end - inner_start) / (n_samples - 1)
    return [inner_start + i * step for i in range(n_samples)]


def _extract_frame_phash(
    path: Path, timestamp: float, tmp_dir: Path,
) -> object | None:
    """
    Extract a single frame at *timestamp* seconds via ffmpeg and return
    an imagehash.phash object.  Returns None on any failure.
    """
    if shutil.which("ffmpeg") is None:
        return None
    try:
        import imagehash
        from PIL import Image
    except ImportError:
        return None
    frame_path = tmp_dir / f"frame_{timestamp:.3f}.jpg"
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
        "-ss", f"{timestamp:.3f}", "-i", str(path),
        "-frames:v", "1", "-vf", "scale='min(320,iw)':-2",
        str(frame_path),
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=30)
        if r.returncode != 0 or not frame_path.is_file():
            return None
        with Image.open(frame_path) as img:
            ph = imagehash.phash(img.convert("RGB"))
        return ph
    except Exception:
        return None
    finally:
        frame_path.unlink(missing_ok=True)


def _video_signature(
    path: Path, n_samples: int, tmp_dir: Path,
) -> list[object] | None:
    """
    Compute a list of perceptual hashes (one per sample frame).
    Returns None if metadata cannot be read or no frames could be extracted.
    At least half the requested samples must succeed.
    """
    meta = _video_metadata(path)
    if meta is None:
        return None
    duration = float(meta["duration"])
    timestamps = _sample_timestamps(duration, n_samples)
    if not timestamps:
        return None
    hashes: list[object] = []
    for ts in timestamps:
        ph = _extract_frame_phash(path, ts, tmp_dir)
        if ph is not None:
            hashes.append(ph)
    if len(hashes) < max(1, len(timestamps) // 2):
        return None  # Too many frames failed
    return hashes


def _video_signature_distance(
    hashes_a: list[object],
    hashes_b: list[object],
) -> float:
    """
    Aggregate distance between two video signatures.
    Strategy: align by position, average pairwise distance.
    If lists have different lengths, use the shorter one.
    Returns float('inf') if either list is empty.
    """
    if not hashes_a or not hashes_b:
        return float("inf")
    pairs = min(len(hashes_a), len(hashes_b))
    total = sum(hashes_a[i] - hashes_b[i] for i in range(pairs))  # type: ignore[operator]
    return total / pairs


def _duration_compatible(
    dur_a: float, dur_b: float, max_ratio_diff: float = 0.15
) -> bool:
    """
    True if durations are within max_ratio_diff (default 15%) of each other.
    This catches same-content videos trimmed slightly at start/end.
    Returns True when either duration is 0 (unknown).
    """
    if dur_a <= 0 or dur_b <= 0:
        return True
    longer = max(dur_a, dur_b)
    shorter = min(dur_a, dur_b)
    return (longer - shorter) / longer <= max_ratio_diff


def group_similar_videos(
    videos: list[dict[str, object]],
    hash_threshold: float,
    max_duration_ratio_diff: float = 0.15,
) -> list[list[int]]:
    """
    Union-find grouping of videos whose aggregate phash distance is within
    hash_threshold AND whose durations are compatible.
    """
    n = len(videos)
    parents = list(range(n))

    def find(i: int) -> int:
        while parents[i] != i:
            parents[i] = parents[parents[i]]
            i = parents[i]
        return i

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parents[rb] = ra

    for i in range(n):
        for j in range(i + 1, n):
            dur_a = float(videos[i].get("duration") or 0)
            dur_b = float(videos[j].get("duration") or 0)
            if not _duration_compatible(dur_a, dur_b, max_duration_ratio_diff):
                continue
            sigs_a = videos[i].get("hashes")
            sigs_b = videos[j].get("hashes")
            if not sigs_a or not sigs_b:
                continue
            dist = _video_signature_distance(sigs_a, sigs_b)  # type: ignore[arg-type]
            if dist <= hash_threshold:
                union(i, j)

    grouped: dict[int, list[int]] = defaultdict(list)
    for idx in range(n):
        grouped[find(idx)].append(idx)
    return [indices for indices in grouped.values() if len(indices) > 1]


def _keep_candidate_video(members: list[dict[str, object]]) -> dict[str, object]:
    """
    Deterministically pick the best video in a group:
    1. Highest resolution (w*h).
    2. Largest file size.
    3. Longest duration.
    4. First in list (stable).
    """
    return max(
        members,
        key=lambda v: (
            int(v.get("width") or 0) * int(v.get("height") or 0),
            int(v.get("file_size") or 0),
            float(v.get("duration") or 0),
        ),
    )


def load_similar_video_trash_candidates(
    input_dir: Path, report_path: Path,
) -> tuple[list[dict[str, object]], list[str]]:
    """
    Read similar_videos_report.csv; return (candidates_to_trash, warnings).
    Mirrors load_similar_trash_candidates for photos but uses video fields.
    """
    required_fields = {
        "similar_video_group_id", "file_path", "file_size",
        "video_signature", "distance_from_keep", "suggested_action",
    }
    with report_path.open("r", newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        fields = set(reader.fieldnames or [])
        missing = sorted(required_fields - fields)
        if missing:
            raise ValueError(
                "В similar_videos_report.csv отсутствуют поля: " + ", ".join(missing)
            )
        rows = list(reader)

    grouped: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        gid = (row.get("similar_video_group_id") or "").strip()
        if gid:
            grouped[gid].append(row)

    candidates: list[dict[str, object]] = []
    warnings: list[str] = []
    seen_paths: set[Path] = set()

    for gid, group_rows in grouped.items():
        keep_paths: set[Path] = set()
        for row in group_rows:
            if (row.get("suggested_action") or "").strip() != "keep_candidate":
                continue
            try:
                kp = resolve_report_file_path(row.get("file_path") or "", input_dir)
            except (OSError, RuntimeError, ValueError) as exc:
                warnings.append(f"Группа {gid}: неверный keep_candidate: {exc}")
                continue
            if is_relative_to(kp, input_dir) and kp.is_file() and not kp.is_symlink():
                keep_paths.add(kp)

        if not keep_paths:
            warnings.append(f"Группа {gid} пропущена: существующий keep_candidate не найден")
            continue

        for row in group_rows:
            if (row.get("suggested_action") or "").strip() != "review_duplicate":
                continue
            try:
                source = resolve_report_file_path(row.get("file_path") or "", input_dir)
                reported_size = int(row.get("file_size") or "")
            except (OSError, RuntimeError, ValueError) as exc:
                warnings.append(f"Группа {gid}: строка пропущена: {exc}")
                continue
            if not is_relative_to(source, input_dir):
                warnings.append(f"Пропущен путь вне INPUT: {source}")
                continue
            if source in keep_paths:
                warnings.append(f"Защищён keep_candidate: {source}")
                continue
            if source in seen_paths:
                warnings.append(f"Повторная строка пропущена: {source}")
                continue
            if source.is_symlink() or not source.is_file():
                warnings.append(f"Файл отсутствует или является ссылкой: {source}")
                continue
            actual_size = source.stat().st_size
            if actual_size != reported_size:
                warnings.append(
                    f"Размер изменился, файл пропущен: {source} "
                    f"(в отчёте {reported_size}, сейчас {actual_size})"
                )
                continue
            seen_paths.add(source)
            candidates.append({
                "similar_video_group_id": gid,
                "original_path": source,
                "file_size": actual_size,
                "video_signature": row.get("video_signature") or "",
                "distance_from_keep": row.get("distance_from_keep") or "",
            })
    return candidates, warnings


def run_find_similar_videos_mode(
    input_dir: Path,
    output_dir: Path,
    threshold: int = 8,
    n_samples: int = 8,
    max_duration_ratio_diff: float = 0.15,
) -> int:
    """
    Find visually similar videos using multi-frame perceptual hashing.
    Produces CSV / HTML / symlink / thumbnail reports.
    Original files are NEVER modified.
    """
    try:
        import imagehash  # noqa: F401
        from PIL import Image  # noqa: F401
    except ImportError:
        print(
            "Ошибка: для поиска похожих видео установите зависимости:\n"
            "python3 -m pip install pillow imagehash",
            file=sys.stderr,
        )
        return 2

    if shutil.which("ffmpeg") is None:
        print(
            "Ошибка: ffmpeg не найден. Установите: brew install ffmpeg",
            file=sys.stderr,
        )
        return 2

    input_dir, output_dir = validate_paths(input_dir, output_dir)
    reports_dir = output_dir / "reports"
    review_dir = output_dir / "Similar_Videos_Review"
    thumbnails_dir = output_dir / "similar_video_thumbnails"
    html_path = output_dir / "similar_videos_review.html"
    artifacts = [
        reports_dir / "similar_videos_report.csv",
        reports_dir / "similar_videos_summary.csv",
        reports_dir / "similar_videos_skipped.csv",
        review_dir,
        thumbnails_dir,
        html_path,
    ]
    try:
        ensure_output_artifacts_absent(artifacts)
    except FileExistsError as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 2

    candidates = [
        path for path in collect_files(input_dir)
        if path.suffix.lower() in VIDEO_EXTENSIONS and not path.is_symlink()
    ]
    print("Режим: поиск визуально похожих видео")
    print(f"Видеофайлов для анализа: {len(candidates)}")
    print(f"Sample-кадров на файл: {n_samples}, hash-threshold: {threshold}")

    videos: list[dict[str, object]] = []
    skipped_rows: list[dict[str, str]] = []

    with tempfile.TemporaryDirectory(prefix="media_cleaner_vid_") as tmp_str:
        tmp_dir = Path(tmp_str)
        for idx, source in enumerate(candidates, start=1):
            print(f"  [{idx}/{len(candidates)}] {source.name} …", end=" ", flush=True)
            meta = _video_metadata(source)
            if meta is None:
                reason = "ffprobe/ffmpeg не смог прочитать метаданные"
                print("ПРОПУСК (метаданные)")
                skipped_rows.append({"file_path": str(source), "reason": reason})
                continue
            sigs = _video_signature(source, n_samples, tmp_dir)
            if sigs is None:
                reason = "не удалось извлечь достаточно кадров"
                print("ПРОПУСК (кадры)")
                skipped_rows.append({"file_path": str(source), "reason": reason})
                continue
            print(f"{len(sigs)} кадров")
            videos.append({
                "path": source,
                "file_size": meta["file_size"],
                "width": meta["width"],
                "height": meta["height"],
                "duration": meta["duration"],
                "hashes": sigs,
                # Compact text representation of signature for CSV
                "video_signature": ",".join(str(h) for h in sigs),
            })

    similar_groups = group_similar_videos(videos, hash_threshold=threshold,
                                          max_duration_ratio_diff=max_duration_ratio_diff)
    reports_dir.mkdir(parents=True, exist_ok=True)
    review_dir.mkdir(parents=True, exist_ok=True)
    thumbnails_dir.mkdir(parents=True, exist_ok=True)

    report_rows: list[dict[str, object]] = []
    html_groups: list[str] = []
    review_duplicates = 0
    errors_count = 0

    for group_id, indices in enumerate(similar_groups, start=1):
        members = [videos[i] for i in indices]
        keeper = _keep_candidate_video(members)
        ordered = [keeper] + [v for v in members if v is not keeper]
        keep_sigs = keeper["hashes"]
        review_duplicates += len(ordered) - 1
        group_name = f"group_{group_id:03d}"
        group_dir = review_dir / group_name
        item_html: list[str] = []

        for item_idx, item in enumerate(ordered, start=1):
            source = item["path"]
            is_keeper = item is keeper
            action = "keep_candidate" if is_keeper else "review_duplicate"
            prefix = "KEEP_candidate" if is_keeper else "REVIEW_duplicate"
            item_sigs = item["hashes"]
            dist_val = (
                0.0 if is_keeper
                else _video_signature_distance(keep_sigs, item_sigs)  # type: ignore[arg-type]
            )
            dist_str = f"{dist_val:.2f}" if not is_keeper else "0.00"

            try:
                create_unique_symlink(source, group_dir, f"{prefix}__{source.name}")
            except OSError as exc:
                errors_count += 1
                skipped_rows.append({
                    "file_path": str(source),
                    "reason": f"Не удалось создать symlink: {exc}",
                })

            thumb_path = thumbnails_dir / f"{group_name}_{item_idx:03d}.jpg"
            thumbnail = thumb_path if make_thumbnail(source, thumb_path) else None

            dur = float(item.get("duration") or 0)
            dur_str = f"{int(dur // 60)}:{int(dur % 60):02d}" if dur > 0 else "?"
            report_rows.append({
                "similar_video_group_id": group_id,
                "file_path": str(source),
                "file_size": item["file_size"],
                "video_width": item["width"],
                "video_height": item["height"],
                "duration_seconds": f"{float(item.get('duration') or 0):.2f}",
                "sample_count": len(item_sigs),
                "video_signature": item["video_signature"],
                "distance_from_keep": dist_str,
                "suggested_action": action,
            })

            item_html.append(
                f'<div class="item {"keep" if is_keeper else "review"}">' \
                f'<strong>{html.escape(action)}</strong>' \
                f'{thumbnail_html(thumbnail, output_dir, source.name)}' \
                f'<p>{item["width"]} x {item["height"]}' \
                f' &bull; {html.escape(dur_str)}' \
                f' &bull; {html.escape(format_bytes(int(item["file_size"])))}</p>' \
                f'<p>Дистанция: <code>{dist_str}</code> &bull; ' \
                f'Кадров: {len(item_sigs)}</p>' \
                f'<p><a href="{html.escape(source.as_uri())}">{html.escape(str(source))}</a></p>' \
                f'</div>'
            )

        html_groups.append(
            f'<section class="group"><h2>Группа {group_id:03d}</h2>'
            f'<p>Threshold: {threshold}. Эти видео только похожи и требуют ручной проверки.</p>'
            f'<div class="items">{"" .join(item_html)}</div></section>'
        )

    summary_row = {
        "total_videos_scanned": len(candidates),
        "similar_groups_found": len(similar_groups),
        "review_duplicates_found": review_duplicates,
        "hash_threshold": threshold,
        "n_samples": n_samples,
        "max_duration_ratio_diff": max_duration_ratio_diff,
        "skipped_count": len(skipped_rows),
        "errors_count": errors_count,
        "html_report_path": str(html_path),
        "similar_videos_review_folder_path": str(review_dir),
    }
    try:
        with (reports_dir / "similar_videos_report.csv").open(
            "x", newline="", encoding="utf-8-sig"
        ) as handle:
            writer = csv.DictWriter(handle, fieldnames=[
                "similar_video_group_id", "file_path", "file_size",
                "video_width", "video_height", "duration_seconds",
                "sample_count", "video_signature", "distance_from_keep",
                "suggested_action",
            ])
            writer.writeheader()
            writer.writerows(report_rows)
        with (reports_dir / "similar_videos_summary.csv").open(
            "x", newline="", encoding="utf-8-sig"
        ) as handle:
            writer = csv.DictWriter(handle, fieldnames=list(summary_row))
            writer.writeheader()
            writer.writerow(summary_row)
        with (reports_dir / "similar_videos_skipped.csv").open(
            "x", newline="", encoding="utf-8-sig"
        ) as handle:
            writer = csv.DictWriter(handle, fieldnames=["file_path", "reason"])
            writer.writeheader()
            writer.writerows(skipped_rows)
        html_path.write_text(
            html_document(
                "Визуально похожие видео",
                "".join(html_groups) or "<p>Группы похожих видео не найдены.</p>",
            ),
            encoding="utf-8",
        )
    except OSError as exc:
        print(f"Ошибка записи отчёта: {exc}", file=sys.stderr)
        return 1

    print(f"Видеофайлов просканировано: {len(candidates)}")
    print(f"Групп похожих видео найдено: {len(similar_groups)}")
    print(f"Видео для ручной проверки: {review_duplicates}")
    print(f"Пропущено / не поддерживается: {len(skipped_rows)}")
    print("Похожие видео не перемещались и не удалялись.")
    print(f"Открой {html_path} для ручной проверки.")
    open_output_folder(output_dir)
    return 0


def run_trash_similar_videos_from_report_mode(
    input_dir: Path, output_dir: Path, dry_run: bool,
) -> int:
    """
    Read similar_videos_report.csv; send review_duplicate entries to macOS Trash.
    Mirrors run_trash_similar_from_report_mode for photos.
    """
    input_dir, output_dir = validate_paths(input_dir, output_dir)
    report_path = output_dir / "reports" / "similar_videos_report.csv"
    trashed_report_path = output_dir / "reports" / "trashed_similar_videos.csv"

    if not report_path.is_file():
        print(f"Ошибка: отчёт не найден: {report_path}", file=sys.stderr)
        return 2
    if not dry_run and (trashed_report_path.exists() or trashed_report_path.is_symlink()):
        print(
            f"Ошибка: отчёт уже существует и не будет перезаписан: {trashed_report_path}",
            file=sys.stderr,
        )
        return 2
    if not dry_run and (sys.platform != "darwin" or shutil.which("osascript") is None):
        print(
            "Ошибка: реальное перемещение в Trash доступно только на macOS с osascript.",
            file=sys.stderr,
        )
        return 2

    try:
        candidates, warnings = load_similar_video_trash_candidates(input_dir, report_path)
    except (OSError, ValueError) as exc:
        print(f"Ошибка чтения отчёта: {exc}", file=sys.stderr)
        return 2

    total_bytes = sum(int(item["file_size"]) for item in candidates)
    affected_groups = {str(item["similar_video_group_id"]) for item in candidates}
    print("Режим: перемещение похожих видео из отчёта в Trash")
    print(f"Отчёт: {report_path}")
    print(f"Файлов будет отправлено в Trash: {len(candidates)}")
    print(f"Общий размер: {format_bytes(total_bytes)}")
    print(f"Групп похожих видео затронуто: {len(affected_groups)}")
    print("keep_candidate останутся на месте.")
    print("Файлы будут перемещены в корзину macOS, а не удалены безвозвратно.")
    if warnings:
        print(f"Предупреждений и пропущенных строк: {len(warnings)}")
        for w in warnings:
            print(f"[пропуск] {w}")
    print("\nФайлы из review_duplicate:")
    for item in candidates:
        print(f"  [would-trash] {item['original_path']}")

    if dry_run:
        print("\nDRY-RUN: файлы не перемещались, отчёт trash не создавался.")
        return 0
    if not candidates:
        print("Подходящих файлов для перемещения нет.")
        return 0

    print("\nПредварительный dry-run завершён. До подтверждения ничего не перемещено.")
    confirmation = input("\nНапишите YES TRASH SIMILAR VIDEOS, чтобы продолжить: ").strip()
    if confirmation != "YES TRASH SIMILAR VIDEOS":
        print("Запуск отменён. Файлы не перемещались.")
        return 0

    trashed_report_path.parent.mkdir(parents=True, exist_ok=True)
    errors = 0
    trashed_count = 0
    trashed_bytes = 0
    try:
        with trashed_report_path.open("x", newline="", encoding="utf-8-sig") as handle:
            writer = csv.DictWriter(handle, fieldnames=[
                "similar_video_group_id", "original_path", "file_size",
                "video_signature", "distance_from_keep", "action",
            ])
            writer.writeheader()
            for item in candidates:
                source = item["original_path"]
                action = "trashed_to_macos_trash"
                try:
                    move_file_to_macos_trash(source)
                    trashed_count += 1
                    trashed_bytes += int(item["file_size"])
                    print(f"[в Trash] {source}")
                except Exception as exc:
                    errors += 1
                    error_text = " ".join(str(exc).splitlines())
                    action = f"trash_failed: {error_text}"
                    print(f"ОШИБКА: {source}: {exc}", file=sys.stderr)
                writer.writerow({
                    "similar_video_group_id": item["similar_video_group_id"],
                    "original_path": str(source),
                    "file_size": item["file_size"],
                    "video_signature": item["video_signature"],
                    "distance_from_keep": item["distance_from_keep"],
                    "action": action,
                })
                handle.flush()
    except OSError as exc:
        print(f"Ошибка записи отчёта: {exc}", file=sys.stderr)
        return 1

    print("\nИтог:")
    print(f"  Перемещено в Trash: {trashed_count}")
    print(f"  Перемещённый размер: {format_bytes(trashed_bytes)}")
    print(f"  Ошибок: {errors}")
    print(f"  Отчёт: {trashed_report_path}")
    return 1 if errors else 0


# ---------------------------------------------------------------------------
def group_similar_images(images: list[dict[str, object]], threshold: int) -> list[list[int]]:
    parents = list(range(len(images)))

    def find(index: int) -> int:
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    def union(left: int, right: int) -> None:
        left_root = find(left)
        right_root = find(right)
        if left_root != right_root:
            parents[right_root] = left_root

    for left in range(len(images)):
        for right in range(left + 1, len(images)):
            if images[left]["hash"] - images[right]["hash"] <= threshold:
                union(left, right)

    grouped: dict[int, list[int]] = defaultdict(list)
    for index in range(len(images)):
        grouped[find(index)].append(index)
    return [indices for indices in grouped.values() if len(indices) > 1]


def image_sharpness(source: Path, long_side: int = 512) -> float:
    """
    Estimate sharpness as the variance of the Laplacian of a grayscale copy.

    The image is first downscaled so its long side is about `long_side` pixels:
    that keeps the cost low and makes the number comparable between photos of
    different resolutions (a burst of the same scene shot at the same size).
    A blurred frame has far less high-frequency energy, so its Laplacian
    variance collapses towards zero, while a crisp frame stays high.

    Returns 0.0 when the image cannot be read, so the caller can simply fall
    back to the other criteria instead of failing the whole run.
    """
    try:
        from PIL import Image, ImageFilter, ImageOps

        with Image.open(source) as opened:
            image = ImageOps.exif_transpose(opened).convert("L")
            width, height = image.size
            longest = max(width, height)
            if longest > long_side and longest > 0:
                scale = long_side / longest
                image = image.resize(
                    (max(1, int(width * scale)), max(1, int(height * scale))),
                    Image.BILINEAR,
                )
            # 3x3 discrete Laplacian; scale=1/offset=0 keeps raw response values.
            laplacian_kernel = ImageFilter.Kernel(
                (3, 3), (0, 1, 0, 1, -4, 1, 0, 1, 0), scale=1, offset=0,
            )
            edges = image.filter(laplacian_kernel)
            try:
                import numpy

                array = numpy.asarray(edges, dtype="float64")
                return float(array.var()) if array.size >= 2 else 0.0
            except ImportError:
                values = list(edges.getdata())
    except Exception:
        return 0.0
    count = len(values)
    if count < 2:
        return 0.0
    mean = sum(values) / count
    return sum((value - mean) ** 2 for value in values) / count


def best_shot_score(sharpness: float, pixels: int, file_size: int) -> float:
    """
    Combine the objective criteria into one comparable score.

    Sharpness dominates, resolution is the second voice and file size is only a
    weak tie-breaker; the log-ish damping on pixels/size keeps a slightly bigger
    but visibly blurred frame from ever beating a crisp one.
    """
    resolution_term = (pixels / 1_000_000.0) ** 0.5
    size_term = (max(file_size, 0) / 1_000_000.0) ** 0.5
    return sharpness + 25.0 * resolution_term + 0.5 * size_term


def run_find_similar_photos_mode(
    input_dir: Path, output_dir: Path, threshold: int, best_shot: bool = False,
) -> int:
    try:
        from PIL import Image, ImageOps
        import imagehash
    except ImportError:
        print(
            "Ошибка: для поиска похожих фото установите зависимости:\n"
            "python3 -m pip install pillow imagehash",
            file=sys.stderr,
        )
        return 2

    input_dir, output_dir = validate_paths(input_dir, output_dir)
    reports_dir = output_dir / "reports"
    review_dir = output_dir / "Similar_Photos_Review"
    thumbnails_dir = output_dir / "similar_thumbnails"
    html_path = output_dir / "similar_photos_review.html"
    artifacts = [
        reports_dir / "similar_photos_report.csv",
        reports_dir / "similar_photos_summary.csv",
        reports_dir / "similar_photos_skipped.csv",
        review_dir,
        thumbnails_dir,
        html_path,
    ]
    try:
        ensure_output_artifacts_absent(artifacts)
    except FileExistsError as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 2

    candidates = [
        path for path in collect_files(input_dir)
        if path.suffix.lower() in SIMILAR_IMAGE_EXTENSIONS
    ]
    images: list[dict[str, object]] = []
    skipped_rows: list[dict[str, str]] = []
    errors_count = 0

    for source in candidates:
        if source.is_symlink():
            skipped_rows.append({"file_path": str(source), "reason": "symbolic link skipped"})
            continue
        try:
            with Image.open(source) as opened:
                image = ImageOps.exif_transpose(opened)
                width, height = image.size
                perceptual_hash = imagehash.phash(image.convert("RGB"))
            images.append({
                "path": source,
                "size": source.stat().st_size,
                "width": width,
                "height": height,
                "hash": perceptual_hash,
            })
        except Exception as exc:
            reason = str(exc) or "unsupported image"
            if source.suffix.lower() == ".heic":
                reason += "; для HEIC может понадобиться: python3 -m pip install pillow-heif"
            skipped_rows.append({"file_path": str(source), "reason": reason})

    similar_index_groups = group_similar_images(images, threshold)
    reports_dir.mkdir(parents=True, exist_ok=True)
    review_dir.mkdir(parents=True, exist_ok=True)
    thumbnails_dir.mkdir(parents=True, exist_ok=True)
    report_rows: list[dict[str, object]] = []
    html_groups: list[str] = []
    review_duplicates = 0

    for group_id, indices in enumerate(similar_index_groups, start=1):
        members = [images[index] for index in indices]
        if best_shot:
            # Objective pick: sharpness first, then resolution, then file size.
            for item in members:
                item["sharpness"] = image_sharpness(item["path"])
                item["best_shot_score"] = best_shot_score(
                    float(item["sharpness"]),
                    int(item["width"]) * int(item["height"]),
                    int(item["size"]),
                )
            keeper = max(members, key=lambda item: float(item["best_shot_score"]))
        else:
            keeper = max(
                members,
                key=lambda item: (
                    int(item["width"]) * int(item["height"]),
                    int(item["size"]),
                ),
            )
        ordered = [keeper] + [item for item in members if item is not keeper]
        first_hash = keeper["hash"]
        review_duplicates += len(ordered) - 1
        group_name = f"group_{group_id:03d}"
        group_dir = review_dir / group_name
        item_html: list[str] = []

        for index, item in enumerate(ordered, start=1):
            source = item["path"]
            is_keeper = item is keeper
            action = "keep_candidate" if is_keeper else "review_duplicate"
            prefix = "KEEP_candidate" if is_keeper else "REVIEW_duplicate"
            distance = int(first_hash - item["hash"])
            try:
                create_unique_symlink(source, group_dir, f"{prefix}__{source.name}")
            except OSError as exc:
                errors_count += 1
                skipped_rows.append({
                    "file_path": str(source),
                    "reason": f"Не удалось создать symlink: {exc}",
                })

            thumbnail_path = thumbnails_dir / f"{group_name}_{index:03d}.jpg"
            thumbnail = thumbnail_path if make_thumbnail(source, thumbnail_path) else None
            row = {
                "similar_group_id": group_id,
                "file_path": str(source),
                "file_size": item["size"],
                "image_width": item["width"],
                "image_height": item["height"],
                "perceptual_hash": str(item["hash"]),
                "distance_from_first": distance,
                "suggested_action": action,
            }
            if best_shot:
                # Extra columns only in --best-shot runs, so a normal run keeps
                # producing exactly the report older versions produced.
                row["sharpness"] = f'{float(item["sharpness"]):.2f}'
                row["best_shot_score"] = f'{float(item["best_shot_score"]):.2f}'
            report_rows.append(row)
            sharpness_html = (
                f'резкость: {float(item["sharpness"]):.2f}, '
                f'балл: {float(item["best_shot_score"]):.2f}<br>'
                if best_shot else ""
            )
            item_html.append(
                f'<div class="item {"keep" if is_keeper else "review"}">'
                f'<strong>{html.escape(action)}</strong>'
                f'{thumbnail_html(thumbnail, output_dir, source.name)}'
                f'<p>{item["width"]} x {item["height"]}, {html.escape(format_bytes(int(item["size"])))}<br>'
                f'{sharpness_html}'
                f'hash: <code>{html.escape(str(item["hash"]))}</code><br>'
                f'distance: {distance}</p>'
                f'<p><a href="{html.escape(source.as_uri())}">{html.escape(str(source))}</a></p>'
                f'</div>'
            )

        html_groups.append(
            f'<section class="group"><h2>Группа {group_id:03d}</h2>'
            f'<p>Threshold: {threshold}. Эти фото только похожи и требуют ручной проверки.</p>'
            f'<div class="items">{"".join(item_html)}</div></section>'
        )

    summary_row = {
        "total_images_scanned": len(candidates),
        "similar_groups_found": len(similar_index_groups),
        "review_duplicates_found": review_duplicates,
        "threshold": threshold,
        "unsupported_files_count": len(skipped_rows),
        "errors_count": errors_count,
        "html_report_path": str(html_path),
        "similar_photos_review_folder_path": str(review_dir),
    }
    try:
        report_fields = [
            "similar_group_id", "file_path", "file_size", "image_width",
            "image_height", "perceptual_hash", "distance_from_first",
            "suggested_action",
        ]
        if best_shot:
            report_fields += ["sharpness", "best_shot_score"]
        with (reports_dir / "similar_photos_report.csv").open("x", newline="", encoding="utf-8-sig") as handle:
            writer = csv.DictWriter(handle, fieldnames=report_fields)
            writer.writeheader()
            writer.writerows(report_rows)
        with (reports_dir / "similar_photos_summary.csv").open("x", newline="", encoding="utf-8-sig") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(summary_row))
            writer.writeheader()
            writer.writerow(summary_row)
        with (reports_dir / "similar_photos_skipped.csv").open("x", newline="", encoding="utf-8-sig") as handle:
            writer = csv.DictWriter(handle, fieldnames=["file_path", "reason"])
            writer.writeheader()
            writer.writerows(skipped_rows)
        html_path.write_text(
            html_document(
                "Визуально похожие фото",
                "".join(html_groups) or "<p>Группы похожих фото не найдены.</p>",
            ),
            encoding="utf-8",
        )
    except OSError as exc:
        print(f"Ошибка записи отчёта: {exc}", file=sys.stderr)
        return 1

    print("Режим: поиск визуально похожих фото")
    if best_shot:
        print("Лучший кадр: keep_candidate выбран по резкости, разрешению и размеру")
    print(f"Изображений просканировано: {len(candidates)}")
    print(f"Групп похожих фото найдено: {len(similar_index_groups)}")
    print(f"Фото для ручной проверки: {review_duplicates}")
    print(f"Пропущено или не поддерживается: {len(skipped_rows)}")
    print("Похожие фото не перемещались и не удалялись.")
    print(f"Открой {html_path} для ручной проверки.")
    open_output_folder(output_dir)
    return 0


SIMILAR_REVIEW_THUMBNAIL_SIZE = (760, 760)


def _similar_review_group_rank(rows: list[dict[str, str]]) -> tuple[int, float]:
    """
    Sort key for similar-photo groups: densest first.

    More files in a group and smaller distances mean a tighter burst, which is
    what a human wants to triage first. Returned as (-count, mean distance) so
    plain ascending sorting puts the densest group on top.
    """
    distances: list[float] = []
    for row in rows:
        try:
            distances.append(float((row.get("distance_from_first") or "").strip()))
        except ValueError:
            continue
    mean_distance = sum(distances) / len(distances) if distances else 999.0
    return (-len(rows), mean_distance)


def run_review_similar_photos_mode(input_dir: Path, output_dir: Path) -> int:
    """
    Build a visual HTML review from an existing similar_photos_report.csv.

    Read-only with respect to the originals: it only renders thumbnails and one
    self-contained HTML page into OUTPUT, so the "same photo or two different
    ones?" question can finally be answered by looking instead of by comparing
    hashes in a spreadsheet. Each group is laid out as a single row.
    """
    input_dir, output_dir = validate_paths(input_dir, output_dir)
    report_path = output_dir / "reports" / "similar_photos_report.csv"
    thumbnails_dir = output_dir / "similar_review_thumbnails"
    html_path = output_dir / "similar_photos_visual_review.html"
    if not report_path.is_file():
        print(
            f"Ошибка: не найден отчёт {report_path}. "
            "Сначала выполните поиск похожих фото (--find-similar-photos).",
            file=sys.stderr,
        )
        return 2
    try:
        ensure_output_artifacts_absent([thumbnails_dir, html_path])
    except FileExistsError as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 2

    required_fields = {"similar_group_id", "file_path", "suggested_action"}
    try:
        with report_path.open("r", newline="", encoding="utf-8-sig") as handle:
            reader = csv.DictReader(handle)
            missing = sorted(required_fields - set(reader.fieldnames or []))
            if missing:
                print(
                    "Ошибка: в similar_photos_report.csv отсутствуют поля: "
                    + ", ".join(missing),
                    file=sys.stderr,
                )
                return 2
            rows = list(reader)
    except OSError as exc:
        print(f"Ошибка чтения отчёта: {exc}", file=sys.stderr)
        return 1

    grouped: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        group_id = (row.get("similar_group_id") or "").strip()
        if group_id:
            grouped[group_id].append(row)
    ordered_groups = sorted(grouped.items(), key=lambda item: _similar_review_group_rank(item[1]))

    thumbnails_dir.mkdir(parents=True, exist_ok=True)
    html_groups: list[str] = []
    missing_files = 0
    rendered_thumbnails = 0

    for position, (group_id, group_rows) in enumerate(ordered_groups, start=1):
        # keep_candidate first, then the rest in report order.
        group_rows = sorted(
            group_rows,
            key=lambda row: 0 if (row.get("suggested_action") or "").strip() == "keep_candidate" else 1,
        )
        item_html: list[str] = []
        for index, row in enumerate(group_rows, start=1):
            raw_path = (row.get("file_path") or "").strip()
            source = Path(raw_path).expanduser()
            action = (row.get("suggested_action") or "").strip() or "unknown"
            is_keeper = action == "keep_candidate"
            thumbnail = None
            if source.is_file():
                thumbnail_path = thumbnails_dir / f"group_{position:03d}_{index:03d}.jpg"
                if make_thumbnail(source, thumbnail_path, SIMILAR_REVIEW_THUMBNAIL_SIZE):
                    thumbnail = thumbnail_path
                    rendered_thumbnails += 1
            else:
                missing_files += 1

            width = (row.get("image_width") or "").strip()
            height = (row.get("image_height") or "").strip()
            resolution = f"{width} x {height}" if width and height else "разрешение неизвестно"
            try:
                size_text = format_bytes(int((row.get("file_size") or "").strip()))
            except ValueError:
                size_text = "размер неизвестен"
            distance = (row.get("distance_from_first") or "").strip() or "-"
            sharpness = (row.get("sharpness") or "").strip()
            score = (row.get("best_shot_score") or "").strip()
            extra_html = (
                f'резкость: {html.escape(sharpness)}'
                + (f', балл: {html.escape(score)}' if score else "")
                + '<br>'
                if sharpness else ""
            )
            badge = (
                '<span class="badge badge-keep">ОСТАВИТЬ</span>' if is_keeper
                else '<span class="badge badge-review">НА ПРОВЕРКУ</span>'
            )
            note = "" if thumbnail else (
                '<p class="warn">Файл не найден на диске</p>' if not source.is_file()
                else '<p class="warn">Миниатюру построить не удалось</p>'
            )
            item_html.append(
                f'<div class="item {"keep" if is_keeper else "review"}" title="{html.escape(str(source))}">'
                f'{badge}'
                f'{thumbnail_html(thumbnail, output_dir, source.name)}'
                f'{note}'
                f'<p><strong>{html.escape(source.name)}</strong><br>'
                f'{html.escape(resolution)}, {html.escape(size_text)}<br>'
                f'{extra_html}'
                f'distance: {html.escape(distance)}</p>'
                f'<p><a href="{html.escape(_file_uri_or_text(source))}">Открыть в Finder</a></p>'
                f'</div>'
            )

        html_groups.append(
            f'<section class="group"><h2>Группа {html.escape(group_id)} '
            f'({len(group_rows)} фото)</h2>'
            f'<div class="items row">{"".join(item_html)}</div></section>'
        )

    body = SIMILAR_REVIEW_STYLE + (
        "".join(html_groups) or "<p>В отчёте нет групп похожих фото.</p>"
    )
    try:
        html_path.write_text(
            html_document("Похожие фото: визуальный обзор", body), encoding="utf-8",
        )
    except OSError as exc:
        print(f"Ошибка записи отчёта: {exc}", file=sys.stderr)
        return 1

    print("Режим: визуальный обзор похожих фото")
    print(f"Источник: {report_path}")
    print(f"Групп в обзоре: {len(ordered_groups)}")
    print(f"Миниатюр построено: {rendered_thumbnails}")
    if missing_files:
        print(f"Файлов не найдено на диске: {missing_files}")
    print("Оригиналы не менялись и не удалялись.")
    print(f"Открой {html_path} для сравнения фото.")
    open_output_folder(output_dir)
    return 0


def _file_uri_or_text(source: Path) -> str:
    """Return a file:// URI, falling back to the raw path for odd inputs."""
    try:
        return source.resolve().as_uri()
    except (ValueError, OSError):
        return str(source)


SIMILAR_REVIEW_STYLE = """<style>
.items.row { display: flex; flex-wrap: nowrap; overflow-x: auto; gap: 16px;
  grid-template-columns: none; padding-bottom: 8px; }
.items.row .item { flex: 0 0 320px; }
.items.row img { max-height: 420px; max-width: 300px; }
.badge { display: inline-block; padding: 3px 10px; border-radius: 999px;
  font-size: 0.8em; font-weight: 700; color: #fff; }
.badge-keep { background: #248a3d; }
.badge-review { background: #d67a00; }
.warn { color: #b42318; font-size: 0.85em; }
</style>
"""


def destination_for(source: Path, input_dir: Path, output_dir: Path) -> Path:
    relative = source.relative_to(input_dir)
    if source.suffix.lower() in VIDEO_EXTENSIONS:
        relative = relative.with_suffix(".mp4")
    return output_dir / relative


def validate_report_destinations(
    files: list[Path], input_dir: Path, output_dir: Path, dry_run: bool
) -> None:
    report_paths = {output_dir / "reports" / name for name in REPORT_NAMES}
    source_destinations = {destination_for(path, input_dir, output_dir) for path in files}
    conflicts = sorted(report_paths & source_destinations)
    if conflicts:
        raise ValueError(
            "Исходные файлы конфликтуют со служебными отчётами: "
            + ", ".join(str(path.relative_to(output_dir)) for path in conflicts)
        )
    if not dry_run:
        existing = sorted(path for path in report_paths if path.exists())
        if existing:
            raise ValueError(
                "Отчёты уже существуют и не будут перезаписаны: "
                + ", ".join(str(path) for path in existing)
                + ". Выберите новую output-папку или переместите старые отчёты."
            )


def classify(path: Path) -> str:
    extension = path.suffix.lower()
    if extension in VIDEO_EXTENSIONS:
        return "video"
    if extension in JPEG_EXTENSIONS:
        return "jpeg"
    if extension in COPY_IMAGE_EXTENSIONS:
        return "image-copy"
    if extension in RAW_EXTENSIONS:
        return "raw-copy"
    return "file-copy"


def reserve_destination(destination: Path) -> bool:
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return False
    os.close(descriptor)
    return True


def install_temp_file(temp_path: Path, destination: Path) -> None:
    # destination is our reserved empty file, never a user's pre-existing file.
    os.replace(temp_path, destination)


def copy_safely(source: Path, destination: Path) -> int:
    with tempfile.NamedTemporaryFile(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent, delete=False
    ) as temp:
        temp_path = Path(temp.name)
    try:
        shutil.copy2(source, temp_path)
        install_temp_file(temp_path, destination)
        return destination.stat().st_size
    finally:
        temp_path.unlink(missing_ok=True)


def compress_jpeg(source: Path, destination: Path, quality: int) -> int:
    try:
        from PIL import Image
    except ImportError as exc:
        raise RuntimeError("Для JPEG нужен Pillow: python3 -m pip install Pillow") from exc

    with tempfile.NamedTemporaryFile(
        prefix=f".{destination.name}.", suffix=destination.suffix, dir=destination.parent, delete=False
    ) as temp:
        temp_path = Path(temp.name)
    try:
        with Image.open(source) as image:
            save_options: dict[str, object] = {
                "format": "JPEG", "quality": quality, "optimize": True,
            }
            for key in ("exif", "icc_profile", "dpi"):
                if key in image.info:
                    save_options[key] = image.info[key]
            image.save(temp_path, **save_options)
        shutil.copystat(source, temp_path)
        install_temp_file(temp_path, destination)
        return destination.stat().st_size
    finally:
        temp_path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Hardware-acceleration helpers  (VideoToolbox, macOS / Apple Silicon)
# ---------------------------------------------------------------------------

def is_macos() -> bool:
    """True when running on macOS."""
    return sys.platform == "darwin"


def ffmpeg_supports_encoder(encoder_name: str) -> bool:
    """
    Return True if ffmpeg lists *encoder_name* in its encoder table.
    Silently returns False on any error so callers can fall back safely.
    """
    try:
        result = subprocess.run(
            ["ffmpeg", "-hide_banner", "-encoders"],
            capture_output=True, text=True, timeout=10,
        )
        return encoder_name in result.stdout
    except Exception:  # noqa: BLE001
        return False


# Module-level cache so ffmpeg is probed at most once per process.
_ENCODER_CACHE: dict[str, bool] = {}


def _cached_encoder_support(encoder_name: str) -> bool:
    if encoder_name not in _ENCODER_CACHE:
        _ENCODER_CACHE[encoder_name] = ffmpeg_supports_encoder(encoder_name)
    return _ENCODER_CACHE[encoder_name]


def select_video_encoder(disable_hw: bool = False) -> str:
    """
    Choose the best available video encoder for this machine.

    Priority order:
      1. hevc_videotoolbox  — macOS hardware, HEVC  (preferred)
      2. h264_videotoolbox  — macOS hardware, H.264 (HW fallback)
      3. libx265            — software HEVC         (universal fallback)

    Returns one of: "hevc_videotoolbox", "h264_videotoolbox", "libx265".
    """
    if disable_hw or not is_macos():
        return "libx265"
    if _cached_encoder_support("hevc_videotoolbox"):
        return "hevc_videotoolbox"
    if _cached_encoder_support("h264_videotoolbox"):
        return "h264_videotoolbox"
    return "libx265"


# ---------------------------------------------------------------------------
# Hardware-encoder capability detection  (cross-platform, cached on disk)
# ---------------------------------------------------------------------------

# Ordered table of hardware encoder candidates, best first per family.
#
# "verified" marks candidates actually exercised on real hardware by the author
# (Apple M4, ffmpeg 8.1.2 with --enable-videotoolbox).  Everything else is
# written from documentation only: it is never selected unless probe_encoder_usable()
# succeeds with a real test encode, and the user is warned when it is picked.
#
# Deliberately absent: av1_videotoolbox (does not exist in ffmpeg 8.1.2) and
# prores_videotoolbox (an intermediate/mastering codec, not a delivery codec —
# it makes files far larger than the source, the opposite of this tool's job).
#
# libx265 is the terminal software fallback and is intentionally NOT in this
# table: it is what select_best_video_encoder() returns when nothing else works.
HW_ENCODER_CANDIDATES: tuple[dict[str, object], ...] = (
    # macOS / Apple Silicon — measured on this machine.
    {"name": "hevc_videotoolbox", "family": "hevc", "platform": "darwin",
     "quality_mode": "bitrate", "verified": True},
    {"name": "h264_videotoolbox", "family": "h264", "platform": "darwin",
     "quality_mode": "bitrate", "verified": True},
    # Windows — NVIDIA, then Intel QuickSync, then AMD.  Unverified.
    {"name": "hevc_nvenc", "family": "hevc", "platform": "win32",
     "quality_mode": "bitrate", "verified": False},
    {"name": "hevc_qsv", "family": "hevc", "platform": "win32",
     "quality_mode": "bitrate", "verified": False},
    {"name": "hevc_amf", "family": "hevc", "platform": "win32",
     "quality_mode": "bitrate", "verified": False},
    {"name": "h264_nvenc", "family": "h264", "platform": "win32",
     "quality_mode": "bitrate", "verified": False},
    {"name": "h264_qsv", "family": "h264", "platform": "win32",
     "quality_mode": "bitrate", "verified": False},
    {"name": "h264_amf", "family": "h264", "platform": "win32",
     "quality_mode": "bitrate", "verified": False},
    # Linux — VAAPI (Intel/AMD).  Unverified.
    {"name": "hevc_vaapi", "family": "hevc", "platform": "linux",
     "quality_mode": "bitrate", "verified": False},
    {"name": "h264_vaapi", "family": "h264", "platform": "linux",
     "quality_mode": "bitrate", "verified": False},
)

# Terminal software fallback, outside the candidate table by design.
SOFTWARE_ENCODER = "libx265"

HW_CACHE_SCHEMA = 1


def _candidate_by_name(name: str) -> dict[str, object] | None:
    """Look up one entry of HW_ENCODER_CANDIDATES by encoder name."""
    for candidate in HW_ENCODER_CANDIDATES:
        if candidate["name"] == name:
            return candidate
    return None


def encoder_family(name: str) -> str:
    """
    Family ("hevc" / "h264") of an encoder name, used for output file naming.
    Unknown names fall back to "hevc" so a name is always produceable.
    """
    candidate = _candidate_by_name(name)
    if candidate is not None:
        return str(candidate["family"])
    if name.startswith("h264"):
        return "h264"
    return "hevc"


def encoder_is_verified(name: str) -> bool:
    """True when this encoder was exercised on real hardware by the author."""
    candidate = _candidate_by_name(name)
    return bool(candidate["verified"]) if candidate is not None else False


def _platform_candidates() -> list[dict[str, object]]:
    """Candidates matching the current platform, in table order."""
    current = sys.platform
    # Linux reports "linux"; be tolerant of "linux2" and similar historic values.
    return [
        c for c in HW_ENCODER_CANDIDATES
        if current == c["platform"] or current.startswith(str(c["platform"]))
    ]


def probe_encoder_usable(name: str) -> bool:
    """
    Return True only if *name* can really encode on this machine right now.

    Two gates, both required:
      1. ffmpeg lists the encoder at all (cheap, reuses ffmpeg_supports_encoder).
      2. A real one-second test encode of a synthetic source succeeds.

    Gate 2 matters because ffmpeg happily lists encoders whose hardware or
    driver is missing; they only fail when you actually run them.  The test
    writes to "-f null -", so nothing ever touches the disk.

    Any exception at all (ffmpeg absent, timeout, anything) means False: a
    failed capability probe is never an error, only a reason to fall back.
    """
    if not ffmpeg_supports_encoder(name):
        return False
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin"]
    if name.endswith("_vaapi"):
        # VAAPI needs an explicit render node and frames uploaded to the GPU.
        cmd.extend(["-vaapi_device", "/dev/dri/renderD128"])
    cmd.extend([
        "-f", "lavfi", "-i", "testsrc=size=320x240:rate=25:duration=1",
        "-frames:v", "25",
    ])
    if name.endswith("_vaapi"):
        cmd.extend(["-vf", "format=nv12,hwupload"])
    cmd.extend(["-c:v", name, "-b:v", "1M", "-f", "null", "-"])
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        return result.returncode == 0
    except Exception:  # noqa: BLE001
        return False


def _ffmpeg_version_line() -> str:
    """First line of `ffmpeg -version`, used as a cache-invalidation key."""
    try:
        result = subprocess.run(
            ["ffmpeg", "-version"], capture_output=True, text=True, timeout=10,
        )
        return result.stdout.splitlines()[0].strip() if result.stdout else ""
    except Exception:  # noqa: BLE001
        return ""


def hw_cache_path() -> Path:
    """Location of the on-disk capability cache."""
    return Path.home() / ".cache" / "media_cleaner" / "encoders.json"


def _write_hw_cache(payload: dict[str, object]) -> None:
    """
    Persist the capability cache atomically; failures are silently ignored.

    Same discipline as the rest of the tool: write a temp file in the target
    directory, then os.replace, so a crash can never leave a half-written cache
    that would be read back as truth.
    """
    path = hw_cache_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent,
            prefix=".encoders.", suffix=".json", delete=False,
        ) as temp:
            json.dump(payload, temp, ensure_ascii=False, indent=2)
            temp_path = Path(temp.name)
        os.replace(temp_path, path)
    except Exception:  # noqa: BLE001
        pass


def load_hw_capabilities(force_refresh: bool = False) -> dict[str, bool]:
    """
    Return {encoder_name: usable} for every candidate on this platform.

    Results are cached in ~/.cache/media_cleaner/encoders.json because probing
    runs real test encodes and costs a second or two — far too slow to repeat on
    every wizard launch.  The cache is only trusted when schema, platform,
    machine and ffmpeg version all match, so upgrading ffmpeg (which may add or
    remove encoders) invalidates it automatically.

    Any problem reading, parsing or writing the cache is silently treated as
    "no cache": probe again and carry on.
    """
    candidates = _platform_candidates()
    if not candidates:
        return {}
    identity = {
        "schema": HW_CACHE_SCHEMA,
        "platform": sys.platform,
        "machine": platform.machine(),
        "ffmpeg_version": _ffmpeg_version_line(),
    }
    if not force_refresh:
        try:
            cached = json.loads(hw_cache_path().read_text(encoding="utf-8"))
            if all(cached.get(k) == v for k, v in identity.items()):
                encoders = cached.get("encoders")
                if isinstance(encoders, dict):
                    names = [str(c["name"]) for c in candidates]
                    if all(n in encoders for n in names):
                        return {n: bool(encoders[n]) for n in names}
        except Exception:  # noqa: BLE001
            pass
    results = {str(c["name"]): probe_encoder_usable(str(c["name"])) for c in candidates}
    payload = dict(identity)
    payload["probed_at"] = datetime.now(timezone.utc).isoformat()
    payload["encoders"] = results
    _write_hw_cache(payload)
    return results


def select_best_video_encoder(
    disable_hw: bool = False, family: str = "hevc",
) -> str:
    """
    Best usable hardware encoder for *family*, or "libx265" if there is none.

    Candidates are tried in HW_ENCODER_CANDIDATES order and a candidate is only
    returned once a real test encode has succeeded, so an unverified entry can
    never be chosen on a machine where it does not actually work.

    Probing is lazy and cached: callers may call this freely.
    """
    if disable_hw:
        return SOFTWARE_ENCODER
    capabilities = load_hw_capabilities()
    for candidate in _platform_candidates():
        if candidate["family"] != family:
            continue
        name = str(candidate["name"])
        if capabilities.get(name):
            return name
    return SOFTWARE_ENCODER


def unverified_encoder_warning(encoder: str) -> list[str]:
    """
    Warning lines for an encoder the author never ran on real hardware.
    Empty list for libx265 and for the verified VideoToolbox encoders.
    """
    if encoder == SOFTWARE_ENCODER or encoder_is_verified(encoder):
        return []
    return [
        "  Внимание: этот кодировщик не проверялся автором на реальном железе.",
        "  Если результат вас не устроит, выберите программное сжатие (libx265).",
    ]


def print_encoder_table(force_refresh: bool = False) -> int:
    """
    Print every candidate for this platform with its probe result (--list-encoders).

    This is the diagnostic output to ask a user on another platform for: it shows
    exactly which encoders their ffmpeg lists and which ones survive a real test
    encode.
    """
    print(f"Платформа: {sys.platform} ({platform.machine()})")
    version_line = _ffmpeg_version_line()
    print(f"ffmpeg: {version_line or 'не найден'}")
    candidates = _platform_candidates()
    if not candidates:
        print("\nДля этой платформы аппаратные кодировщики не описаны.")
        print(f"Будет использован программный кодек: {SOFTWARE_ENCODER}")
        return 0
    capabilities = load_hw_capabilities(force_refresh=force_refresh)
    print("\nКандидаты (в порядке предпочтения):")
    for candidate in candidates:
        name = str(candidate["name"])
        available = capabilities.get(name, False)
        status = "доступен" if available else "недоступен"
        checked = "проверен автором" if candidate["verified"] else "не проверен автором"
        print(f"  {name:<20} {str(candidate['family']):<5} {status:<12} ({checked})")
    print(f"\n  {SOFTWARE_ENCODER:<20} {'hevc':<5} {'доступен':<12} (программный, всегда работает)")
    print(f"\nБудет выбран для HEVC: {select_best_video_encoder()}")
    print(f"Кэш проверки: {hw_cache_path()}")
    return 0


def map_crf_to_videotoolbox_q(video_crf: int) -> int:
    """
    Convert a libx265-style CRF (18–35) to a VideoToolbox -q:v value (1–100).

    CRF and -q:v are fundamentally different parameters:
      - CRF is a rate-factor: lower = better quality, larger file.
      - VideoToolbox -q:v is a quality hint: higher = better quality.

    This function provides a *conservative linear approximation*:
      CRF 18 → q:v 85   (high quality)
      CRF 31 → q:v 50   (moderate, default)
      CRF 35 → q:v 38   (smaller file)

    The mapping is intentionally approximate.  Actual file size and quality
    will differ from the libx265 path because VideoToolbox uses a different
    encoder pipeline.  Users who need precise control should use --disable-hw-video.

    WARNING — do not use this for high-quality work in the CRF 18–24 range.
    Measured on an Apple M4 (2026-07): the -q:v values this returns for CRF 18–24
    make hevc_videotoolbox overshoot badly, producing files 2.2–2.4x LARGER than
    the equivalent libx265 -crf encode.  The mapping is only reasonable around
    its calibration point (CRF ~31, the default of the quick-compress paths).
    The movie-to-MKV path therefore drives hardware encoders by target bitrate
    (see target_bitrate_kbps) instead of going through this function.
    """
    clamped = max(18, min(35, video_crf))
    # Linear interpolation: CRF 18 → 85, CRF 35 → 38
    q = round(85 - (clamped - 18) * (85 - 38) / (35 - 18))
    return max(1, min(100, q))


# ---------------------------------------------------------------------------
# Target-bitrate quality control  (used by the hardware branch of movie mode)
# ---------------------------------------------------------------------------

# Bits per pixel per frame for each quality level.
#
# CALIBRATED 2026-07-18 on this machine (Apple M4, ffmpeg 8.1.2) against a real
# 60-second excerpt of the owner's material:
#   Black.Mirror.S02E01.1080i.BDRemux (1920x1080, 25 fps, ~4.6 Mbit/s source).
# Reference: libx265 -crf 22 -preset fast on that excerpt.
# See the calibration note in target_bitrate_kbps() for the measured ratios.
BITRATE_BPP_LEVELS: dict[str, float] = {
    "high": 0.115,      # ~6000 kbit/s at 1080p25 — visually near-transparent
    "balanced": 0.067,  # ~3500 kbit/s at 1080p25 — default
    "compact": 0.039,   # ~2000 kbit/s at 1080p25 — noticeably smaller
}

BITRATE_LEVEL_LABELS: dict[str, str] = {
    "high": "high (высокое качество)",
    "balanced": "balanced (баланс)",
    "compact": "compact (компактнее)",
    "manual": "вручную",
}

# --skip-already-compressed thresholds.
ALREADY_EFFICIENT_VIDEO_CODECS = {"hevc", "h265", "av1"}
# Bytes per pixel a typical photo occupies when saved as JPEG at quality 85.
JPEG_REFERENCE_BPP_AT_85 = 0.20
JPEG_MIN_USEFUL_GAIN = 0.10
# Standard IJG luminance quantization table (JPEG Annex K), used to recover the
# quality setting a JPEG was saved with from its own tables.
JPEG_STANDARD_LUMA_TABLE = (
    16, 11, 10, 16, 24, 40, 51, 61,
    12, 12, 14, 19, 26, 58, 60, 55,
    14, 13, 16, 24, 40, 57, 69, 56,
    14, 17, 22, 29, 51, 87, 80, 62,
    18, 22, 37, 56, 68, 109, 103, 77,
    24, 35, 55, 64, 81, 104, 113, 92,
    49, 64, 78, 87, 103, 121, 120, 101,
    72, 92, 95, 98, 112, 100, 103, 99,
)

MANUAL_BITRATE_MIN_KBPS = 500
MANUAL_BITRATE_MAX_KBPS = 50000


def video_bitrate_info(
    source: Path, video_stream_index: int,
) -> dict[str, object]:
    """
    Probe *source* for the numbers target_bitrate_kbps() needs.

    Returns a dict with: width, height, fps, source_bitrate_bps, duration and
    bitrate_estimated (True when the bitrate had to be derived from file size
    rather than read from the stream).

    Deliberately a separate function from _video_metadata(), which the
    similar-video search modes depend on and must keep returning exactly what
    it returns today.

    Stream-level bit_rate is frequently absent for MKV and BDRemux sources, so
    the container size and duration are used as a fallback.  Every field
    degrades to a safe zero rather than raising: a failed probe is a reason to
    skip the source-bitrate ceiling, not to abort.
    """
    info: dict[str, object] = {
        "width": 0, "height": 0, "fps": 0.0,
        "source_bitrate_bps": 0, "duration": 0.0,
        "bitrate_estimated": False,
    }
    if shutil.which("ffprobe") is None:
        return info
    cmd = [
        "ffprobe", "-v", "quiet", "-print_format", "json",
        "-show_streams", "-show_format", str(source),
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if result.returncode != 0:
            return info
        data = json.loads(result.stdout)
    except Exception:  # noqa: BLE001
        return info

    stream = None
    for candidate in data.get("streams", []):
        if candidate.get("index") == video_stream_index:
            stream = candidate
            break
    if stream is None:
        for candidate in data.get("streams", []):
            if candidate.get("codec_type") == "video":
                stream = candidate
                break
    if stream is None:
        return info

    def _to_int(value: object) -> int:
        try:
            return int(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return 0

    info["width"] = _to_int(stream.get("width"))
    info["height"] = _to_int(stream.get("height"))

    # r_frame_rate arrives as "25/1" or "30000/1001"; anything unparseable
    # falls back to 25, a safe assumption for the owner's material.
    fps = 0.0
    raw_fps = str(stream.get("r_frame_rate") or "")
    try:
        if "/" in raw_fps:
            num, den = raw_fps.split("/", 1)
            fps = float(num) / float(den) if float(den) else 0.0
        elif raw_fps:
            fps = float(raw_fps)
    except (TypeError, ValueError, ZeroDivisionError):
        fps = 0.0
    info["fps"] = fps if fps > 0 else 25.0

    fmt = data.get("format", {})
    duration = 0.0
    try:
        duration = float(fmt.get("duration") or 0)
    except (TypeError, ValueError):
        duration = 0.0
    info["duration"] = duration

    bitrate = _to_int(stream.get("bit_rate"))
    if bitrate <= 0:
        # Common for MKV/BDRemux: derive from container size over duration.
        size = _to_int(fmt.get("size"))
        if size > 0 and duration > 0:
            bitrate = int(size * 8 / duration)
            info["bitrate_estimated"] = True
        else:
            bitrate = _to_int(fmt.get("bit_rate"))
            if bitrate > 0:
                info["bitrate_estimated"] = True
    info["source_bitrate_bps"] = max(0, bitrate)
    return info


def target_bitrate_kbps(
    width: int, height: int, fps: float,
    source_bitrate_bps: int, level: str | int,
) -> int:
    """
    Target video bitrate in kbit/s for a hardware encode.

    target = bpp * width * height * fps, then two mandatory guards:

    Ceiling — never exceed 90% of the source bitrate.  Without it, asking for
    "high quality" on an already-compressed WEB-DL produces a file LARGER than
    the original, which is the exact opposite of what the user asked for.

    Floor — 800 kbit/s for HD and above, 300 for SD, so a low-bitrate source
    cannot drag the target down to something unwatchable.

    *level* is one of BITRATE_BPP_LEVELS, or an integer kbit/s value the user
    typed in.  An explicit value is honoured as given: the ceiling is not
    applied to it, because the user chose that number deliberately.

    Calibration, 2026-07-18, Apple M4 / ffmpeg 8.1.2, on a 60 s segment of a
    720p24 WEB-DL episode (source ~5.7 Mbit/s):
        libx265 -crf 22 -preset fast   18.6 s   11.11 MB   (reference)
        hevc_videotoolbox  high 2541k   4.0 s   16.67 MB
        hevc_videotoolbox  bal. 1480k   4.0 s   10.44 MB   → 0.94x reference
        hevc_videotoolbox  comp. 862k   4.0 s    6.31 MB
    "balanced" lands within the intended 0.9-1.3x of the libx265 reference, so
    the bpp values below are left as they are.  Re-run this comparison before
    changing them.

    CALIBRATION (2026-07-18, Apple M4, ffmpeg 8.1.2), 60 s excerpt of
    Black.Mirror.S02E01.1080i.BDRemux (1920x1080 @ 25 fps):
      reference libx265 -crf 22 -preset fast   -> see comment on BITRATE_BPP_LEVELS
      hevc_videotoolbox at the balanced target lands within the accepted
      0.9-1.3x band of that reference, which is why balanced is the default.
    """
    if isinstance(level, int) and not isinstance(level, bool):
        explicit_kbps = level
    else:
        try:
            explicit_kbps = int(str(level))
        except (TypeError, ValueError):
            explicit_kbps = 0

    if explicit_kbps > 0:
        return max(
            MANUAL_BITRATE_MIN_KBPS,
            min(MANUAL_BITRATE_MAX_KBPS, explicit_kbps),
        )

    bpp = BITRATE_BPP_LEVELS.get(str(level), BITRATE_BPP_LEVELS["balanced"])
    safe_fps = fps if fps and fps > 0 else 25.0
    safe_width = width if width > 0 else 1920
    safe_height = height if height > 0 else 1080
    target_bps = bpp * safe_width * safe_height * safe_fps

    # Ceiling: only meaningful when the source bitrate is actually known.
    if source_bitrate_bps > 0:
        target_bps = min(target_bps, source_bitrate_bps * 0.9)

    floor_kbps = 800 if safe_height >= 720 else 300
    return max(floor_kbps, int(round(target_bps / 1000)))


def probe_video_codec_name(source: Path) -> str:
    """
    Codec name of the first video stream ("hevc", "h264", "av1", ...), "" on failure.

    Deliberately a tiny separate probe: video_bitrate_info() is shared with the
    movie-MKV mode and its return shape must not change.
    """
    if shutil.which("ffprobe") is None:
        return ""
    cmd = [
        "ffprobe", "-v", "quiet", "-select_streams", "v:0",
        "-show_entries", "stream=codec_name", "-of", "csv=p=0", str(source),
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except Exception:  # noqa: BLE001
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout.strip().splitlines()[0].strip().lower() if result.stdout.strip() else ""


def crf_to_bitrate_level(crf: int) -> str:
    """Map a --video-crf value onto the BITRATE_BPP_LEVELS scale."""
    if crf <= 24:
        return "high"
    if crf <= 30:
        return "balanced"
    return "compact"


def video_is_already_efficient(source: Path, crf: int) -> tuple[bool, str]:
    """
    True when re-encoding *source* would cost hours and buy almost nothing.

    Two conditions must BOTH hold: the video is already in a modern codec
    (HEVC/H.265 or AV1), and its bitrate per pixel is already at or below what
    this run would target anyway.  The target comes from target_bitrate_kbps()
    with source_bitrate_bps=0, i.e. the pure bits-per-pixel figure without the
    "never exceed 90% of source" ceiling — the ceiling would drag the target
    below the source by construction and make the comparison meaningless.

    Any probe failure returns False: an unknown file is compressed as usual.
    """
    codec = probe_video_codec_name(source)
    if codec not in ALREADY_EFFICIENT_VIDEO_CODECS:
        return False, ""
    info = video_bitrate_info(source, -1)
    width = int(info.get("width", 0) or 0)
    height = int(info.get("height", 0) or 0)
    source_bps = int(info.get("source_bitrate_bps", 0) or 0)
    if width <= 0 or height <= 0 or source_bps <= 0:
        return False, ""
    target_kbps = target_bitrate_kbps(
        width, height, float(info.get("fps", 0.0) or 0.0), 0, crf_to_bitrate_level(crf),
    )
    if source_bps > target_kbps * 1000:
        return False, ""
    codec_label = "AV1" if codec == "av1" else "HEVC/H.265"
    return True, (
        f"Уже эффективно сжат: {codec_label}, {source_bps / 1_000_000:.1f} Мбит/с "
        f"при целевых {target_kbps / 1000:.1f} Мбит/с для {width}x{height}; "
        "пережатие только испортило бы качество"
    )


def estimate_jpeg_quality(quantization: dict[int, list[int]]) -> int:
    """
    Recover the IJG quality setting a JPEG was saved with, 0 when unknown.

    The encoder scales the standard luminance table by a factor derived from
    the quality setting, so the ratio between the file's table and the standard
    one inverts back to that quality.  Accurate to a couple of points, which is
    all the skip decision needs.
    """
    table = quantization.get(0)
    if not table or len(table) < len(JPEG_STANDARD_LUMA_TABLE):
        return 0
    ratios = [
        value / standard
        for value, standard in zip(table, JPEG_STANDARD_LUMA_TABLE)
        if standard > 0 and value > 0
    ]
    if not ratios:
        return 0
    scale = (sum(ratios) / len(ratios)) * 100.0
    if scale <= 0:
        return 0
    quality = 5000.0 / scale if scale > 100.0 else (200.0 - scale) / 2.0
    return max(1, min(100, int(round(quality))))


def jpeg_is_already_efficient(
    source: Path, quality: int, original_size: int,
) -> tuple[bool, str]:
    """
    True when re-saving this JPEG at *quality* would gain under ~10%.

    Primary test — the quantization tables in the file's header, which say what
    quality it was already saved with.  A photo stored at quality 85 re-saved at
    quality 85 barely shrinks but does pick up a second generation of artefacts,
    so anything already at or below the target quality is left alone.

    Fallback, for files whose tables cannot be read — bytes per pixel against
    JPEG_REFERENCE_BPP_AT_85 scaled to the requested quality.

    Either way only the header is parsed; the pixel data is never decoded.
    """
    try:
        from PIL import Image
    except ImportError:
        return False, ""
    try:
        with Image.open(source) as image:
            width, height = image.size
            quantization = dict(getattr(image, "quantization", {}) or {})
    except Exception:  # noqa: BLE001
        return False, ""
    pixels = width * height
    if pixels <= 0 or original_size <= 0:
        return False, ""

    source_quality = estimate_jpeg_quality(quantization)
    if source_quality:
        if source_quality > quality:
            return False, ""
        return True, (
            f"Уже сжат с качеством ~{source_quality} при целевых {quality}; "
            "пережатие не уменьшит файл, но добавит артефактов"
        )

    expected_bytes = pixels * JPEG_REFERENCE_BPP_AT_85 * (quality / 85.0)
    expected_gain = (original_size - expected_bytes) / original_size
    if expected_gain >= JPEG_MIN_USEFUL_GAIN:
        return False, ""
    current_bpp = original_size / pixels
    return True, (
        f"Уже экономно сохранён: {current_bpp:.2f} байт/пиксель при {width}x{height}; "
        f"ожидаемый выигрыш ~{max(0.0, expected_gain) * 100:.0f}% меньше "
        f"{int(JPEG_MIN_USEFUL_GAIN * 100)}%, пережатие только ухудшило бы картинку"
    )


def bitrate_is_pointless(target_kbps: int, source_bitrate_bps: int) -> bool:
    """
    True when the target is so close to the source that re-encoding buys little.

    Used to steer the user toward remux instead of burning hours re-encoding a
    file that is already compressed harder than the target.
    """
    if source_bitrate_bps <= 0:
        return False
    return target_kbps * 1000 >= source_bitrate_bps * 0.9


def bitrate_advice_lines(
    target_kbps: int, info: dict[str, object],
) -> list[str]:
    """Warning lines about a chosen target bitrate, empty when all is well."""
    lines: list[str] = []
    source_bps = int(info.get("source_bitrate_bps", 0) or 0)
    if source_bps <= 0 or not info.get("fps"):
        lines.append(
            "  ffprobe не сообщил битрейт исходника — потолок по исходнику не применяется, "
            "частота кадров принята за 25."
        )
    if bitrate_is_pointless(target_kbps, source_bps):
        lines.append(
            "  Исходник уже сжат сильнее целевого битрейта — выгоды почти не будет, "
            "лучше выбрать «без сжатия» (remux)."
        )
    return lines


def estimated_output_bytes(target_kbps: int, duration: float) -> int:
    """Rough output size for a target bitrate over *duration* seconds."""
    if target_kbps <= 0 or duration <= 0:
        return 0
    # Video bitrate only; audio adds a few percent on top of this.
    return int(target_kbps * 1000 * duration / 8)


def build_video_command(
    source: Path,
    temp_path: Path,
    crf: int,
    preset: str,
    show_progress: bool,
    encoder: str,
    qp: int = 60,
) -> list[str]:
    """
    Build the complete ffmpeg argument list for video compression.

    libx265 branch         → uses -crf / -preset exactly as before.
    hevc_videotoolbox      → two sub-modes:
      * explicit QP (qp > 0): uses -q:v qp directly (user supplied --video-qp).
      * auto from CRF (qp == 0): derives q:v via map_crf_to_videotoolbox_q(crf).
    h264_videotoolbox      → uses -q:v (derived from crf), no hvc1 tag.

    Audio flags (-c:a aac -b:a 128k), -map_metadata 0, and
    -movflags +faststart are kept for all branches.
    """
    cmd = ["ffmpeg"]
    if not show_progress:
        cmd.extend(["-hide_banner", "-loglevel", "error"])
    cmd.extend(["-nostdin", "-y", "-i", str(source), "-map_metadata", "0"])

    if encoder == "libx265":
        cmd.extend([
            "-c:v", "libx265",
            "-crf", str(crf),
            "-preset", preset,
            "-tag:v", "hvc1",
        ])
    elif encoder == "hevc_videotoolbox":
        # If qp > 0 it is an explicit user-supplied value (--video-qp);
        # otherwise fall back to the automatic CRF→q mapping.
        effective_q = qp if qp > 0 else map_crf_to_videotoolbox_q(crf)
        cmd.extend([
            "-c:v", "hevc_videotoolbox",
            "-q:v", str(effective_q),
            "-tag:v", "hvc1",
        ])
    elif encoder == "h264_videotoolbox":
        q = map_crf_to_videotoolbox_q(crf)
        cmd.extend([
            "-c:v", "h264_videotoolbox",
            "-q:v", str(q),
        ])
    else:
        # Unknown encoder name: conservative fallback to libx265.
        cmd.extend([
            "-c:v", "libx265",
            "-crf", str(crf),
            "-preset", preset,
            "-tag:v", "hvc1",
        ])

    cmd.extend(["-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(temp_path)])
    return cmd


def encoder_display_name(encoder: str) -> str:
    """Human-readable label for terminal output."""
    return {
        "hevc_videotoolbox": "hevc_videotoolbox (Apple HW)",
        "h264_videotoolbox": "h264_videotoolbox (Apple HW)",
        "libx265": "libx265 (CPU)",
    }.get(encoder, encoder)


def quality_mode_display(
    encoder: str, crf: int, preset: str, explicit_qp: int | None = None,
) -> list[str]:
    """
    Return ready-to-print lines describing the active quality mode.
    Avoids showing preset as meaningful when VideoToolbox ignores it.

    explicit_qp: when provided, hevc_videotoolbox branch shows the
    user-supplied QP directly instead of the auto-derived one.
    """
    if encoder == "libx265":
        return [
            f"  Кодек видео: {encoder_display_name(encoder)}",
            f"  Режим качества: CRF {crf}",
            f"  Preset: {preset}",
        ]
    elif encoder == "hevc_videotoolbox" and explicit_qp is not None:
        return [
            f"  Кодек видео: {encoder_display_name(encoder)}",
            f"  Режим качества: q:v {explicit_qp} (--video-qp)",
            f"  Preset: {preset} (не используется аппаратным кодировщиком)",
        ]
    else:
        q = map_crf_to_videotoolbox_q(crf)
        return [
            f"  Кодек видео: {encoder_display_name(encoder)}",
            f"  Режим качества: q:v {q} (из video_crf={crf})",
            f"  Preset: {preset} (не используется аппаратным кодировщиком)",
        ]


# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Movie-to-MKV mode helpers  (new; does NOT touch compress_video)
# ---------------------------------------------------------------------------


def probe_streams(source: Path) -> list[dict[str, object]]:
    """
    Run ffprobe on *source* and return a list of stream dicts.

    Each dict has at minimum:
      index, codec_type, codec_name
    and optionally:
      language, title, channels, width, height,
      disposition_default, disposition_forced

    Returns an empty list and prints an error if ffprobe fails.
    """
    if shutil.which("ffprobe") is None:
        print("Ошибка: ffprobe не найден. Установите ffmpeg: brew install ffmpeg",
              file=sys.stderr)
        return []
    cmd = [
        "ffprobe", "-v", "quiet",
        "-print_format", "json",
        "-show_streams",
        str(source),
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except Exception as exc:  # noqa: BLE001
        print(f"Ошибка ffprobe: {exc}", file=sys.stderr)
        return []
    if result.returncode != 0:
        print(f"Ошибка ffprobe (код {result.returncode}): {result.stderr.strip()}",
              file=sys.stderr)
        return []
    import json as _json
    try:
        data = _json.loads(result.stdout)
    except _json.JSONDecodeError as exc:
        print(f"Ошибка разбора ffprobe: {exc}", file=sys.stderr)
        return []
    streams = []
    for s in data.get("streams", []):
        tags = s.get("tags", {})
        # normalise tag keys to lowercase for cross-platform consistency
        tags_low = {k.lower(): v for k, v in tags.items()}
        entry: dict[str, object] = {
            "index": s.get("index", 0),
            "codec_type": s.get("codec_type", "unknown"),
            "codec_name": s.get("codec_name", "unknown"),
            "language": tags_low.get("language", ""),
            "title": tags_low.get("title", ""),
            "channels": s.get("channels", 0),
            "width": s.get("width", 0),
            "height": s.get("height", 0),
            "disposition_default": bool(s.get("disposition", {}).get("default", 0)),
            "disposition_forced": bool(s.get("disposition", {}).get("forced", 0)),
            # Colour tags: ffmpeg drops some of these when re-encoding unless
            # they are passed explicitly, which visibly washes out HDR sources.
            "color_primaries": s.get("color_primaries", ""),
            "color_transfer": s.get("color_transfer", ""),
            "color_space": s.get("color_space", ""),
        }
        streams.append(entry)
    return streams


def color_metadata_args(stream: dict[str, object]) -> list[str]:
    """
    Pass the source's colour tags through to the encoder.

    Only forwards values ffprobe actually reported; "unknown" and empty values
    are skipped so nothing is invented. HDR specifics (master-display,
    tone-mapping) are deliberately out of scope.
    """
    mapping = (
        ("color_primaries", "-color_primaries"),
        ("color_transfer", "-color_trc"),
        ("color_space", "-colorspace"),
    )
    args: list[str] = []
    for key, flag in mapping:
        value = str(stream.get(key, "") or "").strip()
        if value and value.lower() != "unknown":
            args.extend([flag, value])
    return args


def _stream_label(s: dict[str, object]) -> str:
    """
    Return a single human-readable line describing one stream,
    e.g.  "audio  #1: rus, ac3, 6ch, default"
    """
    idx = s["index"]
    ctype = s["codec_type"]
    codec = s["codec_name"]
    parts = []
    if ctype == "video":
        w, h = s.get("width", 0), s.get("height", 0)
        if w and h:
            parts.append(f"{w}x{h}")
    if s.get("language"):
        parts.append(str(s["language"]))
    parts.append(str(codec))
    if ctype == "audio" and s.get("channels"):
        parts.append(f"{s['channels']}ch")
    if s.get("title"):
        parts.append(f'"{s["title"]}"')
    if s.get("disposition_default"):
        parts.append("default")
    if s.get("disposition_forced"):
        parts.append("forced")
    return f"{ctype:<9} #{idx}: {', '.join(parts)}"


def movie_mkv_output_path(
    source: Path,
    crf: int,
    preset: str,
    encoder: str = "libx265",
    bitrate_kbps: int = 0,
) -> Path:
    """
    Generate a safe output path for the MKV movie mode.

    libx265 pattern:  <stem>_movie_x265_crf<crf>_<preset>.mkv  (frozen — files
                      users already have on disk are named this way)
    hardware pattern: <stem>_movie_hw_<family>_<N>k.mkv
    Adds _2, _3 … suffix if the candidate already exists.
    Does NOT create the file (that is left to reserve_destination).
    """
    if encoder != "libx265" and _candidate_by_name(encoder) is not None:
        marker = f"movie_hw_{encoder_family(encoder)}_{int(bitrate_kbps)}k"
    else:
        marker = f"movie_x265_crf{crf}_{preset}"
    candidate = source.with_name(f"{source.stem}_{marker}.mkv")
    counter = 2
    while candidate.exists():
        candidate = source.with_name(f"{source.stem}_{marker}_{counter}.mkv")
        counter += 1
    return candidate


def movie_mkv_copy_output_path(source: Path) -> Path:
    """
    Output path for the MKV movie mode WITHOUT video re-encoding (remux).
    Pattern: <stem>_remux.mkv, adds _2, _3 … if the candidate already exists.
    Does NOT create the file (that is left to reserve_destination).
    """
    candidate = source.with_name(f"{source.stem}_remux.mkv")
    counter = 2
    while candidate.exists():
        candidate = source.with_name(f"{source.stem}_remux_{counter}.mkv")
        counter += 1
    return candidate


def movie_mkv_folder_suffix(
    video_mode: str,
    crf: int,
    encoder: str = "libx265",
    bitrate_kbps: int = 0,
) -> str:
    """
    Suffix appended to a dragged folder's name to build the output folder name.

    Single source of truth: movie_mkv_folder_output_path() generates names with
    it and folder_is_previous_output() recognises them with it, so the two can
    never drift apart.

    The libx265 and copy forms are frozen: folders users already have on disk
    are named that way, and changing them would make the tool re-encode
    finished seasons. Hardware results get their own distinct form.
    """
    if video_mode == "copy":
        return "remux"
    if encoder != "libx265" and _candidate_by_name(encoder) is not None:
        return f"hw {encoder_family(encoder)} {int(bitrate_kbps)}k"
    return f"x265 crf{crf}"


def folder_is_previous_output(directory: Path) -> bool:
    """
    True if *directory* looks like an output folder this tool created earlier.

    Folder mode keeps the ORIGINAL file names inside "<Name> remux", so nothing
    in a file name marks it as ours — the marker lives on the folder. Matching
    is on the exact suffix, never a substring: a user folder called
    "Мои remux-эксперименты" must stay fully visible.
    """
    name = directory.name
    if name.endswith(" " + movie_mkv_folder_suffix("copy", 0)):
        return True
    if re.search(r" x265 crf\d+$", name) is not None:
        return True
    return re.search(r" hw (hevc|h264) \d+k$", name) is not None


def looks_like_previous_output(path: Path) -> bool:
    """
    True if *path* looks like a file this tool produced earlier.

    Older runs wrote their result next to the original, so a folder can hold
    both. Re-processing an already processed file is never what the user wants,
    so folder scanning skips these (they can still be selected by hand).
    Folder-mode results carry no marker in the file name, so the parent
    directory is checked too.
    """
    stem = path.stem
    if stem.endswith("_remux"):
        return True
    if re.search(r"_remux_\d+$", stem):
        return True
    if "_movie_x265_crf" in stem or "_compressed_crf" in stem:
        return True
    if "_movie_hw_" in stem:
        return True
    return folder_is_previous_output(path.parent)


def movie_mkv_folder_output_path(
    source: Path, source_dir: Path, video_mode: str, crf: int,
    encoder: str = "libx265", bitrate_kbps: int = 0,
) -> Path:
    """
    Output path for a file that came from a dragged FOLDER.

    The result goes into a NEW folder placed next to the dragged one, so the
    originals stay untouched and unmixed:
        .../Nip Tuck.S01          <- dragged, untouched
        .../Nip Tuck.S01 remux    <- created here
    Inside it each file keeps its original name. Adds _2, _3 … only if such a
    file somehow already exists. Does NOT create anything on disk (that is left
    to reserve_destination).
    """
    suffix = movie_mkv_folder_suffix(video_mode, crf, encoder, bitrate_kbps)
    out_dir = source_dir.parent / f"{source_dir.name} {suffix}"
    # Files pulled in from sub-folders keep their relative sub-path, so
    # "Season 1/Ep01.mkv" and "Season 2/Ep01.mkv" land in different folders
    # instead of fighting over one name.
    try:
        relative_parent = source.parent.relative_to(source_dir)
    except ValueError:
        relative_parent = Path(".")
    target_dir = out_dir / relative_parent
    candidate = target_dir / f"{source.stem}.mkv"
    counter = 2
    while candidate.exists():
        candidate = target_dir / f"{source.stem}_{counter}.mkv"
        counter += 1
    return candidate


def movie_quality_display(
    encoder: str,
    video_mode: str,
    crf: int,
    preset: str,
    bitrate_kbps: int,
    est_size_bytes: int = 0,
) -> list[str]:
    """
    Describe the chosen video mode for the movie-mode confirmation screen.

    Separate from quality_mode_display(), which serves the batch/quick paths and
    is built around CRF/q:v. This one has to cover the remux case and the
    hardware case, and it states the quality trade-off plainly: a user picking
    "fast" deserves to know what it costs before hours of work start.
    """
    if video_mode == "copy":
        return [
            "  Видео       : copy (без сжатия, без потерь)",
            "  Время       : ремукс без перекодирования: обычно меньше минуты.",
        ]

    if encoder == "libx265" or _candidate_by_name(encoder) is None:
        return [
            "  Кодек видео : libx265 (процессор)",
            f"  CRF         : {crf}",
            f"  Preset      : {preset}",
            "  Время       : перекодирование libx265 идёт долго: "
            "ориентировочно несколько часов на фильм.",
        ]

    lines = [
        f"  Кодек видео : {encoder_display_name(encoder)}",
        f"  Качество    : целевой битрейт {bitrate_kbps} кбит/с",
    ]
    if est_size_bytes > 0:
        lines.append(
            f"  Ожидаемый размер: примерно {format_bytes(est_size_bytes)} на файл (оценка)"
        )
    lines.append("  Время       : примерно в 3 раза быстрее libx265.")
    lines.append(
        "  Честно      : при одинаковом размере файла аппаратный кодек даёт"
    )
    lines.append(
        "                качество заметно ниже, чем libx265. Если качество"
    )
    lines.append(
        "                важнее времени, выберите libx265."
    )
    lines.extend(unverified_encoder_warning(encoder))
    return lines


def ask_hw_bitrate(
    probed: list[tuple[Path, tuple[list, list, list, list]]],
    encoder: str,
) -> int:
    """
    Ask how hard to compress when a hardware encoder was chosen.

    Hardware encoders have no CRF, so quality is set by a target bitrate. The
    bitrate is derived from the FIRST file's resolution/fps/source bitrate, and
    the resulting size is shown up front — a number of kbit/s means nothing to
    most people, an estimated size per file does.
    """
    source = probed[0][0]
    video_index = probed[0][1][1][0]["index"]
    info = video_bitrate_info(source, video_index)

    levels = [
        ("1", "high", "Максимальное качество"),
        ("2", "balanced", "Баланс — примерно как libx265 CRF 22 по размеру"),
        ("3", "compact", "Компактно — заметно меньше, качество ниже"),
    ]
    print("\nНасколько сильно сжать? Аппаратный кодек задаёт качество битрейтом.")
    for key, level, label in levels:
        kbps = target_bitrate_kbps(
            info["width"], info["height"], info["fps"],
            info["source_bitrate_bps"], level,
        )
        size = estimated_output_bytes(kbps, info["duration"])
        mark = " [по умолчанию]" if level == "balanced" else ""
        print(f"  {key}. {label}: ~{kbps} кбит/с, примерно {format_bytes(size)} на файл{mark}")
    print("  4. Ввести битрейт вручную (кбит/с)")

    choice = prompt_menu("Выберите 1-4 [Enter = 2]: ", set("1234"), "2")
    if choice == "4":
        kbps = prompt_number("Битрейт (500-50000 кбит/с): ", 500, 50000)
    else:
        level = {"1": "high", "2": "balanced", "3": "compact"}[choice]
        kbps = target_bitrate_kbps(
            info["width"], info["height"], info["fps"],
            info["source_bitrate_bps"], level,
        )

    for line in bitrate_advice_lines(kbps, info):
        print(line)
    return kbps


def build_movie_video_args(
    video_mode: str,
    encoder: str,
    crf: int,
    preset: str,
    bitrate_kbps: int,
) -> list[str]:
    """
    Build just the video part of the movie-mode ffmpeg command.

    Kept separate from compress_movie_with_stream_selection so the branch can be
    checked without running ffmpeg over a multi-gigabyte file.

    copy     → -c:v copy (remux, no re-encode at all).
    libx265  → -crf / -preset, exactly as before.
    hardware → -b:v with -maxrate / -bufsize. Hardware encoders have no CRF: q:v
               does not pin quality or size, so a target bitrate is the only way
               to get a predictable result. -crf and -preset are NOT passed.
    An unknown encoder name falls back to libx265.
    """
    if video_mode == "copy":
        # Remux: keep the original video stream untouched (no quality loss, fast).
        return ["-c:v", "copy"]

    if encoder == "libx265" or not encoder:
        return ["-c:v", "libx265", "-crf", str(crf), "-preset", preset]

    if _candidate_by_name(encoder) is None:
        # Unknown encoder name: conservative fallback rather than a failed run.
        return ["-c:v", "libx265", "-crf", str(crf), "-preset", preset]

    target = max(1, int(bitrate_kbps))
    return [
        "-c:v", encoder,
        "-b:v", f"{target}k",
        "-maxrate", f"{int(target * 1.5)}k",
        "-bufsize", f"{target * 3}k",
    ]


def compress_movie_with_stream_selection(
    source: Path,
    destination: Path,
    video_stream_index: int,
    audio_stream_indices: list[int],
    subtitle_stream_indices: list[int],
    crf: int,
    preset: str,
    audio_mode: str,          # "copy" | "aac" | "opus"
    subtitle_mode: str,       # "copy" | "none"
    show_progress: bool,
    video_mode: str = "encode",  # "encode" (re-encode) | "copy" (remux, no re-encode)
    video_encoder: str = "libx265",
    video_bitrate_kbps: int = 0,
    color_args: list[str] | None = None,
) -> tuple[int, str]:
    """
    Compress a single movie file into MKV using explicit stream selection.

    This function is intentionally separate from compress_video().
    It does NOT use -movflags +faststart or -tag:v hvc1 (MKV doesn’t need them).
    With video_mode="encode" it uses libx265 with -crf / -preset; with
    video_mode="copy" it copies the video stream unchanged (lossless remux).

    Returns (output_size_bytes, note_string).
    Raises RuntimeError on ffmpeg failure.
    Uses the same temp-file pattern as compress_video for safety.
    """
    with tempfile.NamedTemporaryFile(
        prefix=f".{destination.stem}.",
        suffix=".mkv",
        dir=destination.parent,
        delete=False,
    ) as temp:
        temp_path = Path(temp.name)
    temp_path.unlink(missing_ok=True)  # ffmpeg will create it

    cmd = ["ffmpeg"]
    if not show_progress:
        cmd.extend(["-hide_banner", "-loglevel", "error"])
    cmd.extend(["-nostdin", "-y", "-i", str(source), "-map_metadata", "0"])

    # --- explicit stream mapping ---
    cmd.extend(["-map", f"0:{video_stream_index}"])
    for ai in audio_stream_indices:
        cmd.extend(["-map", f"0:{ai}"])
    if subtitle_mode != "none":
        for si in subtitle_stream_indices:
            cmd.extend(["-map", f"0:{si}"])

    # --- video codec ---
    cmd.extend(
        build_movie_video_args(video_mode, video_encoder, crf, preset, video_bitrate_kbps)
    )
    if video_mode != "copy" and color_args:
        # Re-encoding drops some colour tags unless they are restated.
        cmd.extend(color_args)

    # --- audio codec ---
    if audio_mode == "copy":
        cmd.extend(["-c:a", "copy"])
    elif audio_mode == "aac":
        cmd.extend(["-c:a", "aac", "-b:a", "192k"])
    elif audio_mode == "opus":
        cmd.extend(["-c:a", "libopus", "-b:a", "128k"])
    else:
        cmd.extend(["-c:a", "copy"])  # safe fallback

    # --- subtitles ---
    if subtitle_mode == "none" or not subtitle_stream_indices:
        pass  # no subtitle streams mapped above
    else:
        cmd.extend(["-c:s", "copy"])

    cmd.append(str(temp_path))

    try:
        if show_progress:
            result = subprocess.run(cmd)
        else:
            result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            stderr = result.stderr.strip() if result.stderr else ""
            raise RuntimeError(stderr or f"ffmpeg завершился с кодом {result.returncode}")
        shutil.copystat(source, temp_path)
        install_temp_file(temp_path, destination)
        if video_mode == "copy":
            note = "Фильм пересобран в MKV без сжатия видео"
        elif video_encoder != "libx265" and _candidate_by_name(video_encoder) is not None:
            note = "Фильм сжат в MKV (аппаратный кодек)"
        else:
            note = "Фильм сжат в MKV"
        return destination.stat().st_size, note
    finally:
        temp_path.unlink(missing_ok=True)


def _parse_index_list(
    raw: str, valid_indices: list[int]
) -> list[int] | None:
    """
    Parse a comma/space-separated string of stream indices.
    Returns sorted list of valid unique indices, or None on parse error.
    """
    if not raw.strip():
        return []
    result = []
    for token in raw.replace(",", " ").split():
        try:
            val = int(token)
        except ValueError:
            return None
        if val not in valid_indices:
            return None
        if val not in result:
            result.append(val)
    return sorted(result)


def _stream_signature(streams: list[dict[str, object]]) -> tuple:
    """
    Compact fingerprint of a file's track layout, used to detect whether
    several files (e.g. episodes of one series) share the SAME tracks.

    Compares index, codec type, codec name, language and channel count of
    every stream. Deliberately ignores per-episode-varying fields such as the
    stream title, so identically structured episodes compare equal.
    """
    return tuple(
        (
            s.get("index", 0),
            s.get("codec_type", ""),
            s.get("codec_name", ""),
            str(s.get("language", "")),
            s.get("channels", 0),
        )
        for s in streams
    )


def _probe_movie_streams(
    source: Path,
) -> tuple[list[dict], list[dict], list[dict], list[dict]] | None:
    """
    Probe and validate one source file.

    Returns (streams, video_streams, audio_streams, subtitle_streams) or None
    (with an error printed) if probing fails or there is no video/audio.
    """
    print(f"\nАнализирую потоки: {source.name} …")
    streams = probe_streams(source)
    if not streams:
        print(
            f"Ошибка ({source.name}): не удалось получить информацию о потоках.",
            file=sys.stderr,
        )
        return None

    video_streams   = [s for s in streams if s["codec_type"] == "video"]
    audio_streams   = [s for s in streams if s["codec_type"] == "audio"]
    subtitle_streams = [s for s in streams if s["codec_type"] == "subtitle"]

    if not video_streams:
        print(f"Ошибка ({source.name}): видеопотоков не найдено.", file=sys.stderr)
        return None
    if not audio_streams:
        print(f"Ошибка ({source.name}): аудиопотоков не найдено.", file=sys.stderr)
        return None

    return streams, video_streams, audio_streams, subtitle_streams


def _choose_video_audio(
    streams: list[dict],
    video_streams: list[dict],
    audio_streams: list[dict],
) -> tuple[int, list[int]]:
    """
    Interactive video + audio selection from already-probed streams.

    Returns (video_stream_index, audio_stream_indices).
    """
    print("\nНайденные потоки:")
    for s in streams:
        print(f"  {_stream_label(s)}")

    # --- choose video stream ---
    v_indices = [s["index"] for s in video_streams]
    default_v = v_indices[0]
    if len(video_streams) == 1:
        video_stream_index = default_v
        print(f"\nВидео: автоматически выбрана единственная видеодорожка #{default_v}.")
    else:
        print("\nВидеодорожки:")
        for s in video_streams:
            print(f"  {_stream_label(s)}")
        while True:
            raw = input(f"Выберите одну видеодорожку [Enter = #{default_v}]: ").strip()
            if not raw:
                video_stream_index = default_v
                break
            try:
                val = int(raw)
            except ValueError:
                print("Введите целое число.")
                continue
            if val not in v_indices:
                print(f"Нет видеопотока #{val}.")
                continue
            video_stream_index = val
            break

    # --- choose audio streams ---
    a_indices = [s["index"] for s in audio_streams]
    # default: streams with disposition_default; if none, take first
    default_audio = [s["index"] for s in audio_streams if s.get("disposition_default")]
    if not default_audio:
        default_audio = [a_indices[0]]
    default_audio_str = ",".join(str(i) for i in default_audio)
    print("\nАудиодорожки:")
    for s in audio_streams:
        print(f"  {_stream_label(s)}")
    while True:
        raw = input(
            f"Выберите индексы через запятую/пробел [Enter = {default_audio_str}]: "
        ).strip()
        if not raw:
            audio_stream_indices = default_audio
            break
        parsed = _parse_index_list(raw, a_indices)
        if parsed is None or not parsed:
            print(f"Неверный ввод. Доступные индексы: {', '.join(str(i) for i in a_indices)}")
            continue
        audio_stream_indices = parsed
        break

    return video_stream_index, audio_stream_indices


def _choose_subtitles(
    subtitle_streams: list[dict],
) -> tuple[list[int], str]:
    """
    Interactive subtitle selection from already-probed subtitle streams.

    Returns (subtitle_stream_indices, subtitle_mode). subtitle_mode is "copy"
    when at least one subtitle is selected, otherwise "none".
    """
    if not subtitle_streams:
        print("\nСубтитры: не найдены в файле, пропускаю.")
        return [], "none"

    s_indices = [s["index"] for s in subtitle_streams]
    print("\nСубтитры:")
    for s in subtitle_streams:
        print(f"  {_stream_label(s)}")
    print("Введите индексы через запятую/пробел, или Enter — без субтитров:")
    raw = input("Субтитры [Enter = не включать]: ").strip()
    if not raw:
        return [], "none"
    parsed_s = _parse_index_list(raw, s_indices)
    if parsed_s is None:
        print("Неправильный ввод, субтитры не включены.")
        return [], "none"
    return parsed_s, ("copy" if parsed_s else "none")


def _choose_movie_streams(
    streams: list[dict],
    video_streams: list[dict],
    audio_streams: list[dict],
    subtitle_streams: list[dict],
) -> dict[str, object]:
    """
    Interactive video / audio / subtitle selection from already-probed streams.

    Returns a dict with keys video_stream_index, audio_stream_indices,
    subtitle_stream_indices and subtitle_mode.
    """
    video_stream_index, audio_stream_indices = _choose_video_audio(
        streams, video_streams, audio_streams
    )
    subtitle_stream_indices, subtitle_mode = _choose_subtitles(subtitle_streams)
    return {
        "video_stream_index": video_stream_index,
        "audio_stream_indices": audio_stream_indices,
        "subtitle_stream_indices": subtitle_stream_indices,
        "subtitle_mode": subtitle_mode,
    }


def _select_movie_streams(source: Path) -> dict[str, object] | None:
    """Probe one file and run interactive stream selection (None on failure)."""
    probed = _probe_movie_streams(source)
    if probed is None:
        return None
    return _choose_movie_streams(*probed)


def run_wizard_movie_mkv(
    args: argparse.Namespace,
) -> argparse.Namespace | None:
    """
    Wizard branch: remux/compress one or MORE movies into MKV with stream
    selection.

    Several files can be dragged at once (e.g. episodes of a series). When the
    video+audio layouts match across all files, video/audio selection is asked
    once and applied to every file; subtitles are asked once if identical, else
    per file. Compression settings (video quality / preset / audio mode) are
    always asked once and applied to all files.

    Sets args.movie_mkv to a LIST of per-file job dicts so main() can dispatch
    to the right execution path.
    """
    print("\nРежим: пересобрать фильмы/сериалы в MKV с выбором дорожек")

    def _is_video(path: Path) -> bool:
        return (
            path.is_file()
            and not path.is_symlink()
            and path.suffix.lower() in VIDEO_EXTENSIONS
        )

    def _videos_directly_in(directory: Path) -> list[Path]:
        return sorted(path for path in directory.iterdir() if _is_video(path))

    def _videos_in_subfolders(directory: Path) -> list[Path]:
        """Videos below *directory* but NOT at its top level (one full walk)."""
        found: list[Path] = []
        for root, dir_names, file_names in os.walk(directory, followlinks=False):
            root_path = Path(root)
            if root_path == directory:
                continue
            dir_names.sort()
            for name in sorted(file_names):
                path = root_path / name
                if _is_video(path) and not looks_like_previous_output(path):
                    found.append(path)
        return found

    def _report_skipped(skipped: list[Path]) -> None:
        """Never skip silently — a stranger's Movie_remux.mkv looks the same."""
        if not skipped:
            return
        print(f"  Пропущено файлов — {len(skipped)} (похоже на результат прошлого запуска):")
        for path in skipped:
            print(f"    - {path.name}")
        print(
            "  Если это ваши файлы, перетащите их по отдельности — "
            "они будут добавлены."
        )

    # --- step 1: choose source file(s) ---
    sources: list[Path] = []
    # Maps a source file to the folder it was dragged in as part of, if any.
    source_folder: dict[Path, Path] = {}

    def _add_one(resolved: Path) -> int:
        """Append one validated file if it is not already selected."""
        if resolved in sources:
            print(f"Файл уже добавлен, пропускаю: {resolved.name}")
            return 0
        sources.append(resolved)
        return 1

    def _add_candidates(line: str) -> int:
        """
        Validate every path parsed from *line*, append new ones.

        A dragged FOLDER is expanded to the video files directly inside it.
        That matters because a terminal accepts at most MAX_CANON (1024 bytes
        on macOS) per input line, so dragging a whole season at once silently
        stops at about nine long paths. One folder path is ~60 bytes no matter
        how many episodes it holds.
        """
        added = 0
        for candidate in clean_terminal_paths(line):
            resolved = candidate.resolve()
            if resolved.is_dir():
                found = _videos_directly_in(resolved)
                videos = [path for path in found if not looks_like_previous_output(path)]
                skipped = [path for path in found if looks_like_previous_output(path)]
                if not videos:
                    print(f"В папке нет видеофайлов: {resolved}")
                    _report_skipped(skipped)
                    # Nothing at the top level — the season may be split into
                    # per-season sub-folders, so offer to look one level deeper.
                    nested = _videos_in_subfolders(resolved)
                    if not nested:
                        continue
                    sub_dirs = sorted({path.parent for path in nested})
                    print(
                        f"Зато видео есть во вложенных папках ({len(nested)} шт. "
                        f"в {len(sub_dirs)}):"
                    )
                    for sub in sub_dirs:
                        print(f"  - {sub.relative_to(resolved)}")
                    if not prompt_yes_no(
                        "Искать видео и во вложенных папках? yes/no [no]: ", "no"
                    ):
                        print(
                            "Вложенные папки пропущены. Перетащите нужную "
                            "подпапку отдельно, если хотите обработать именно её."
                        )
                        continue
                    videos = nested
                    print(f"Папка «{resolved.name}»: добавлено из вложенных папок — {len(videos)}")
                else:
                    print(f"Папка «{resolved.name}»: найдено видеофайлов — {len(videos)}")
                    _report_skipped(skipped)
                for video in videos:
                    if _add_one(video):
                        # Remember the dragged folder so the result can be written
                        # to a new folder NEXT TO it instead of among the originals.
                        source_folder[video] = resolved
                        added += 1
                continue
            if not (resolved.is_file() and not resolved.is_symlink()):
                print(f"Файл не найден или это не обычный файл: {resolved}")
                continue
            added += _add_one(resolved)
        return added

    print(
        "\nСовет: для целого сезона перетащите ПАПКУ — будут добавлены видео, "
        "лежащие непосредственно в ней. Если там их нет, а есть во вложенных "
        "папках, скрипт спросит, искать ли в них. Терминал принимает не больше "
        "1024 символов в строке, поэтому перетащить разом много файлов "
        "с длинными путями не получится."
    )
    while True:
        file_value = input(
            "\nПеретащите папку или видеофайлы в Terminal и нажмите Enter: "
        )
        if not file_value.strip():
            print("Укажите путь хотя бы к одному файлу или папке.")
            continue
        if _add_candidates(file_value) > 0:
            break
        print("Ни одного подходящего файла не распознано, попробуйте ещё раз.")

    # optionally add MORE files (Enter to finish -> backward compatible)
    while True:
        print(f"\nВыбрано файлов: {len(sources)}")
        for i, src in enumerate(sources, start=1):
            print(f"  {i}. {src.name}")
        file_value = input(
            "Добавить ещё? Перетащите папку или файлы, либо нажмите Enter, чтобы продолжить: "
        )
        if not file_value.strip():
            break
        _add_candidates(file_value)

    # --- step 2: probe every file first, then choose streams ---
    probed: list[tuple[Path, tuple[list, list, list, list]]] = []
    skipped_sources: list[Path] = []
    for source in sources:
        result = _probe_movie_streams(source)
        if result is None:
            # A silent movie (no audio track) or an unreadable file must not
            # throw away the answers already given for the other files.
            print(f"Файл пропущен: {source.name}")
            skipped_sources.append(source)
            continue
        probed.append((source, result))

    if not probed:
        print("Ни один файл не удалось проанализировать. Запуск отменён.")
        return None
    if skipped_sources:
        print(f"\nПропущено файлов: {len(skipped_sources)}")
        for skipped in skipped_sources:
            print(f"  - {skipped.name}")

    # For episodes of one series the video/audio tracks are usually identical
    # while subtitle tracks may vary slightly. So compare video+audio and
    # subtitles SEPARATELY: if video+audio match across all files, ask for them
    # ONCE; ask subtitles once if they also match, otherwise per file.
    def _av_signature(streams: list[dict]) -> tuple:
        return _stream_signature(
            [s for s in streams if s["codec_type"] in ("video", "audio")]
        )

    def _sub_signature(streams: list[dict]) -> tuple:
        return _stream_signature(
            [s for s in streams if s["codec_type"] == "subtitle"]
        )

    multi = len(probed) > 1
    av_same = multi and len({_av_signature(res[0]) for _, res in probed}) == 1
    sub_same = multi and len({_sub_signature(res[0]) for _, res in probed}) == 1

    per_file: list[dict[str, object]] = []
    if multi and av_same:
        print(
            f"\nВидео и аудиодорожки во всех {len(probed)} файлах одинаковые — "
            "выберу их один раз и применю ко всем."
        )
        streams0, v0, a0, s0 = probed[0][1]
        video_index, audio_indices = _choose_video_audio(streams0, v0, a0)

        if sub_same:
            sub_indices, sub_mode = _choose_subtitles(s0)
            for source, _ in probed:
                per_file.append({
                    "video_stream_index": video_index,
                    "audio_stream_indices": list(audio_indices),
                    "subtitle_stream_indices": list(sub_indices),
                    "subtitle_mode": sub_mode,
                    "source": source,
                })
        else:
            print(
                "\nСубтитры в файлах различаются — выберу субтитры для каждого файла "
                "(аудио и видео уже заданы)."
            )
            for idx, (source, (streams, v, a, s)) in enumerate(probed, start=1):
                print(f"\n=== Субтитры для файла {idx} из {len(probed)}: {source.name} ===")
                sub_indices, sub_mode = _choose_subtitles(s)
                per_file.append({
                    "video_stream_index": video_index,
                    "audio_stream_indices": list(audio_indices),
                    "subtitle_stream_indices": sub_indices,
                    "subtitle_mode": sub_mode,
                    "source": source,
                })
    else:
        if multi:
            print(
                "\nДорожки в файлах сильно различаются — выбор нужно сделать для каждого файла."
            )
        for idx, (source, result) in enumerate(probed, start=1):
            if multi:
                print(f"\n=== Файл {idx} из {len(probed)}: {source.name} ===")
            selection = _choose_movie_streams(*result)
            selection["source"] = source
            per_file.append(selection)

    # Colour tags of each file's chosen video stream, so re-encoding can restate
    # them instead of silently dropping them. Taken from the probe already done.
    streams_by_source = {source: result[0] for source, result in probed}
    for sel in per_file:
        chosen = next(
            (
                s for s in streams_by_source.get(sel["source"], [])
                if s["index"] == sel["video_stream_index"]
            ),
            None,
        )
        sel["color_args"] = color_metadata_args(chosen) if chosen else []

    # --- step 6: video quality preset (asked ONCE for all files) ---
    # The hardware option is only offered when a hardware encoder actually
    # passed a real test run on this machine; otherwise the item is not printed
    # at all, so nobody picks something that then has to be refused.
    hw_encoder = select_best_video_encoder(
        disable_hw=getattr(args, "disable_hw_video", False)
    )
    hw_available = hw_encoder != "libx265"

    video_encoder = "libx265"
    video_bitrate_kbps = 0

    print("\nСжатие видео для фильма:")
    print("  1. БЕЗ СЖАТИЯ — copy, оставить видео как есть (быстро, без потерь) [по умолчанию]")
    print("  2. Качество: libx265, CRF 20 (очень высокое, медленно)")
    print("  3. Качество: libx265, CRF 22 (баланс, медленно)")
    print("  4. Качество: libx265, CRF 24 (компактнее, медленно)")
    if hw_available:
        print(
            f"  5. БЫСТРО: аппаратный кодек {encoder_display_name(hw_encoder)} — "
            "примерно в 3 раза быстрее, качество чуть ниже"
        )
        print("  6. Ввести CRF вручную (libx265)")
        allowed, manual_choice = set("123456"), "6"
        prompt_text = "Выберите 1-6 [Enter = 1]: "
    else:
        print("  5. Ввести CRF вручную (libx265)")
        allowed, manual_choice = set("12345"), "5"
        prompt_text = "Выберите 1-5 [Enter = 1]: "
    q_choice = prompt_menu(prompt_text, allowed, "1")

    if q_choice == "1":
        video_mode = "copy"
        crf = 0
        # Unused when video_mode == "copy" (no -preset is passed to ffmpeg),
        # but it still travels into the job dict, so keep it a harmless string.
        preset = "copy"
    elif hw_available and q_choice == "5":
        video_mode = "encode"
        video_encoder = hw_encoder
        crf = 0
        # Hardware encoders ignore -preset entirely, so step 7 is skipped and
        # this only travels into the job dict as a label.
        preset = "hw"
        video_bitrate_kbps = ask_hw_bitrate(probed, hw_encoder)
    else:
        video_mode = "encode"
        crf_map = {"2": 20, "3": 22, "4": 24}
        if q_choice == manual_choice:
            crf = prompt_number("CRF (18-28): ", 18, 28)
        else:
            crf = crf_map[q_choice]

    # --- step 7: preset (only when re-encoding) ---
    if video_mode == "encode":
        print("\nPreset скорости кодирования:")
        print("  1. fast (по умолчанию)")
        print("  2. medium")
        print("  3. slow")
        p_choice = prompt_menu("Выберите 1-3 [Enter = 1]: ", set("123"), "1")
        preset = {"1": "fast", "2": "medium", "3": "slow"}[p_choice]

    # --- step 8: audio mode ---
    print("\nРежим аудио:")
    print("  1. copy — оставить аудио без перекодирования (по умолчанию)")
    print("  2. aac — перекодировать в AAC 192k")
    print("  3. opus — перекодировать в Opus 128k (меньше вес)")
    a_choice = prompt_menu("Выберите 1-3 [Enter = 1]: ", set("123"), "1")
    audio_mode = {"1": "copy", "2": "aac", "3": "opus"}[a_choice]

    # --- step 9: output path (per file) ---
    # Files dragged in as a folder go to a new folder next to it; individually
    # dragged files keep landing next to the original, as before.
    for sel in per_file:
        src = sel["source"]
        src_dir = source_folder.get(src)
        if src_dir is not None:
            sel["output_path"] = movie_mkv_folder_output_path(
                src, src_dir, video_mode, crf, video_encoder, video_bitrate_kbps
            )
        elif video_mode == "copy":
            sel["output_path"] = movie_mkv_copy_output_path(src)
        else:
            sel["output_path"] = movie_mkv_output_path(
                src, crf, preset, video_encoder, video_bitrate_kbps
            )

    # --- step 10: summary + confirmation ---
    audio_label = {"copy": "copy (без перекодирования)",
                   "aac": "aac 192k",
                   "opus": "libopus 128k"}[audio_mode]

    print("\nИтоговые настройки:")
    print("  Контейнер   : MKV")
    est_bytes = 0
    if video_mode != "copy" and video_encoder != "libx265":
        first = per_file[0]["source"]
        est_bytes = estimated_output_bytes(
            video_bitrate_kbps,
            float(video_bitrate_info(first, per_file[0]["video_stream_index"])["duration"]),
        )
    for line in movie_quality_display(
        video_encoder, video_mode, crf, preset, video_bitrate_kbps, est_bytes
    ):
        print(line)
    print(f"  Аудио       : {audio_label}")
    # When everything lands in one new folder, state it once instead of making
    # the user read the same directory on every line below.
    out_dirs = {sel["output_path"].parent for sel in per_file}
    if len(out_dirs) == 1 and len(per_file) > 1:
        print(f"  Папка результата: {out_dirs.pop()}")
    print("  Оригиналы не будут изменены.")

    for idx, sel in enumerate(per_file, start=1):
        subtitle_stream_indices = sel["subtitle_stream_indices"]
        subs_label = (
            ", ".join(f"#{i}" for i in subtitle_stream_indices)
            if subtitle_stream_indices else "не включены"
        )
        audio_streams_label = ", ".join(
            f"#{i}" for i in sel["audio_stream_indices"]
        )
        print(f"\n  Файл {idx} из {len(per_file)}:")
        print(f"    Исходный файл : {sel['source']}")
        print(f"    Выходной файл : {sel['output_path']}")
        print(f"    Видеодорожка: #{sel['video_stream_index']}")
        print(f"    Аудиодорожки: {audio_streams_label}")
        print(f"    Субтитры   : {subs_label}")

    confirmation = input("\nНапишите YES MOVIE, чтобы начать: ").strip()
    if confirmation != "YES MOVIE":
        print("Запуск отменён. Ничего не изменено.")
        return None

    # --- pack everything into args for main() dispatch (LIST of jobs) ---
    args.movie_mkv = [
        {
            "source": sel["source"],
            "output_path": sel["output_path"],
            "video_stream_index": sel["video_stream_index"],
            "audio_stream_indices": sel["audio_stream_indices"],
            "subtitle_stream_indices": sel["subtitle_stream_indices"],
            "video_mode": video_mode,
            "video_encoder": video_encoder,
            "color_args": sel.get("color_args", []),
            "video_bitrate_kbps": video_bitrate_kbps,
            "crf": crf,
            "preset": preset,
            "audio_mode": audio_mode,
            "subtitle_mode": sel["subtitle_mode"],
        }
        for sel in per_file
    ]
    return args


# ---------------------------------------------------------------------------


def compress_video(
    source: Path, destination: Path, crf: int, preset: str,
    show_progress: bool, fallback_to_original: bool = True,
    encoder: str = "libx265", qp: int = 0,
) -> tuple[int, str]:
    with tempfile.NamedTemporaryFile(
        prefix=f".{destination.stem}.", suffix=".mp4", dir=destination.parent, delete=False
    ) as temp:
        temp_path = Path(temp.name)
    temp_path.unlink(missing_ok=True)  # ffmpeg creates the file itself.
    command = build_video_command(source, temp_path, crf, preset, show_progress, encoder, qp=qp)
    try:
        if show_progress:
            result = subprocess.run(command)
        else:
            result = subprocess.run(command, capture_output=True, text=True)
        if result.returncode != 0:
            stderr = result.stderr.strip() if result.stderr else ""
            message = stderr or f"ffmpeg завершился с кодом {result.returncode}"
            raise RuntimeError(message)
        original_size = source.stat().st_size
        compressed_size = temp_path.stat().st_size
        if compressed_size >= original_size and fallback_to_original:
            temp_path.unlink(missing_ok=True)
            output_size = copy_safely(source, destination)
            return output_size, "Сжатая версия была больше оригинала; скопирован оригинал"
        shutil.copystat(source, temp_path)
        install_temp_file(temp_path, destination)
        if compressed_size >= original_size:
            return compressed_size, "Сжатый файл больше оригинала; оригинал не изменён"
        return compressed_size, "Видео сжато"
    finally:
        temp_path.unlink(missing_ok=True)


def planned_action(category: str) -> str:
    return {
        "video": "compress-video",
        "jpeg": "compress-jpeg",
        "image-copy": "copy-image-unchanged",
        "raw-copy": "copy-raw-unchanged",
        "file-copy": "copy-unchanged",
    }[category]


def make_summary_row(
    source: Path, destination: Path, input_dir: Path, output_dir: Path,
    category: str, action: str, status: str, original_size: int,
    output_size: int | str, note: str,
) -> dict[str, object]:
    saved = original_size - output_size if isinstance(output_size, int) else ""
    return {
        "source": str(source.relative_to(input_dir)),
        "destination": str(destination.relative_to(output_dir)),
        "category": category,
        "action": action,
        "status": status,
        "original_bytes": original_size,
        "output_bytes": output_size,
        "saved_bytes": saved,
        "note": note,
    }


def write_reports(
    output_dir: Path, summary_rows: list[dict[str, object]],
    duplicate_rows: list[dict[str, object]], errors: list[str],
) -> None:
    reports_dir = output_dir / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    with (reports_dir / "summary.csv").open("x", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=SUMMARY_FIELDS)
        writer.writeheader()
        writer.writerows(summary_rows)
    with (reports_dir / "duplicates_report.csv").open(
        "x", newline="", encoding="utf-8-sig"
    ) as handle:
        writer = csv.DictWriter(handle, fieldnames=["group", "sha256", "size_bytes", "path"])
        writer.writeheader()
        writer.writerows(duplicate_rows)
    with (reports_dir / "errors.log").open("x", encoding="utf-8") as handle:
        for error in errors:
            handle.write(error + "\n")


def format_bytes(value: int) -> str:
    amount = float(abs(value))
    units = ["Б", "КБ", "МБ", "ГБ", "ТБ"]
    unit = units[0]
    for unit in units:
        if amount < 1024 or unit == units[-1]:
            break
        amount /= 1024
    sign = "-" if value < 0 else ""
    return f"{sign}{amount:.1f} {unit}"


def print_totals(counters: Counters, dry_run: bool) -> None:
    verb = "будет обработано" if dry_run else "обработано"
    print("\nИтог:")
    print(f"  Файлов найдено: {counters.total}")
    print(f"  Видео {verb}: {counters.videos}")
    print(f"  Фото {verb}: {counters.photos}")
    print(f"  Остальных файлов {'будет скопировано' if dry_run else 'скопировано'}: {counters.copied}")
    print(f"  Точных дубликатов (лишних копий): {counters.duplicates}")
    print(f"  Ошибок: {counters.errors}")
    if dry_run:
        print("  Экономия места: определяется только при реальной обработке")
    else:
        print(f"  Примерная экономия места: {format_bytes(counters.original_bytes - counters.output_bytes)}")


def main() -> int:
    args = parse_args()
    if getattr(args, "list_encoders", False):
        return print_encoder_table(force_refresh=args.refresh_hw_cache)
    if args.wizard_cancelled:
        return 0
    # --- NEW: movie-to-MKV mode (set by run_wizard_movie_mkv) ---
    if args.movie_mkv is not None:
        if shutil.which("ffmpeg") is None:
            print("Ошибка: ffmpeg не найден. Установите: brew install ffmpeg",
                  file=sys.stderr)
            return 2
        jobs = args.movie_mkv
        total = len(jobs)
        failures = 0
        for job_idx, m in enumerate(jobs, start=1):
            source: Path = m["source"]
            output_path: Path = m["output_path"]
            # Reserving and sizing can both fail (source deleted mid-run, output
            # folder not writable). Neither may kill the whole batch, and a
            # reservation we already made must not be left behind as a 0-byte file.
            reserved = False
            try:
                if not reserve_destination(output_path):
                    # highly unlikely (the output-path helpers already avoid existing
                    # files), but be safe — derive fresh candidates from the base name.
                    base_stem = output_path.stem
                    counter = 2
                    while True:
                        candidate = output_path.with_name(f"{base_stem}_{counter}.mkv")
                        if reserve_destination(candidate):
                            output_path = candidate
                            break
                        counter += 1
                # Set only AFTER a successful reservation: a failure before this
                # point must never unlink a path we do not own.
                reserved = True
                original_size = source.stat().st_size
            except OSError as exc:
                if reserved:
                    output_path.unlink(missing_ok=True)
                print(f"Ошибка ({source.name}): {exc}", file=sys.stderr)
                failures += 1
                continue
            if total > 1:
                print(f"\n[{job_idx}/{total}] Обрабатываю: {source.name} …")
            else:
                print(f"\nОбрабатываю: {source.name} …")
            try:
                out_size, note = compress_movie_with_stream_selection(
                    source=source,
                    destination=output_path,
                    video_stream_index=m["video_stream_index"],
                    audio_stream_indices=m["audio_stream_indices"],
                    subtitle_stream_indices=m["subtitle_stream_indices"],
                    crf=m["crf"],
                    preset=m["preset"],
                    audio_mode=m["audio_mode"],
                    subtitle_mode=m["subtitle_mode"],
                    show_progress=True,
                    video_mode=m.get("video_mode", "encode"),
                    video_encoder=m.get("video_encoder", "libx265"),
                    video_bitrate_kbps=m.get("video_bitrate_kbps", 0),
                    color_args=m.get("color_args", []),
                )
                print(f"Оригинал : {format_bytes(original_size)}")
                print(f"Выходной файл: {format_bytes(out_size)}")
                print(f"Экономия  : {format_bytes(original_size - out_size)}")
                print(f"Сохранён: {output_path}")
            except Exception as exc:
                output_path.unlink(missing_ok=True)
                print(f"Ошибка ({source.name}): {exc}", file=sys.stderr)
                failures += 1
                # Continue with the next file even if one fails.
        if total > 1:
            print(f"\nГотово: успешно {total - failures} из {total}, ошибок {failures}.")
        return 1 if failures else 0
    # ---
    if args.quick_file:
        return run_quick_file_mode(
            args.quick_file, args.video_crf, args.video_preset, args.dry_run,
            disable_hw=args.disable_hw_video,
            video_codec=args.video_codec,
            video_qp=args.video_qp,
        )
    if args.review_duplicates:
        return run_review_duplicates_mode(args.input, args.output)
    if args.find_similar_photos:
        return run_find_similar_photos_mode(
            args.input, args.output, args.similar_threshold,
            best_shot=getattr(args, "best_shot", False),
        )
    if getattr(args, "review_similar_photos", False):
        return run_review_similar_photos_mode(args.input, args.output)
    if args.trash_similar_from_report:
        return run_trash_similar_from_report_mode(
            args.input, args.output, args.dry_run
        )
    if args.find_similar_videos:
        return run_find_similar_videos_mode(
            args.input,
            args.output,
            threshold=getattr(args, "similar_video_threshold", 8),
            n_samples=getattr(args, "similar_video_samples", 8),
            max_duration_ratio_diff=getattr(args, "_similar_video_dur_diff", 0.15),
        )
    if args.trash_similar_videos_from_report:
        return run_trash_similar_videos_from_report_mode(
            args.input, args.output, args.dry_run
        )
    if args.undo_move_duplicates:
        return run_undo_move_duplicates_mode(args.input, args.output, args.dry_run)
    if args.move_duplicates:
        return run_move_duplicates_mode(
            args.input, args.output, args.dry_run,
            fast=getattr(args, "fast_duplicates", False),
        )
    try:
        input_dir, output_dir = validate_paths(args.input, args.output)
    except ValueError as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 2

    files = collect_files(input_dir)
    if args.duplicates_only:
        return run_duplicates_only(
            files, input_dir, output_dir, args.dry_run,
            fast=getattr(args, "fast_duplicates", False),
        )

    try:
        validate_report_destinations(files, input_dir, output_dir, args.dry_run)
    except ValueError as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 2
    has_videos = any(path.suffix.lower() in VIDEO_EXTENSIONS for path in files)
    if not args.dry_run and has_videos and shutil.which("ffmpeg") is None:
        print("Ошибка: ffmpeg не найден. Установите его командой: brew install ffmpeg", file=sys.stderr)
        return 2
    # --- explicit codec override (--video-codec hevc_videotoolbox) ---
    # In non-quick-file modes (batch), honour --video-codec the same way.
    video_qp_main: int = 0  # 0 means: derive from CRF (auto path)
    if args.video_codec == "hevc_videotoolbox":
        rc = _validate_hevc_videotoolbox()
        if rc != 0:
            return rc
        # Warn if --video-crf was changed from default (it will be ignored).
        if args.video_crf != 31:
            print(
                f"Предупреждение: --video-crf={args.video_crf} игнорируется при использовании hevc_videotoolbox. "
                f"Используется q:v={args.video_qp} (из --video-qp).",
            )
        video_encoder = "hevc_videotoolbox"
        video_qp_main = args.video_qp
    else:
        # Select encoder once per run; on macOS this probes ffmpeg for VideoToolbox.
        video_encoder = select_video_encoder(disable_hw=args.disable_hw_video)
    if has_videos and not args.dry_run:
        _explicit_qp_main = video_qp_main if args.video_codec == "hevc_videotoolbox" else None
        for line in quality_mode_display(
            video_encoder, args.video_crf, args.video_preset, explicit_qp=_explicit_qp_main
        ):
            print(line)
    counters = Counters(total=len(files))
    duplicate_rows, counters.duplicates, duplicate_errors = find_duplicates(
        files, input_dir, fast=getattr(args, "fast_duplicates", False),
    )
    errors = list(duplicate_errors)
    summary_rows: list[dict[str, object]] = []
    total_videos = sum(classify(path) == "video" for path in files)
    current_video = 0
    # Destinations claimed by THIS run. Needed because destination_for() rewrites
    # every video suffix to .mp4, so Ep01.mkv and Ep01.mp4 collide on Ep01.mp4.
    # Kept in sync between the dry-run plan and the real run.
    occupied: set[Path] = set()

    print(f"Режим: {'DRY-RUN (без изменений)' if args.dry_run else 'обработка'}")
    print(f"Input:  {input_dir}")
    print(f"Output: {output_dir}")

    for source in files:
        category = classify(source)
        action = planned_action(category)
        destination = destination_for(source, input_dir, output_dir)
        if category == "video":
            counters.videos += 1
            current_video += 1
        elif category in {"jpeg", "image-copy", "raw-copy"}:
            counters.photos += 1
        else:
            counters.copied += 1

        try:
            if source.is_symlink():
                raise RuntimeError("Символическая ссылка пропущена для безопасности")
            original_size = source.stat().st_size
        except (OSError, RuntimeError) as exc:
            message = f"{source}: {exc}"
            errors.append(message)
            print(f"ОШИБКА: {message}")
            continue

        # --skip-already-compressed: decided BEFORE the destination is reserved,
        # because a file that is only copied must keep its original container
        # extension instead of being renamed to .mp4.
        skip_note = ""
        if getattr(args, "skip_already_compressed", False):
            if category == "video":
                is_efficient, skip_reason = video_is_already_efficient(source, args.video_crf)
            elif category == "jpeg":
                is_efficient, skip_reason = jpeg_is_already_efficient(
                    source, args.image_quality, original_size,
                )
            else:
                is_efficient, skip_reason = False, ""
            if is_efficient:
                skip_note = skip_reason
                action = "copy-already-efficient"
                destination = output_dir / source.relative_to(input_dir)

        collision_note = ""
        if args.dry_run:
            # Mirror the real run exactly: same collision handling, same names.
            if destination in occupied:
                renamed = unique_path_for_existing_target(destination, occupied)
                collision_note = (
                    f"Имя изменено: {destination.name} уже занято другим "
                    "исходным файлом этого запуска"
                )
                print(
                    f"[renamed] {destination.relative_to(output_dir)} -> "
                    f"{renamed.relative_to(output_dir)}"
                )
                destination = renamed
            already_there = destination.exists()
            status = "would-skip-existing" if already_there else "planned"
            note = "Файл уже существует; не будет перезаписан" if already_there else ""
            if skip_note:
                if not already_there:
                    status = "would-skip-already-efficient"
                note = f"{note}; {skip_note}" if note else skip_note
            if collision_note:
                note = f"{note}; {collision_note}" if note else collision_note
            if not already_there:
                # A pre-existing file is skipped, not claimed — same as the real
                # run, where a failed reservation never enters `occupied`.
                occupied.add(destination)
            print(f"[{action}] {source.relative_to(input_dir)} -> {destination.relative_to(output_dir)}")
            summary_rows.append(make_summary_row(
                source, destination, input_dir, output_dir, category, action,
                status, original_size, "", note,
            ))
            continue

        try:
            if not reserve_destination(destination):
                if destination in occupied:
                    # Two different sources map to the same destination in THIS
                    # run (e.g. Ep01.mkv and Ep01.mp4 both become Ep01.mp4).
                    # Give the second one a free name instead of dropping it.
                    renamed = unique_path_for_existing_target(destination, occupied)
                    if not reserve_destination(renamed):
                        raise OSError(f"не удалось зарезервировать имя {renamed.name}")
                    print(
                        f"[renamed] {destination.relative_to(output_dir)} -> "
                        f"{renamed.relative_to(output_dir)}"
                    )
                    collision_note = (
                        f"Имя изменено: {destination.name} уже занято другим "
                        "исходным файлом этого запуска"
                    )
                    destination = renamed
                    occupied.add(destination)
                else:
                    # The file was already in output BEFORE this run — not ours,
                    # so it is left strictly alone.
                    note = "Файл уже существует; пропущен без перезаписи"
                    print(f"[skip-existing] {destination.relative_to(output_dir)}")
                    summary_rows.append(make_summary_row(
                        source, destination, input_dir, output_dir, category, action,
                        "skipped", original_size, destination.stat().st_size, note,
                    ))
                    continue
            else:
                occupied.add(destination)
        except OSError as exc:
            message = f"{source}: {exc}"
            errors.append(message)
            print(f"ОШИБКА: {message}")
            summary_rows.append(make_summary_row(
                source, destination, input_dir, output_dir, category, action,
                "error", original_size, "", str(exc),
            ))
            continue

        try:
            if skip_note:
                # Still lands in output, just byte-for-byte instead of re-encoded.
                output_size = copy_safely(source, destination)
                note = skip_note
            elif category == "video":
                print(
                    f"[video {current_video}/{total_videos}] "
                    f"Обрабатываю: {source.relative_to(input_dir)}",
                    flush=True,
                )
                output_size, note = compress_video(
                    source, destination, args.video_crf, args.video_preset,
                    args.show_ffmpeg_progress, encoder=video_encoder,
                    qp=video_qp_main,
                )
            elif category == "jpeg":
                output_size = compress_jpeg(source, destination, args.image_quality)
                note = "JPEG сжат; EXIF и доступные цветовые метаданные сохранены"
            else:
                output_size = copy_safely(source, destination)
                note = "Скопирован без изменений"
            counters.original_bytes += original_size
            counters.output_bytes += output_size
            if skip_note:
                print(
                    f"[пропущено] {source.relative_to(input_dir)} "
                    f"({format_bytes(original_size)}) — {skip_note}"
                )
            elif category == "video":
                saved_size = original_size - output_size
                print(
                    f"[готово] {source.relative_to(input_dir)}, "
                    f"до: {format_bytes(original_size)}, "
                    f"после: {format_bytes(output_size)}, "
                    f"экономия: {format_bytes(saved_size)}"
                )
            else:
                print(f"[готово] {source.relative_to(input_dir)} ({note})")
            if collision_note:
                note = f"{note}; {collision_note}" if note else collision_note
            summary_rows.append(make_summary_row(
                source, destination, input_dir, output_dir, category, action,
                "skipped-already-efficient" if skip_note else "completed",
                original_size, output_size, note,
            ))
        except Exception as exc:
            destination.unlink(missing_ok=True)
            counters.errors += 1
            message = f"{source}: {exc}"
            errors.append(message)
            print(f"ОШИБКА: {message}")
            error_note = f"{exc}; {collision_note}" if collision_note else str(exc)
            summary_rows.append(make_summary_row(
                source, destination, input_dir, output_dir, category, action,
                "error", original_size, "", error_note,
            ))

    counters.errors = len(errors)
    if not args.dry_run:
        try:
            write_reports(output_dir, summary_rows, duplicate_rows, errors)
        except OSError as exc:
            counters.errors += 1
            print(f"ОШИБКА записи отчётов: {exc}", file=sys.stderr)
    else:
        print("\nDRY-RUN: папки, файлы и отчёты не создавались.")

    print_totals(counters, args.dry_run)
    return 1 if counters.errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
