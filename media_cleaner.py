#!/usr/bin/env python3
"""Safely copy and compress a media archive into a separate folder."""

from __future__ import annotations

import argparse
import csv
import hashlib
import html
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from collections import defaultdict
from dataclasses import dataclass
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
        description="Безопасно сжимает медиафайлы в отдельную папку. Оригиналы не меняются.",
        epilog=(
            "Примеры:\n"
            "  python3 media_cleaner.py --wizard\n"
            "  python3 media_cleaner.py INPUT OUTPUT --dry-run\n"
            "  python3 media_cleaner.py INPUT OUTPUT --duplicates-only\n"
            "  python3 media_cleaner.py INPUT OUTPUT --move-duplicates --dry-run\n"
            "  python3 media_cleaner.py INPUT OUTPUT --review-duplicates\n"
            "  python3 media_cleaner.py INPUT OUTPUT --find-similar-photos --similar-threshold 5\n"
            "  python3 media_cleaner.py INPUT OUTPUT --trash-similar-from-report --dry-run\n"
            "  python3 media_cleaner.py INPUT OUTPUT --find-similar-videos\n"
            "  python3 media_cleaner.py INPUT OUTPUT --trash-similar-videos-from-report --dry-run\n"
            "  python3 media_cleaner.py --quick-file /path/to/video.mov --video-crf 31 --video-preset fast"
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
            "Видеокодек: x265 (по умолчанию, libx265 + CRF) или "
            "hevc_videotoolbox (аппаратное HEVC, только macOS, использует --video-qp)"
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
        "--image-quality", type=int, default=85, choices=range(1, 101), metavar="1..100",
        help="Качество JPEG от 1 до 100 (по умолчанию: 85)",
    )
    args = parser.parse_args()
    args.wizard_cancelled = False
    args.movie_mkv = None  # populated by run_wizard_movie_mkv if chosen
    # Duration-diff attr is set by wizard; provide a default for CLI path.
    args._similar_video_dur_diff = 0.15
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
    ]
    if args.quick_file and (args.wizard or any(special_modes)):
        parser.error("--quick-file нельзя сочетать с другими режимами")
    if sum(bool(mode) for mode in special_modes) > 1:
        parser.error("выберите только один режим работы с дубликатами или похожими файлами")
    if args.dry_run and (args.review_duplicates or args.find_similar_photos or args.find_similar_videos):
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


def clean_terminal_path(value: str) -> Path:
    cleaned = value.strip()
    if len(cleaned) >= 2 and cleaned[0] == cleaned[-1] and cleaned[0] in {"'", '"'}:
        cleaned = cleaned[1:-1].strip()
    else:
        try:
            parts = shlex.split(cleaned)
            if len(parts) == 1:
                cleaned = parts[0]
        except ValueError:
            cleaned = cleaned.strip("'\"")
    return Path(cleaned).expanduser()


def clean_terminal_paths(value: str) -> list[Path]:
    """
    Parse ONE terminal line that may contain SEVERAL dragged paths.

    macOS Terminal drops multiple files as a single space-separated line,
    quoting or backslash-escaping any path that contains spaces. shlex.split
    understands both forms, so each token becomes one path. Falls back to
    treating the whole line as a single path if it can't be parsed.
    """
    cleaned = value.strip()
    if not cleaned:
        return []
    try:
        parts = shlex.split(cleaned)
    except ValueError:
        return [clean_terminal_path(cleaned)]
    if not parts:
        return []
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
    print(" 11. Сжать один фильм в MKV с выбором аудио и субтитров")
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
        print("  1. x265 (процессор, использует CRF) [по умолчанию]")
        print("  2. hevc_videotoolbox (аппаратный HEVC, только macOS, использует QP)")
        codec_choice_w = prompt_menu("Выберите 1-2 [Enter = 1]: ", {"1", "2"}, "1")
        wizard_video_codec = "x265" if codec_choice_w == "1" else "hevc_videotoolbox"
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


def find_duplicates(files: list[Path], input_dir: Path) -> tuple[list[dict[str, object]], int, list[str]]:
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
        for path in same_size_files:
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
) -> int:
    raw_rows, duplicate_copies, errors = find_duplicates(files, input_dir)
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

    output_path = output_path
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
    input_dir: Path, output_dir: Path, dry_run: bool,
) -> int:
    input_dir, output_dir = validate_paths(input_dir, output_dir)
    try:
        ensure_report_files_absent(output_dir, ["duplicates_report.csv", "moved_duplicates.csv", "summary.csv"])
    except FileExistsError as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 2

    files = collect_files(input_dir)
    duplicate_rows_flat, extra_copies, duplicate_errors = find_duplicates(files, input_dir)
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


def run_wizard_quick_file(args: argparse.Namespace) -> argparse.Namespace | None:
    while True:
        file_value = input("\nВставьте или перетащите видеофайл в Terminal: ")
        if not file_value.strip():
            print("Укажите путь к видеофайлу.")
            continue
        source = clean_terminal_path(file_value).resolve()
        if source.is_file() and not source.is_symlink():
            break
        print(f"Файл не найден: {source}")

    # --- codec selection ---
    print("\nКодек видео:")
    print("  1. x265 (процессор, использует CRF) [по умолчанию]")
    print("  2. hevc_videotoolbox (аппаратный HEVC, только macOS, использует QP)")
    codec_choice = prompt_menu("Выберите 1-2 [Enter = 1]: ", {"1", "2"}, "1")
    video_codec = "x265" if codec_choice == "1" else "hevc_videotoolbox"

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
        encoder = select_video_encoder(disable_hw=getattr(args, "disable_hw_video", False))

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


def make_thumbnail(source: Path, destination: Path) -> bool:
    destination.parent.mkdir(parents=True, exist_ok=True)
    extension = source.suffix.lower()
    if extension in SIMILAR_IMAGE_EXTENSIONS or extension == ".heif":
        try:
            from PIL import Image, ImageOps

            with Image.open(source) as image:
                image = ImageOps.exif_transpose(image)
                image.thumbnail((360, 260))
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
            "-vf", "scale='min(360,iw)':-2", str(destination),
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
    print(f"Режим: поиск визуально похожих видео")
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
                print(f"ПРОПУСК (метаданные)")
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


def run_find_similar_photos_mode(
    input_dir: Path, output_dir: Path, threshold: int,
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
            report_rows.append({
                "similar_group_id": group_id,
                "file_path": str(source),
                "file_size": item["size"],
                "image_width": item["width"],
                "image_height": item["height"],
                "perceptual_hash": str(item["hash"]),
                "distance_from_first": distance,
                "suggested_action": action,
            })
            item_html.append(
                f'<div class="item {"keep" if is_keeper else "review"}">'
                f'<strong>{html.escape(action)}</strong>'
                f'{thumbnail_html(thumbnail, output_dir, source.name)}'
                f'<p>{item["width"]} x {item["height"]}, {html.escape(format_bytes(int(item["size"])))}<br>'
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
        with (reports_dir / "similar_photos_report.csv").open("x", newline="", encoding="utf-8-sig") as handle:
            writer = csv.DictWriter(handle, fieldnames=[
                "similar_group_id", "file_path", "file_size", "image_width",
                "image_height", "perceptual_hash", "distance_from_first",
                "suggested_action",
            ])
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
    print(f"Изображений просканировано: {len(candidates)}")
    print(f"Групп похожих фото найдено: {len(similar_index_groups)}")
    print(f"Фото для ручной проверки: {review_duplicates}")
    print(f"Пропущено или не поддерживается: {len(skipped_rows)}")
    print("Похожие фото не перемещались и не удалялись.")
    print(f"Открой {html_path} для ручной проверки.")
    open_output_folder(output_dir)
    return 0


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
    """
    clamped = max(18, min(35, video_crf))
    # Linear interpolation: CRF 18 → 85, CRF 35 → 38
    q = round(85 - (clamped - 18) * (85 - 38) / (35 - 18))
    return max(1, min(100, q))


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
        }
        streams.append(entry)
    return streams


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


def movie_mkv_output_path(source: Path, crf: int, preset: str) -> Path:
    """
    Generate a safe output path for the MKV movie mode.
    Pattern: <stem>_movie_x265_crf<crf>_<preset>.mkv
    Adds _2, _3 … suffix if the candidate already exists.
    Does NOT create the file (that is left to reserve_destination).
    """
    base = f"{source.stem}_movie_x265_crf{crf}_{preset}.mkv"
    candidate = source.with_name(base)
    counter = 2
    while candidate.exists():
        candidate = source.with_name(
            f"{source.stem}_movie_x265_crf{crf}_{preset}_{counter}.mkv"
        )
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


def looks_like_previous_output(path: Path) -> bool:
    """
    True if *path* looks like a file this tool produced earlier.

    Older runs wrote their result next to the original, so a folder can hold
    both. Re-processing an already processed file is never what the user wants,
    so folder scanning skips these (they can still be selected by hand).
    """
    stem = path.stem
    if stem.endswith("_remux"):
        return True
    if re.search(r"_remux_\d+$", stem):
        return True
    return "_movie_x265_crf" in stem or "_compressed_crf" in stem


def movie_mkv_folder_output_path(
    source: Path, source_dir: Path, video_mode: str, crf: int,
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
    suffix = "remux" if video_mode == "copy" else f"x265 crf{crf}"
    out_dir = source_dir.parent / f"{source_dir.name} {suffix}"
    candidate = out_dir / f"{source.stem}.mkv"
    counter = 2
    while candidate.exists():
        candidate = out_dir / f"{source.stem}_{counter}.mkv"
        counter += 1
    return candidate


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
    video_mode: str = "encode",  # "encode" (libx265) | "copy" (remux, no re-encode)
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
    if video_mode == "copy":
        # Remux: keep the original video stream untouched (no quality loss, fast).
        cmd.extend(["-c:v", "copy"])
    else:
        # libx265 only; no HW branch — conservative choice.
        cmd.extend(["-c:v", "libx265", "-crf", str(crf), "-preset", preset])

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
        note = (
            "Фильм пересобран в MKV без сжатия видео"
            if video_mode == "copy" else "Фильм сжат в MKV"
        )
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
        print("Ошибка: не удалось получить информацию о потоках.", file=sys.stderr)
        return None

    video_streams   = [s for s in streams if s["codec_type"] == "video"]
    audio_streams   = [s for s in streams if s["codec_type"] == "audio"]
    subtitle_streams = [s for s in streams if s["codec_type"] == "subtitle"]

    if not video_streams:
        print("Ошибка: видеопотоков не найдено.", file=sys.stderr)
        return None
    if not audio_streams:
        print("Ошибка: аудиопотоков не найдено.", file=sys.stderr)
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
    print("\nРежим: сжать фильм(ы) в MKV с выбором дорожек")

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
                found = sorted(
                    path for path in resolved.iterdir()
                    if path.is_file()
                    and not path.is_symlink()
                    and path.suffix.lower() in VIDEO_EXTENSIONS
                )
                videos = [path for path in found if not looks_like_previous_output(path)]
                skipped = len(found) - len(videos)
                if not videos:
                    print(f"В папке нет видеофайлов: {resolved}")
                    continue
                print(f"Папка «{resolved.name}»: найдено видеофайлов — {len(videos)}")
                if skipped:
                    print(
                        f"  (пропущено {skipped} — похоже на результат прошлого запуска)"
                    )
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
        "\nСовет: для целого сезона перетащите ПАПКУ — будут добавлены все видео "
        "внутри неё. Терминал принимает не больше 1024 символов в строке, "
        "поэтому перетащить разом много файлов с длинными путями не получится."
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
    for source in sources:
        result = _probe_movie_streams(source)
        if result is None:
            return None
        probed.append((source, result))

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

    # --- step 6: video quality preset (asked ONCE for all files) ---
    print("\nСжатие видео для фильма:")
    print("  1. БЕЗ СЖАТИЯ — copy, оставить видео как есть (быстро, без потерь)")
    print("  2. Очень высокое качество: CRF 20")
    print("  3. Баланс: CRF 22 (по умолчанию)")
    print("  4. Компактнее: CRF 24")
    print("  5. Ввести вручную")
    q_choice = prompt_menu("Выберите 1-5 [Enter = 3]: ", set("12345"), "3")
    if q_choice == "1":
        video_mode = "copy"
        crf = 0
        preset = "copy"
    else:
        video_mode = "encode"
        crf_map = {"2": 20, "3": 22, "4": 24}
        if q_choice == "5":
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
                src, src_dir, video_mode, crf
            )
        elif video_mode == "copy":
            sel["output_path"] = movie_mkv_copy_output_path(src)
        else:
            sel["output_path"] = movie_mkv_output_path(src, crf, preset)

    # --- step 10: summary + confirmation ---
    audio_label = {"copy": "copy (без перекодирования)",
                   "aac": "aac 192k",
                   "opus": "libopus 128k"}[audio_mode]

    print("\nИтоговые настройки:")
    print(f"  Контейнер  : MKV")
    if video_mode == "copy":
        print(f"  Видео       : copy (без сжатия, без потерь)")
    else:
        print(f"  Кодек видео: libx265")
        print(f"  CRF        : {crf}")
        print(f"  Preset     : {preset}")
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
    if args.wizard_cancelled:
        return 0
    # --- NEW: movie-to-MKV mode (set by run_wizard_movie_mkv) ---
    if args.movie_mkv is not None:
        if shutil.which("ffmpeg") is None:
            print("Ошибка: ffmpeg не найден. Установите: brew install ffmpeg",
                  file=sys.stderr)
            return 2
        # Support both a single job dict (backward compat) and a list of jobs.
        jobs = args.movie_mkv if isinstance(args.movie_mkv, list) else [args.movie_mkv]
        total = len(jobs)
        failures = 0
        for job_idx, m in enumerate(jobs, start=1):
            source: Path = m["source"]
            output_path: Path = m["output_path"]
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
            original_size = source.stat().st_size
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
            args.input, args.output, args.similar_threshold
        )
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
    if args.move_duplicates:
        return run_move_duplicates_mode(args.input, args.output, args.dry_run)
    try:
        input_dir, output_dir = validate_paths(args.input, args.output)
    except ValueError as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 2

    files = collect_files(input_dir)
    if args.duplicates_only:
        return run_duplicates_only(files, input_dir, output_dir, args.dry_run)

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
    duplicate_rows, counters.duplicates, duplicate_errors = find_duplicates(files, input_dir)
    errors = list(duplicate_errors)
    summary_rows: list[dict[str, object]] = []
    total_videos = sum(classify(path) == "video" for path in files)
    current_video = 0

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

        if args.dry_run:
            status = "would-skip-existing" if destination.exists() else "planned"
            note = "Файл уже существует; не будет перезаписан" if destination.exists() else ""
            print(f"[{action}] {source.relative_to(input_dir)} -> {destination.relative_to(output_dir)}")
            summary_rows.append(make_summary_row(
                source, destination, input_dir, output_dir, category, action,
                status, original_size, "", note,
            ))
            continue

        if not reserve_destination(destination):
            note = "Файл уже существует; пропущен без перезаписи"
            print(f"[skip-existing] {destination.relative_to(output_dir)}")
            summary_rows.append(make_summary_row(
                source, destination, input_dir, output_dir, category, action,
                "skipped", original_size, destination.stat().st_size, note,
            ))
            continue

        try:
            if category == "video":
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
            if category == "video":
                saved_size = original_size - output_size
                print(
                    f"[готово] {source.relative_to(input_dir)}, "
                    f"до: {format_bytes(original_size)}, "
                    f"после: {format_bytes(output_size)}, "
                    f"экономия: {format_bytes(saved_size)}"
                )
            else:
                print(f"[готово] {source.relative_to(input_dir)} ({note})")
            summary_rows.append(make_summary_row(
                source, destination, input_dir, output_dir, category, action,
                "completed", original_size, output_size, note,
            ))
        except Exception as exc:
            destination.unlink(missing_ok=True)
            counters.errors += 1
            message = f"{source}: {exc}"
            errors.append(message)
            print(f"ОШИБКА: {message}")
            summary_rows.append(make_summary_row(
                source, destination, input_dir, output_dir, category, action,
                "error", original_size, "", str(exc),
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
