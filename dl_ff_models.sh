#!/usr/bin/env bash
set -euo pipefail

ROPE_DIR="/workspace/Rope"
ASSETS_DIR="/tmp/rope-assets"

echo "[1/6] Checking Rope dir..."
test -d "$ROPE_DIR" || { echo "Missing $ROPE_DIR"; exit 1; }

echo "[2/6] Creating target folders..."
mkdir -p \
  "$ROPE_DIR/models" \
  "$ROPE_DIR/dfl_models" \
  "$ROPE_DIR/liveportrait_onnx" \
  "$ROPE_DIR/gfpgan" \
  "$ROPE_DIR/codeformer"

echo "[3/6] Fetching rope-assets..."
rm -rf "$ASSETS_DIR"
git clone --depth 1 https://github.com/Alucard24/rope-assets "$ASSETS_DIR"

echo "[4/6] Copying model files..."
find "$ASSETS_DIR" -type f \( \
  -iname "*.onnx" -o \
  -iname "*.pth"  -o \
  -iname "*.pt"   -o \
  -iname "*.ckpt" -o \
  -iname "*.engine" -o \
  -iname "*.trt" \
\) | while read -r f; do
  base="$(basename "$f")"
  case "$base" in
    *.dfm)
      cp -f "$f" "$ROPE_DIR/dfl_models/$base" ;;
    *liveportrait*|*landmark203*|*motion_extractor*|*appearance_feature_extractor*|*stitching*|*warping*)
      cp -f "$f" "$ROPE_DIR/liveportrait_onnx/$base" ;;
    *)
      cp -f "$f" "$ROPE_DIR/models/$base" ;;
  esac
done

echo "[5/6] Copying DFM models..."
find "$ASSETS_DIR" -type f -iname "*.dfm" -exec cp -f {} "$ROPE_DIR/dfl_models/" \; || true

echo "[6/6] Done. Installed files:"
echo "--- models ---"
find "$ROPE_DIR/models" -maxdepth 1 -type f | sort
echo "--- dfl_models ---"
find "$ROPE_DIR/dfl_models" -maxdepth 1 -type f | sort
echo "--- liveportrait_onnx ---"
find "$ROPE_DIR/liveportrait_onnx" -maxdepth 1 -type f | sort || true
