# Media Downloader

FastAPI-бэкенд для скачивания аудио/видео с YouTube.

## Установка

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Также нужен **ffmpeg**:
```bash
brew install ffmpeg   # macOS
```

## Запуск

```bash
source .venv/bin/activate
uvicorn main:app --reload --port 8000
```

Документация API доступна на: http://localhost:8000/docs

## API

| Метод | Путь | Описание |
|-------|------|----------|
| GET | `/info?url=<URL>` | Метаданные + список форматов |
| POST | `/download/video` | Скачать видео (MP4) |
| POST | `/download/audio` | Скачать аудио (MP3) |
| GET | `/downloads` | Список скачанных файлов |
| DELETE | `/downloads/{filename}` | Удалить файл |
| GET | `/health` | Проверка работоспособности |

### Параметры `/download/video`

```json
{
  "url": "https://www.youtube.com/watch?v=...",
  "quality": "720p"
}
```

Доступные качества: `best` (по умолчанию), `2160p`, `1080p`, `720p`, `480p`, `360p`, `worst`

### Параметры `/download/audio`

```json
{
  "url": "https://www.youtube.com/watch?v=..."
}
```

## Скачанные файлы

Все файлы сохраняются в папку `downloads/` в корне проекта.
