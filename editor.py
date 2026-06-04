"""
Эндпоинты для обработки уже скачанных аудио/видео файлов.
Все операции выполняются через ffmpeg.
"""

import ffmpeg
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Optional
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, UploadFile, File, Form
from fastapi.responses import FileResponse
from pydantic import BaseModel, field_validator

DOWNLOADS_DIR = Path("downloads")
EDITED_DIR = Path("downloads/edited")
EDITED_DIR.mkdir(parents=True, exist_ok=True)


router = APIRouter(prefix="/edit", tags=["edit"])


def _edited_url(filename: str) -> str:
    """Безопасный URL для отредактированного файла с percent-encoding."""
    return f"/downloads/edited/{quote(filename)}/file"


# ── Helpers ───────────────────────────────────────────────────────────────────

def _resolve_file(filename: str) -> Path:
    """Ищет файл: сначала в downloads/edited/ (приоритет — последняя версия),
    затем в downloads/ (оригинал)."""
    for base in (EDITED_DIR, DOWNLOADS_DIR):
        path = base / filename
        if path.exists():
            return path
    raise HTTPException(status_code=404, detail=f"Файл не найден: {filename}")


def _parse_time(value: str) -> float:
    """
    Принимает секунды ('90', '90.5') или формат 'HH:MM:SS' / 'MM:SS'.
    Возвращает количество секунд в виде float.
    """
    if ":" in value:
        parts = value.strip().split(":")
        parts = [float(p) for p in parts]
        if len(parts) == 2:
            return parts[0] * 60 + parts[1]
        if len(parts) == 3:
            return parts[0] * 3600 + parts[1] * 60 + parts[2]
        raise ValueError("Неверный формат времени")
    return float(value)


def _out_path(stem: str, suffix: str, tag: str) -> Path:
    """Строит путь для выходного файла в downloads/edited/.

    Все редактирования одного трека всегда идут в один файл:
      downloads/edited/{оригинальный_stem}.mp3
    Суффиксы __cover/__meta не накапливаются.
    Если входной файл совпадает с выходным — ffmpeg пишет во временный файл,
    который потом заменяет оригинал.
    """
    clean = re.sub(r"(__[a-zA-Z0-9]+)+$", "", stem)
    return EDITED_DIR / f"{clean}{suffix}"


def _safe_ffmpeg_run(args: list, src: Path, out: Path) -> None:
    """Запускает ffmpeg. Если src == out, использует временный файл."""
    if src.resolve() == out.resolve():
        tmp = out.with_suffix(".tmp" + out.suffix)
        run_args = args[:-1] + [str(tmp)]
        proc = subprocess.run(run_args, capture_output=True)
        if proc.returncode != 0:
            if tmp.exists():
                tmp.unlink()
            raise RuntimeError(proc.stderr.decode("utf-8", errors="replace")[-800:])
        tmp.replace(out)
    else:
        proc = subprocess.run(args, capture_output=True)
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr.decode("utf-8", errors="replace")[-800:])


# ── Schemas ───────────────────────────────────────────────────────────────────

class TrimRequest(BaseModel):
    filename: str
    start: str = "0"   # секунды или HH:MM:SS
    end: Optional[str] = None  # если не указан — до конца файла

    @field_validator("start", "end", mode="before")
    @classmethod
    def coerce_to_str(cls, v):
        return str(v) if v is not None else v


class ExtractAudioRequest(BaseModel):
    filename: str
    format: str = "mp3"   # mp3 | aac | wav | flac
    quality: str = "192k"  # битрейт для mp3/aac


class ConvertRequest(BaseModel):
    filename: str
    format: str  # mp4 | webm | mp3 | wav | flac


class SpeedRequest(BaseModel):
    filename: str
    speed: float  # 0.5 = вдвое медленнее, 2.0 = вдвое быстрее

    @field_validator("speed")
    @classmethod
    def speed_range(cls, v):
        if not 0.25 <= v <= 4.0:
            raise ValueError("speed должен быть от 0.25 до 4.0")
        return v


# ── Routes ────────────────────────────────────────────────────────────────────

@router.post("/trim", summary="Обрезать аудио или видео")
def trim(req: TrimRequest):
    """
    Вырезает фрагмент из файла.

    - **start**: начало в секундах или `HH:MM:SS` (по умолчанию 0)
    - **end**: конец в секундах или `HH:MM:SS` (по умолчанию — конец файла)

    Пример: `{"filename": "song.mp3", "start": "1:30", "end": "3:00"}`
    """
    src = _resolve_file(req.filename)
    start_sec = _parse_time(req.start)

    kwargs = {"ss": start_sec, "loglevel": "error"}
    if req.end is not None:
        end_sec = _parse_time(req.end)
        if end_sec <= start_sec:
            raise HTTPException(status_code=400, detail="end должен быть больше start")
        kwargs["t"] = end_sec - start_sec  # ffmpeg принимает длительность, не конец

    out = _out_path(src.stem, src.suffix, f"trim_{req.start}-{req.end or 'end'}".replace(":", "-"))

    try:
        (
            ffmpeg
            .input(str(src), **kwargs)
            .output(str(out), c="copy")  # copy — без перекодирования, мгновенно
            .overwrite_output()
            .run()
        )
    except ffmpeg.Error as e:
        raise HTTPException(status_code=500, detail=e.stderr.decode() if e.stderr else str(e))

    return {
        "filename": out.name,
        "size_mb": round(out.stat().st_size / 1024 / 1024, 2),
        "download_url": _edited_url(out.name),
    }


@router.post("/extract-audio", summary="Извлечь аудиодорожку из видео")
def extract_audio(req: ExtractAudioRequest):
    """
    Вытаскивает аудиодорожку из видеофайла и сохраняет в выбранном формате.

    Поддерживаемые форматы: `mp3`, `aac`, `wav`, `flac`
    """
    allowed = {"mp3", "aac", "wav", "flac"}
    if req.format not in allowed:
        raise HTTPException(status_code=400, detail=f"Формат должен быть одним из: {allowed}")

    src = _resolve_file(req.filename)
    out = _out_path(src.stem, f".{req.format}", "audio")

    try:
        stream = ffmpeg.input(str(src))
        audio = stream.audio

        if req.format == "mp3":
            out_stream = ffmpeg.output(audio, str(out), acodec="libmp3lame", audio_bitrate=req.quality, loglevel="error")
        elif req.format == "aac":
            out_stream = ffmpeg.output(audio, str(out), acodec="aac", audio_bitrate=req.quality, loglevel="error")
        elif req.format == "wav":
            out_stream = ffmpeg.output(audio, str(out), acodec="pcm_s16le", loglevel="error")
        else:  # flac
            out_stream = ffmpeg.output(audio, str(out), acodec="flac", loglevel="error")

        out_stream.overwrite_output().run()
    except ffmpeg.Error as e:
        raise HTTPException(status_code=500, detail=e.stderr.decode() if e.stderr else str(e))

    return {
        "filename": out.name,
        "size_mb": round(out.stat().st_size / 1024 / 1024, 2),
        "download_url": _edited_url(out.name),
    }


@router.post("/upload-extract-audio", summary="Извлечь аудио из загруженного видеофайла")
async def upload_extract_audio(
    file: UploadFile = File(..., description="Видеофайл с компьютера (mp4, mkv, avi, mov, webm …)"),
    format: str = Form("mp3"),   # mp3 | aac | wav | flac
    quality: str = Form("192k"), # битрейт для mp3/aac
):
    """
    Принимает видеофайл, загруженный прямо с компьютера, и извлекает из него аудиодорожку.

    - **file**: видеофайл — поддерживаются mp4, mkv, avi, mov, webm и любой формат, который умеет ffmpeg
    - **format**: формат выходного аудио — `mp3`, `aac`, `wav`, `flac` (по умолчанию `mp3`)
    - **quality**: битрейт для mp3/aac, например `128k`, `192k`, `256k`, `320k` (по умолчанию `192k`)

    Максимальный размер файла — **500 МБ**.
    Результат доступен через `download_url` из ответа.
    """
    allowed_formats = {"mp3", "aac", "wav", "flac"}
    if format not in allowed_formats:
        raise HTTPException(status_code=400, detail=f"Формат должен быть одним из: {allowed_formats}")

    original_name = file.filename or "video"
    stem = re.sub(r'[\\/*?:"<>|]', "_", Path(original_name).stem) or "audio"
    src_suffix = Path(original_name).suffix or ".mp4"

    MAX_SIZE = 500 * 1024 * 1024

    tmp_path: Optional[str] = None
    try:
        with tempfile.NamedTemporaryFile(suffix=src_suffix, delete=False) as tmp:
            shutil.copyfileobj(file.file, tmp)
            tmp_path = tmp.name

        file_size = os.path.getsize(tmp_path)
        if file_size == 0:
            raise HTTPException(status_code=400, detail="Загруженный файл пустой")
        if file_size > MAX_SIZE:
            raise HTTPException(status_code=400, detail="Файл слишком большой — максимум 500 МБ")

        out = _out_path(stem, f".{format}", "audio")

        stream = ffmpeg.input(tmp_path)
        audio = stream.audio

        if format == "mp3":
            out_stream = ffmpeg.output(audio, str(out), acodec="libmp3lame", audio_bitrate=quality, loglevel="error")
        elif format == "aac":
            out_stream = ffmpeg.output(audio, str(out), acodec="aac", audio_bitrate=quality, loglevel="error")
        elif format == "wav":
            out_stream = ffmpeg.output(audio, str(out), acodec="pcm_s16le", loglevel="error")
        else:  # flac
            out_stream = ffmpeg.output(audio, str(out), acodec="flac", loglevel="error")

        try:
            out_stream.overwrite_output().run()
        except ffmpeg.Error as e:
            raise HTTPException(status_code=500, detail=e.stderr.decode("utf-8", errors="replace") if e.stderr else str(e))

    except HTTPException:
        raise
    except Exception as exc:
        import traceback
        raise HTTPException(status_code=500, detail=traceback.format_exc()) from exc
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)

    return {
        "filename": out.name,
        "size_mb": round(out.stat().st_size / 1024 / 1024, 2),
        "download_url": _edited_url(out.name),
        "original_name": original_name,
    }


@router.post("/convert", summary="Конвертировать в другой формат")
def convert(req: ConvertRequest):
    """
    Конвертирует файл в другой формат.

    Поддерживаемые форматы: `mp4`, `webm`, `mp3`, `wav`, `flac`
    """
    allowed = {"mp4", "webm", "mp3", "wav", "flac"}
    if req.format not in allowed:
        raise HTTPException(status_code=400, detail=f"Формат должен быть одним из: {allowed}")

    src = _resolve_file(req.filename)
    if src.suffix.lstrip(".") == req.format:
        raise HTTPException(status_code=400, detail="Файл уже в этом формате")

    out = _out_path(src.stem, f".{req.format}", "converted")

    try:
        (
            ffmpeg
            .input(str(src))
            .output(str(out), loglevel="error")
            .overwrite_output()
            .run()
        )
    except ffmpeg.Error as e:
        raise HTTPException(status_code=500, detail=e.stderr.decode() if e.stderr else str(e))

    return {
        "filename": out.name,
        "size_mb": round(out.stat().st_size / 1024 / 1024, 2),
        "download_url": _edited_url(out.name),
    }


@router.post("/speed", summary="Изменить скорость воспроизведения")
def change_speed(req: SpeedRequest):
    """
    Ускоряет или замедляет аудио/видео без изменения высоты тона.

    - **speed**: `0.5` — вдвое медленнее, `2.0` — вдвое быстрее (диапазон: 0.25–4.0)
    """
    src = _resolve_file(req.filename)
    suffix = src.suffix
    out = _out_path(src.stem, suffix, f"speed{req.speed}x".replace(".", "_"))

    try:
        stream = ffmpeg.input(str(src))
        is_video = suffix.lower() in {".mp4", ".webm", ".mkv", ".avi", ".mov"}

        if is_video:
            video = stream.video.filter("setpts", f"{1 / req.speed}*PTS")
            audio = stream.audio.filter("atempo", _clamp_atempo(req.speed))
            out_stream = ffmpeg.output(video, audio, str(out), loglevel="error")
        else:
            audio = stream.audio.filter("atempo", _clamp_atempo(req.speed))
            out_stream = ffmpeg.output(audio, str(out), loglevel="error")

        out_stream.overwrite_output().run()
    except ffmpeg.Error as e:
        raise HTTPException(status_code=500, detail=e.stderr.decode() if e.stderr else str(e))

    return {
        "filename": out.name,
        "size_mb": round(out.stat().st_size / 1024 / 1024, 2),
        "download_url": _edited_url(out.name),
    }


@router.get("/probe", summary="Информация о файле (длительность, кодеки, битрейт)")
def probe(filename: str):
    """Возвращает техническую информацию о файле через ffprobe."""
    src = _resolve_file(filename)
    try:
        info = ffmpeg.probe(str(src))
    except ffmpeg.Error as e:
        raise HTTPException(status_code=500, detail=e.stderr.decode() if e.stderr else str(e))

    streams = []
    for s in info.get("streams", []):
        streams.append({
            "type": s.get("codec_type"),
            "codec": s.get("codec_name"),
            "duration": s.get("duration"),
            "bitrate": s.get("bit_rate"),
            "sample_rate": s.get("sample_rate"),
            "channels": s.get("channels"),
            "resolution": f"{s.get('width')}x{s.get('height')}" if s.get("width") else None,
        })

    fmt = info.get("format", {})
    return {
        "filename": filename,
        "duration_sec": float(fmt.get("duration", 0)),
        "size_mb": round(int(fmt.get("size", 0)) / 1024 / 1024, 2),
        "bitrate": fmt.get("bit_rate"),
        "format": fmt.get("format_name"),
        "streams": streams,
    }


# ── Скачивание отредактированных файлов ──────────────────────────────────────

@router.get("/edited", summary="Список отредактированных файлов")
def list_edited():
    files = []
    for f in EDITED_DIR.iterdir():
        if f.is_file():
            files.append({
                "name": f.name,
                "size_mb": round(f.stat().st_size / 1024 / 1024, 2),
                "download_url": _edited_url(f.name),
            })
    return {"files": sorted(files, key=lambda x: x["name"])}


@router.post("/set-metadata", summary="Записать ID3-теги в аудиофайл")
def set_metadata(
    filename: str = Form(...),
    title:    str = Form(""),
    artist:   str = Form(""),
):
    """
    Записывает title и artist в ID3-теги аудиофайла через ffmpeg.
    Возвращает путь к новому файлу с обновлёнными тегами.
    """
    src = _resolve_file(filename)
    out = _out_path(src.stem, src.suffix, "meta")

    args = [
        "ffmpeg", "-y",
        "-i", str(src),
        "-c", "copy",
        "-map_metadata", "0",
    ]
    if title.strip():
        args += ["-metadata", f"title={title.strip()}"]
    if artist.strip():
        args += ["-metadata", f"artist={artist.strip()}"]
    args.append(str(out))

    try:
        _safe_ffmpeg_run(args, src, out)
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=f"ffmpeg error: {e}")

    return {
        "filename": out.name,
        "size_mb": round(out.stat().st_size / 1024 / 1024, 2),
        "download_url": _edited_url(out.name),
    }


@router.post("/set-cover", summary="Встроить обложку в аудиофайл (MP3)")
async def set_cover(
    filename: str = Form(...),
    cover: UploadFile = File(...),
):
    """
    Встраивает изображение как обложку (ID3 APIC) в MP3-файл.

    - **filename**: имя файла в папке downloads/ или downloads/edited/
    - **cover**: изображение (JPG / PNG / WebP), максимум 10 МБ

    Возвращает путь к новому файлу с обложкой.
    """
    src = _resolve_file(filename)
    if src.suffix.lower() not in {".mp3", ".m4a", ".flac"}:
        raise HTTPException(status_code=400, detail="Встраивание обложки поддерживается только для MP3 / M4A / FLAC")

    img_data = await cover.read()

    # Лимит 10 МБ
    if len(img_data) > 10 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="Изображение слишком большое — максимум 10 МБ")

    if not img_data:
        raise HTTPException(status_code=400, detail="Файл изображения пустой")

    # Определяем расширение по magic bytes, игнорируя имя файла
    img_suffix = _detect_image_suffix(img_data, cover.filename)
    if img_suffix is None:
        raise HTTPException(
            status_code=400,
            detail="Неподдерживаемый формат изображения — нужен JPG, PNG или WebP",
        )

    tmp_path: Optional[str] = None
    try:
        with tempfile.NamedTemporaryFile(suffix=img_suffix, delete=False) as tmp:
            tmp.write(img_data)
            tmp_path = tmp.name

        out = _out_path(src.stem, src.suffix, "cover")

        cover_args = [
            "ffmpeg", "-y",
            "-i", str(src),
            "-i", tmp_path,
            "-map", "0:0",
            "-map", "1:0",
            "-c", "copy",
            "-id3v2_version", "3",
            "-metadata:s:v", "title=Album cover",
            "-metadata:s:v", "comment=Cover (front)",
            str(out),
        ]
        try:
            _safe_ffmpeg_run(cover_args, src, out)
        except RuntimeError as e:
            raise HTTPException(status_code=500, detail=f"ffmpeg error: {e}")

    except HTTPException:
        raise
    except Exception as exc:
        import traceback
        raise HTTPException(status_code=500, detail=traceback.format_exc()) from exc
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)

    return {
        "filename": out.name,
        "size_mb": round(out.stat().st_size / 1024 / 1024, 2),
        "download_url": _edited_url(out.name),
    }


# ── Internal ──────────────────────────────────────────────────────────────────

def _detect_image_suffix(data: bytes, filename: Optional[str]) -> Optional[str]:
    """Определяет тип изображения по magic bytes и fallback на расширение файла."""
    if data[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    # fallback — по расширению из имени файла
    if filename:
        ext = Path(filename).suffix.lower()
        if ext in {".jpg", ".jpeg", ".png", ".webp"}:
            return ext if ext != ".jpeg" else ".jpg"
    return None


def _clamp_atempo(speed: float) -> float:
    """
    ffmpeg atempo работает только в диапазоне [0.5, 2.0].
    Для значений вне диапазона нужно цеплять фильтры последовательно,
    но для простоты зажимаем в допустимый диапазон.
    """
    return max(0.5, min(2.0, speed))
