#!/bin/bash
# Consistent Grade engine for macOS.
# First run: installs a private Python (via uv), the image libraries and FFmpeg.
# Every run: starts the engine. Keep this window open while you use the panel.
set -e
CG="$HOME/ConsistentGrade"
ENGINE="$CG/engine"
VENV="$CG/venv"
export PATH="/opt/homebrew/bin:/usr/local/bin:$HOME/.local/bin:$PATH"
cd "$ENGINE"
echo "=== Consistent Grade engine ==="

if [ ! -x "$VENV/bin/python" ] || [ "$ENGINE/requirements.txt" -nt "$VENV/.installed" ]; then
  echo "First-time setup (needs internet, takes a few minutes)…"
  if ! command -v uv >/dev/null 2>&1; then
    echo "• Installing uv (Python installer) into ~/.local/bin"
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$PATH"
  fi
  echo "• Creating Python 3.12 environment in $VENV"
  uv venv --allow-existing --python 3.12 "$VENV"
  echo "• Installing numpy, OpenCV, Pillow"
  uv pip install --python "$VENV/bin/python" -r "$ENGINE/requirements.txt"
  if ! command -v ffmpeg >/dev/null 2>&1 || ! command -v ffprobe >/dev/null 2>&1; then
    if command -v brew >/dev/null 2>&1; then
      echo "• Installing FFmpeg with Homebrew"
      brew install ffmpeg
    else
      echo "• Homebrew not found; downloading a static FFmpeg build"
      uv pip install --python "$VENV/bin/python" static-ffmpeg
      "$VENV/bin/python" -c "import static_ffmpeg; static_ffmpeg.add_paths()"
    fi
  fi
  touch "$VENV/.installed"
  echo "Setup complete."
fi

exec "$VENV/bin/python" -m autograde worker --home "$CG"
