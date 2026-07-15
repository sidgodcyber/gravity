"""Local voice transcription (faster-whisper, CPU, int8).

Always invoked as a short-lived subprocess (`exo transcribe <file>`) so the
model's memory is returned to the OS the moment it exits — the resident bot
stays flat. Telegram voice notes are OGG/Opus; faster-whisper decodes them
via PyAV, so no system ffmpeg is needed.
"""

from __future__ import annotations

import logging

from ..config import Config

log = logging.getLogger("exo.transcribe")


def transcribe_file(cfg: Config, audio_path: str) -> str:
    try:
        from faster_whisper import WhisperModel
    except ImportError as e:
        raise SystemExit(
            "voice transcription needs the 'voice' extra:"
            "  uv sync --extra voice   (or pip install faster-whisper)"
        ) from e
    name = cfg.get("voice.whisper_model", "base")
    cache = cfg._path(cfg.get("embeddings.cache_dir", ".cache/models"))
    model_dir = cache / f"whisper-{name}"
    if not (model_dir / "model.bin").exists():
        # Download as PLAIN FILES. The default HF cache layout uses symlinks,
        # which Windows refuses without Developer Mode (WinError 1314).
        from faster_whisper.utils import download_model
        model_dir.mkdir(parents=True, exist_ok=True)
        download_model(name, output_dir=str(model_dir))
    model = WhisperModel(str(model_dir), device="cpu", compute_type="int8")
    # faster-whisper already streams the whole file in ~30s windows internally,
    # so a long note no longer truncates; condition_on_previous_text=False stops
    # one mishcard cascading into later chunks. beam_size=1 keeps CPU cost low.
    segments, _info = model.transcribe(
        str(audio_path), vad_filter=True, beam_size=1,
        condition_on_previous_text=False,
    )
    return " ".join(s.text.strip() for s in segments if s.text.strip()).strip()
