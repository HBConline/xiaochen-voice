#!/bin/bash
# Start llama-server for the Qwen3-0.6B voice brain.
# IMPORTANT: this llama.cpp build requires -fa WITH an explicit value — use `-fa on`.
# A bare `-fa` swallows the next flag as its value and the server refuses to start.
set -e
DIR="$(cd "$(dirname "$0")/.." && pwd)"
MODEL="${BRAIN_MODEL:-$DIR/brain/Qwen3-0.6B-xiaochen-Q4_K_M.gguf}"
PORT="${BRAIN_PORT:-8080}"
# 0.6B is small; 2 threads keeps it responsive without stealing cores from
# the ASR/TTS pipeline. Not pinned with taskset.
THREADS="${BRAIN_THREADS:-2}"

# llama-server is installed to ~/llama.cpp by build_llamacpp_orangepi.sh;
# fall back to PATH if it was installed system-wide.
LLAMA_SERVER="${LLAMA_SERVER:-$HOME/llama.cpp/llama-server}"
[ -x "$LLAMA_SERVER" ] || LLAMA_SERVER=llama-server

# -c/--parallel: this llama.cpp build defaults to 4 slots x 24064 ctx = ~3GB
# KV cache on a 4GB board. One slot, small context: replies are <= 64 tokens.
exec "$LLAMA_SERVER" -m "$MODEL" -fa on -t "$THREADS" --host 0.0.0.0 --port "$PORT" \
    -c 2048 --parallel 1
