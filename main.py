import os
import re
import asyncio
from pathlib import Path
from typing import Optional

import yt_dlp
from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, HttpUrl

from editor import router as editor_router

app = FastAPI(title="Media Downloader", version="0.1.0")

CORS_ORIGINS = os.getenv("CORS_ORIGINS", "").split(",")
CORS_ORIGINS = [o.strip() for o in CORS_ORIGINS if o.strip()]

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS or ["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(editor_router)

DOWNLOADS_DIR = Path("downloads")
DOWNLOADS_DIR.mkdir(exist_ok=True)

YOUTUBE_COOKIES_FILE = os.getenv("YOUTUBE_COOKIES_FILE", "")


# ── Pydantic schemas ──────────────────────────────────────────────────────────

class DownloadRequest(BaseModel):
    url: str
    quality: Optional[str] = "best"  # "best" | "worst" | "1080p" | "720p" | "480p" | "360p"


class FormatInfo(BaseModel):
    format_id: str
    ext: str
    quality: Optional[float]
    resolution: Optional[str]
    filesize: Optional[int]
    vcodec: Optional[str]
    acodec: Optional[str]
    note: Optional[str]


class VideoInfo(BaseModel):
    title: str
    duration: Optional[int]
    uploader: Optional[str]
    view_count: Optional[int]
    thumbnail: Optional[str]
    description: Optional[str]
    webpage_url: str
    formats: list[FormatInfo]


# ── Helpers ───────────────────────────────────────────────────────────────────

def _sanitize_filename(name: str) -> str:
    return re.sub(r'[\\/*?:"<>|]', "_", name)


def _base_ydl_opts() -> dict:
    """Базовые опции yt-dlp, общие для всех запросов.
    Добавляет cookiefile если задан YOUTUBE_COOKIES_FILE — нужно для возрастных ограничений."""
    opts: dict = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        # Node.js + загрузка EJS-скрипта с GitHub для расшифровки форматов YouTube
        "js_runtimes": {"node": {}},
        "remote_components": ["ejs:github"],
    }
    if YOUTUBE_COOKIES_FILE and Path(YOUTUBE_COOKIES_FILE).exists():
        opts["cookiefile"] = YOUTUBE_COOKIES_FILE
    return opts


def _quality_to_format(quality: str) -> str:
    mapping = {
        "best":   "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
        "worst":  "worstvideo+worstaudio/worst",
        "2160p":  "bestvideo[height<=2160][ext=mp4]+bestaudio[ext=m4a]/best[height<=2160]",
        "1080p":  "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/best[height<=1080]",
        "720p":   "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/best[height<=720]",
        "480p":   "bestvideo[height<=480][ext=mp4]+bestaudio[ext=m4a]/best[height<=480]",
        "360p":   "bestvideo[height<=360][ext=mp4]+bestaudio[ext=m4a]/best[height<=360]",
    }
    return mapping.get(quality, mapping["best"])


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/info", response_model=VideoInfo, summary="Получить информацию о видео")
def get_info(url: str):
    """Возвращает метаданные и список доступных форматов без скачивания."""
    ydl_opts = {
        **_base_ydl_opts(),
        "skip_download": True,
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except yt_dlp.utils.DownloadError as e:
        raise HTTPException(status_code=400, detail=str(e))

    formats = []
    for f in info.get("formats", []):
        formats.append(
            FormatInfo(
                format_id=f.get("format_id", ""),
                ext=f.get("ext", ""),
                quality=f.get("quality"),
                resolution=f.get("resolution"),
                filesize=f.get("filesize") or f.get("filesize_approx"),
                vcodec=f.get("vcodec"),
                acodec=f.get("acodec"),
                note=f.get("format_note"),
            )
        )

    return VideoInfo(
        title=info.get("title", ""),
        duration=info.get("duration"),
        uploader=info.get("uploader"),
        view_count=info.get("view_count"),
        thumbnail=info.get("thumbnail"),
        description=info.get("description", "")[:500] if info.get("description") else None,
        webpage_url=info.get("webpage_url", url),
        formats=formats,
    )


@app.post("/download/video", summary="Скачать видео")
def download_video(req: DownloadRequest):
    """Скачивает видео в выбранном качестве. Возвращает JSON с именем файла.
    Сам файл можно получить через GET /downloads/{filename}/file"""
    fmt = _quality_to_format(req.quality)

    output_template = str(DOWNLOADS_DIR / "%(title)s.%(ext)s")
    ydl_opts = {
        **_base_ydl_opts(),
        "format": fmt,
        "outtmpl": output_template,
        "merge_output_format": "mp4",
        "postprocessors": [
            {
                "key": "FFmpegVideoConvertor",
                "preferedformat": "mp4",
            }
        ],
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(req.url, download=True)
            filename = ydl.prepare_filename(info)
            filepath = Path(filename).with_suffix(".mp4")
            if not filepath.exists():
                filepath = Path(filename)
    except yt_dlp.utils.DownloadError as e:
        raise HTTPException(status_code=400, detail=str(e))

    if not filepath.exists():
        raise HTTPException(status_code=500, detail="Файл не найден после скачивания")

    return {
        "filename": filepath.name,
        "size_mb": round(filepath.stat().st_size / 1024 / 1024, 2),
        "download_url": f"/downloads/{filepath.name}/file",
        "title": info.get("title", ""),
    }


@app.post("/download/audio", summary="Скачать аудио (MP3)")
def download_audio(req: DownloadRequest):
    """Скачивает только аудиодорожку и конвертирует в MP3.
    Возвращает JSON с именем файла. Файл — через GET /downloads/{filename}/file"""
    output_template = str(DOWNLOADS_DIR / "%(title)s.%(ext)s")
    ydl_opts = {
        **_base_ydl_opts(),
        "format": "bestaudio/best",
        "outtmpl": output_template,
        "writethumbnail": True,
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            },
            {
                "key": "EmbedThumbnail",
            },
            {
                "key": "FFmpegMetadata",
                "add_metadata": True,
            },
        ],
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(req.url, download=True)
            # yt-dlp меняет расширение после конвертации — строим путь через outtmpl
            raw_filename = ydl.prepare_filename(info)
            filepath = Path(raw_filename).with_suffix(".mp3")
    except yt_dlp.utils.DownloadError as e:
        raise HTTPException(status_code=400, detail=str(e))

    if not filepath.exists():
        raise HTTPException(status_code=500, detail="Файл не найден после скачивания")

    return {
        "filename": filepath.name,
        "size_mb": round(filepath.stat().st_size / 1024 / 1024, 2),
        "download_url": f"/downloads/{filepath.name}/file",
        "title": info.get("title", ""),
    }


@app.get("/downloads/edited/{filename}/file", summary="Скачать отредактированный файл")
def get_edited_file(filename: str):
    """Отдаёт отредактированный файл из downloads/edited/ для скачивания."""
    filepath = Path("downloads/edited") / filename
    if not filepath.exists():
        raise HTTPException(status_code=404, detail="Файл не найден")

    ext = filepath.suffix.lower()
    media_types = {
        ".mp4": "video/mp4",
        ".mp3": "audio/mpeg",
        ".webm": "video/webm",
        ".m4a": "audio/mp4",
        ".wav": "audio/wav",
        ".flac": "audio/flac",
        ".aac": "audio/aac",
    }
    return FileResponse(
        path=str(filepath),
        media_type=media_types.get(ext, "application/octet-stream"),
    )


@app.get("/downloads/{filename}/file", summary="Скачать файл на компьютер")
def get_file(filename: str):
    """Отдаёт файл для скачивания браузером."""
    filepath = DOWNLOADS_DIR / filename
    if not filepath.exists():
        raise HTTPException(status_code=404, detail="Файл не найден")

    ext = filepath.suffix.lower()
    media_types = {
        ".mp4": "video/mp4",
        ".mp3": "audio/mpeg",
        ".webm": "video/webm",
        ".m4a": "audio/mp4",
    }
    media_type = media_types.get(ext, "application/octet-stream")

    return FileResponse(
        path=str(filepath),
        media_type=media_type,
    )


@app.get("/downloads", summary="Список скачанных файлов")
def list_downloads():
    """Возвращает список файлов в папке downloads."""
    files = []
    for f in DOWNLOADS_DIR.iterdir():
        if f.is_file():
            files.append({
                "name": f.name,
                "size_mb": round(f.stat().st_size / 1024 / 1024, 2),
                "path": str(f),
            })
    return {"files": sorted(files, key=lambda x: x["name"])}


@app.delete("/downloads/{filename}", summary="Удалить скачанный файл")
def delete_download(filename: str):
    """Удаляет файл из папки downloads."""
    filepath = DOWNLOADS_DIR / filename
    if not filepath.exists():
        raise HTTPException(status_code=404, detail="Файл не найден")
    filepath.unlink()
    return {"detail": f"Файл {filename} удалён"}


@app.get("/health")
def health():
    return {"status": "ok"}
