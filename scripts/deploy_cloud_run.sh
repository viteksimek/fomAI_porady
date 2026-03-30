#!/usr/bin/env bash
# Jeden příkaz: Docker build (Cloud Build) + nasazení na Cloud Run.
#
# Nemá přístup k tvému GCP z Cursoru — spusť lokálně (Mac + gcloud) nebo v Cloud Shell.
#
# Použití (z KOŘENE repozitáře, kde je Dockerfile):
#   export GOOGLE_CLOUD_PROJECT="fomei2020"
#   export SERVICE_NAME="fomai-porady"          # volitelné
#   export REGION="europe-west1"               # volitelné
#   export GCS_BUCKET="fomei2020-meeting-audio" # jen při prvním vytvoření služby
#   ./scripts/deploy_cloud_run.sh
#
# Nebo pozičně:
#   ./scripts/deploy_cloud_run.sh PROJECT_ID [SERVICE_NAME] [REGION]
#
# Před prvním deployem doporučeně IAM + env jednorázově:
#   ./scripts/setup_existing_cloudrun.sh PROJECT_ID SERVICE_NAME REGION
# (zapne API včetně Speech, role speech.client, env na službě.)
#
set -euo pipefail

PROJECT_ID="${GOOGLE_CLOUD_PROJECT:-${1:-}}"
SERVICE_NAME="${SERVICE_NAME:-${2:-fomai-porady}}"
REGION="${REGION:-${3:-europe-west1}}"
AR_REPO="${AR_REPO:-meeting-api}"
BUCKET_NAME="${GCS_BUCKET:-${PROJECT_ID}-meeting-audio}"

if [[ -z "$PROJECT_ID" ]]; then
  echo "Chybí PROJECT_ID. Příklad:"
  echo "  GOOGLE_CLOUD_PROJECT=fomei2020 ./scripts/deploy_cloud_run.sh"
  echo "  ./scripts/deploy_cloud_run.sh fomei2020 fomai-porady europe-west1"
  exit 1
fi

if ! command -v gcloud &>/dev/null; then
  echo "gcloud není v PATH — nainstaluj Google Cloud SDK nebo otevři Cloud Shell."
  exit 1
fi

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

gcloud config set project "$PROJECT_ID" --quiet

TAG="$(date +%Y%m%d-%H%M)"
IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${AR_REPO}/${SERVICE_NAME}:${TAG}"

echo "==================================================================="
echo "  Build:  $IMAGE"
echo "  Run:    $SERVICE_NAME  ($REGION)"
echo "==================================================================="

gcloud builds submit --tag "$IMAGE" .

if gcloud run services describe "$SERVICE_NAME" --region "$REGION" &>/dev/null; then
  echo "==> Služba už existuje — nasazuji jen nový image (env zůstane z konzole / předchozího deploye)."
  gcloud run deploy "$SERVICE_NAME" \
    --region="$REGION" \
    --platform=managed \
    --image="$IMAGE"
else
  echo "==> První deploy — vytvářím službu s výchozím výpočetním účtem a env (uprav v konzoli dle potřeby)."
  NUM="$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')"
  RUN_SA="${CLOUD_RUN_SA:-${NUM}-compute@developer.gserviceaccount.com}"
  ENV_VARS="GOOGLE_CLOUD_PROJECT=${PROJECT_ID},GCS_BUCKET=${BUCKET_NAME},MODEL_REGION=${REGION},SPEECH_REGION=eu,TRANSCRIPTION_PROVIDER=chirp_3,MODEL_TRANSCRIPT=gemini-2.5-flash,MODEL_MINUTES=gemini-2.5-flash,USE_MEMORY_STORE=false,PROCESS_INLINE=true,SKIP_INTERNAL_OIDC=true"

  gcloud run deploy "$SERVICE_NAME" \
    --region="$REGION" \
    --platform=managed \
    --allow-unauthenticated \
    --port=8080 \
    --memory=2Gi \
    --cpu=2 \
    --timeout=3600 \
    --service-account="$RUN_SA" \
    --image="$IMAGE" \
    --set-env-vars="$ENV_VARS"
fi

URL="$(gcloud run services describe "$SERVICE_NAME" --region="$REGION" --format='value(status.url)')"
echo ""
echo "-------------------------------------------------------------------"
echo "Hotovo:  $URL"
echo "Test:    curl -sS \"$URL/health\""
echo ""
echo "IAM + bucket + Firestore jednorázově (pokud ještě ne):"
echo "  ./scripts/setup_existing_cloudrun.sh $PROJECT_ID $SERVICE_NAME $REGION"
echo "-------------------------------------------------------------------"
