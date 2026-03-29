#!/usr/bin/env bash
# Nastavení GCS + Firestore + Vertex API + IAM pro ÚČET, který už používá běžící Cloud Run,
# a doplnění env proměnných na službě.
#
# Použití (Cloud Shell nebo počítač s gcloud):
#   chmod +x scripts/setup_existing_cloudrun.sh
#   ./scripts/setup_existing_cloudrun.sh PROJECT_ID CLOUD_RUN_SERVICE_NAME [REGION]
#
# Příklad:
#   ./scripts/setup_existing_cloudrun.sh fomei2020 fomai-porady europe-west1

set -euo pipefail

PROJECT_ID="${1:-}"
SERVICE_NAME="${2:-}"
REGION="${3:-${REGION:-europe-west1}}"
BUCKET_NAME="${BUCKET_NAME:-${PROJECT_ID}-meeting-audio}"

if [[ -z "$PROJECT_ID" || -z "$SERVICE_NAME" ]]; then
  echo "Použití:  $0 PROJECT_ID CLOUD_RUN_SERVICE_NAME [REGION]"
  echo "Příklad:  $0 fomei2020 fomai-porady europe-west1"
  exit 1
fi

echo "==> Projekt: $PROJECT_ID  Cloud Run služba: $SERVICE_NAME  region: $REGION"
gcloud config set project "$PROJECT_ID" --quiet

echo "==> Zapínám API (idempotentní)…"
gcloud services enable \
  run.googleapis.com \
  storage.googleapis.com \
  firestore.googleapis.com \
  aiplatform.googleapis.com \
  cloudbuild.googleapis.com \
  artifactregistry.googleapis.com \
  --quiet

echo "==> Bucket: gs://${BUCKET_NAME}"
if gsutil ls -b "gs://${BUCKET_NAME}" &>/dev/null; then
  echo "    (už existuje)"
else
  gsutil mb -l "$REGION" "gs://${BUCKET_NAME}"
fi

echo "==> Firestore (default), Native…"
if gcloud firestore databases create --location="$REGION" --type=firestore-native --quiet 2>/dev/null; then
  echo "    vytvořena"
else
  echo "    (pravděpodobně už existuje — OK)"
fi

echo "==> Zjišťuji služební účet Cloud Run…"
RUN_SA="$(gcloud run services describe "$SERVICE_NAME" \
  --region="$REGION" \
  --format='value(spec.template.spec.serviceAccountName)' 2>/dev/null || true)"

if [[ -z "$RUN_SA" ]]; then
  NUM="$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')"
  RUN_SA="${NUM}-compute@developer.gserviceaccount.com"
  echo "    (služba nemá vlastní SA — používám výchozí Compute: $RUN_SA)"
else
  echo "    $RUN_SA"
fi

grant_roles() {
  local sa="$1"
  echo "==> IAM role pro $sa …"
  for R in roles/aiplatform.user roles/datastore.user roles/storage.objectAdmin roles/cloudtasks.enqueuer; do
    gcloud projects add-iam-policy-binding "$PROJECT_ID" \
      --member="serviceAccount:${sa}" \
      --role="$R" \
      --quiet
  done
  echo "==> Signed URL: TokenCreator pro vlastní účet…"
  gcloud iam service-accounts add-iam-policy-binding "$sa" \
    --member="serviceAccount:${sa}" \
    --role="roles/iam.serviceAccountTokenCreator" \
    --quiet 2>/dev/null || echo "    (už nastaveno nebo nelze — zkontrolujte v IAM)"
}

grant_roles "$RUN_SA"

echo "==> Aktualizuji env proměnné na Cloud Run (doplňuje / přepisuje uvedené klíče)…"
# Jedna řádka — čárky oddělují páry KEY=VALUE
ENV_LINE="GOOGLE_CLOUD_PROJECT=${PROJECT_ID},GCS_BUCKET=${BUCKET_NAME},MODEL_REGION=${REGION},MODEL_TRANSCRIPT=gemini-2.0-flash-001,MODEL_MINUTES=gemini-2.0-flash-001,USE_MEMORY_STORE=false,PROCESS_INLINE=true,SKIP_INTERNAL_OIDC=true"

gcloud run services update "$SERVICE_NAME" \
  --region="$REGION" \
  --update-env-vars="$ENV_LINE"

echo ""
echo "-------------------------------------------------------------------"
echo "Hotovo."
echo "URL služby:"
gcloud run services describe "$SERVICE_NAME" --region="$REGION" --format='value(status.url)'
echo ""
echo "Test: curl \$(gcloud run services describe $SERVICE_NAME --region=$REGION --format='value(status.url)')/health"
echo "Volitelně v konzoli Cloud Run přidejte API_KEY a nastavte X-API-Key u klientů."
echo "-------------------------------------------------------------------"
