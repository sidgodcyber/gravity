# This machine (DESKTOP-NEVA91K)

- 7.8 GB RAM; C: is nearly full (~12 GB free) — keep venvs, caches, models on D:.
  uv cache is redirected via UV_CACHE_DIR=d:\gravity\.uv-cache during installs;
  fastembed model cache goes to .cache/models (config `embeddings.cache_dir`).
- uv resolved Python 3.14.2 for .venv (system also has 3.13). All deps have
  3.14 wheels as of 2026-07.
- Full pytest run takes ~3 minutes here; the firewall tests spawn `cmd /c
  mklink /J` (junctions work without admin; os.symlink usually needs dev mode
  and raises OSError — tests fall back to junctions, skip if neither works).
- rclone and age CLI are NOT installed. Sync uses pyrage (no CLI needed);
  rclone must be installed by the user before remote sync works.
- HuggingFace hub's default cache layout uses SYMLINKS and crashes with
  WinError 1314 on this machine (no Developer Mode). Any HF model download
  must use plain-file mode (e.g. faster_whisper download_model(output_dir=...),
  or local_dir snapshot downloads). fastembed worked because its cache path
  avoids the blob/symlink layout.
- Windows Defender intermittently denies CreateProcess (WinError 5 / EPERM)
  during heavy test runs — ANY subprocess spawn can fail transiently, even
  `python -c` or powershell itself. Tests that spawn subprocesses must retry
  with backoff and skip (not fail) if the OS keeps refusing. It clears after
  ~seconds.
