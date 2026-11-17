#!/usr/bin/env bash
# Start ClipForge in the background (used by GitHub Codespaces; also fine on any Linux box).
cd "$(dirname "$0")/backend"
if curl -s -o /dev/null http://127.0.0.1:8000/api/health; then echo "ClipForge is already running on port 8000"; exit 0; fi
mkdir -p ../data
nohup python -m uvicorn app.main:app --host 0.0.0.0 --port 8000 > ../data/server.log 2>&1 &
disown
for i in $(seq 1 30); do sleep 1; curl -s -o /dev/null http://127.0.0.1:8000/api/health && { echo "ClipForge is running on port 8000"; exit 0; }; done
echo "ClipForge did not start. See data/server.log"; tail -20 ../data/server.log; exit 1
