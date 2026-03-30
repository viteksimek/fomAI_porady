#!/usr/bin/env bash
# Nahraje lokální audio na API (prepare-upload → PUT GCS → finalize) a čeká na completed | failed.
# Volitelně: export API_KEY=…  export BASE_URL=https://…
set -euo pipefail

BASE_URL="${BASE_URL:-https://fomai-porady-635664358681.europe-west1.run.app}"
BASE_URL="${BASE_URL%/}"
FILE="${1:?Cesta k souboru, např. ~/Desktop/SLS.m4a}"

if [[ ! -f "$FILE" ]]; then
  echo "Soubor neexistuje: $FILE" >&2
  exit 1
fi

# Mac bash 3.2 + set -u: neexpandovat prázdné pole; použijeme podmíněné hlavičky.
HDR_KEY=()
if [[ -n "${API_KEY:-}" ]]; then
  HDR_KEY=(-H "X-API-Key: $API_KEY")
fi

BODY=$(python3 -c "
import json, os, sys
p = sys.argv[1]
print(json.dumps({
    'filename': os.path.basename(p),
    'content_type': 'audio/mp4',
    'options': {'language_hint': 'cs'},
}))
" "$FILE")

echo "→ POST prepare-upload …" >&2
RESP=$(curl -sS -X POST "$BASE_URL/v1/jobs/prepare-upload" \
  -H "Content-Type: application/json" \
  ${HDR_KEY[@]+"${HDR_KEY[@]}"} \
  -d "$BODY")

JOB_ID=$(echo "$RESP" | python3 -c "import sys,json; print(json.load(sys.stdin)['job_id'])")
UP=$(echo "$RESP" | python3 -c "import sys,json; print(json.load(sys.stdin)['upload_url'])")

echo "→ PUT soubor do GCS (job $JOB_ID) …" >&2
curl -sS -o /dev/null -w "PUT %{http_code}\n" -X PUT "$UP" \
  -H "Content-Type: audio/mp4" \
  --data-binary "@$FILE" \
  --max-time 3600

FIN="$BASE_URL/v1/jobs/$JOB_ID/finalize-upload"
echo "→ POST finalize …" >&2
curl -sS -o /dev/null -w "finalize %{http_code}\n" -X POST "$FIN" \
  -d '' \
  ${HDR_KEY[@]+"${HDR_KEY[@]}"}

echo "→ čekám na stav (max ~3 hod) …" >&2
DEADLINE=$((SECONDS + 10800))
while true; do
  if (( SECONDS > DEADLINE )); then
    echo "Timeout." >&2
    exit 1
  fi
  S=$(curl -sS "$BASE_URL/v1/jobs/$JOB_ID" ${HDR_KEY[@]+"${HDR_KEY[@]}"})
  ST=$(echo "$S" | python3 -c "import sys,json; print(json.load(sys.stdin).get('status',''))")
  case "$ST" in
    completed)
      echo "$S" | python3 -m json.tool
      exit 0
      ;;
    failed)
      echo "$S" | python3 -m json.tool
      exit 1
      ;;
  esac
  echo "   … $ST ($(date +%H:%M:%S))" >&2
  sleep 8
done
