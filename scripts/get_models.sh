#!/usr/bin/env sh
# Optional: fetch the sherpa-onnx Whisper model (fallback speech engine, hosted on GitHub).
# Not needed if faster-whisper can download its own model (the default).
set -e
SIZE="${1:-base.en}"
DEST="${CLIPFORGE_MODELS_DIR:-$(dirname "$0")/../models}"
mkdir -p "$DEST" && cd "$DEST"
curl -L -O "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/sherpa-onnx-whisper-$SIZE.tar.bz2"
tar xjf "sherpa-onnx-whisper-$SIZE.tar.bz2" && rm "sherpa-onnx-whisper-$SIZE.tar.bz2"
pip install sherpa-onnx
echo "Model ready in $DEST/sherpa-onnx-whisper-$SIZE"
