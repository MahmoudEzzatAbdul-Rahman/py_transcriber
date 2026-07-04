# Call Transcription CLI

Transcribe call recordings (audio or video) into timestamped segments stored in SQLite, using Gemini multimodal audio for Arabic/English code-switching.

## Prerequisites

- Python 3.11+
- [ffmpeg](https://ffmpeg.org/) on your PATH (`brew install ffmpeg` on macOS, `apt install ffmpeg` on Debian/Ubuntu)
- A [Gemini API key](https://ai.google.dev/)

## Setup

```bash
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env          # then set GEMINI_API_KEY
```

## Usage

```bash
# Transcribe a recording (audio or video; screen share video uses audio track only)
python -m transcriber.cli transcribe path/to/recording.mp4

# List all recordings
python -m transcriber.cli list

# Show full transcript for a recording
python -m transcriber.cli show <recording_id>

# Export as JSON
python -m transcriber.cli show <recording_id> --json

# Filter by time range (seconds)
python -m transcriber.cli show <recording_id> --from 120 --to 300
```

## Example output

```
Recording abc123 saved (42 segments)

[00:00.00 - 00:04.20] Speaker 1 (mixed): Hello, كيف حالك today?
...
```

## Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `GEMINI_API_KEY` | — | Required. Your Google AI API key |
| `DATABASE_PATH` | `./data/transcriber.db` | SQLite database file location |
| `GEMINI_MODEL` | `gemini-2.5-flash` | Gemini model for transcription |

## Project layout

```
transcriber/
├── transcriber/       # Python package
│   ├── cli.py         # Typer CLI commands
│   ├── audio.py       # ffmpeg extract + validation
│   ├── gemini_client.py
│   ├── db.py
│   └── models.py
└── data/              # SQLite DB (gitignored)
```

## License

MIT
