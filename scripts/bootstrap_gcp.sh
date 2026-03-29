#!/usr/bin/env bash
# Jednorázová příprava GCP pro Meeting API (krok „2“ z návodu).
# Spusťte lokálně:  bash scripts/bootstrap_gcp.sh VÁŠ_PROJECT_ID
#
# Potřebujete: gcloud nainstalovaný, přihlášení (gcloud auth login),
# zapnutou fakturaci a role Owner / Editor nebo ekvivalentní oprávnění.

set -euo pipefail

PROJECT_ID="${1:-${GOOGLE_CLOUD_PROJECT:-}}"
if [[ -z "$PROJECT_ID" ]]; then
  echo "Použití:  $0 PROJECT_ID"
  echo "Nebo nastavte GOOGLE_CLOUD_PROJECT."
  exit 1
fi

REGION="${REGION:-europe-west1}"
SERVICE_NAME="${SERVICE_NAME:-meeting-api}"
AR_REPO="${AR_REPO:-meeting-api}"
BUCKET_NAME="${BUCKET_NAME:-${PROJECT_ID}-meeting-audio}"
SA_NAME="${SA_NAME:-meeting-api-run}"

SA_EMAIL="${SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"

echo "==> Projekt: $PROJECT_ID  region: $REGION"
gcloud config set project "$PROJECT_ID" --quiet

echo "==> Zapínám API…"
gcloud services enable \
  run.googleapis.com \
  cloudbuild.googleapis.com \
  artifactregistry.googleapis.com \
  storage.googleapis.com \
  firestore.googleapis.com \
  cloudtasks.googleapis.com \
  aiplatform.googleapis.com \
  iamcredentials.googleapis.com \
  --quiet

echo "==> GCS bucket: gs://${BUCKET_NAME}"
if gsutil ls -b "gs://${BUCKET_NAME}" &>/dev/null; then
  echo "    (už existuje)"
else
  gsutil mb -l "$REGION" "gs://${BUCKET_NAME}"
fi

echo "==> Artifact Registry: ${AR_REPO} (${REGION})"
if gcloud artifacts repositories describe "$AR_REPO" --location="$REGION" &>/dev/null; then
  echo "    (už existuje)"
else
  gcloud artifacts repositories create "$AR_REPO" \
    --repository-format=docker \
    --location="$REGION" \
    --description="Meeting API images"
fi

echo "==> Služební účet: ${SA_EMAIL}"
if gcloud iam service-accounts describe "$SA_EMAIL" &>/dev/null; then
  echo "    (už existuje)"
else
  gcloud iam service-accounts create "$SA_NAME" \
    --display-name="Meeting API Cloud Run"
fi

echo "==> IAM role pro runtime účet…"
for R in roles/aiplatform.user roles/datastore.user roles/storage.objectAdmin roles/cloudtasks.enqueuer; do
  gcloud projects add-iam-policy-binding "$PROJECT_ID" \
    --member="serviceAccount:${SA_EMAIL}" \
    --role="$R" \
    --condition=None \
    --quiet
done

echo "==> Signed URL (V4): TokenCreator na vlastní účet…"
gcloud iam service-accounts add-iam-policy-binding "$SA_EMAIL" \
  --member="serviceAccount:${SA_EMAIL}" \
  --role="roles/iam.serviceAccountTokenCreator" \
  --quiet

echo "==> Firestore (default), Native…"
if gcloud firestore databases create --location="$REGION" --type=firestore-native --quiet; then
  echo "    vytvořena"
else
  echo "    (pravděpodobně už existuje — pokud ne, dokončete v konzoli Firestore)"
fi

echo "==> Cloud Tasks fronta meeting-jobs (pro pozdější použití)…"
if gcloud tasks queues describe meeting-jobs --location="$REGION" &>/dev/null; then
  echo "    (už existuje)"
else
  gcloud tasks queues create meeting-jobs --location="$REGION" || true
fi

IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${AR_REPO}/${SERVICE_NAME}:bootstrap-$(date +%Y%m%d%H%M)"

echo ""
echo "-------------------------------------------------------------------"
echo "Hotovo. Další krok (build + deploy) spusťte ze KOŘENE repozitáře:"
echo ""
echo "  export PROJECT_ID=\"${PROJECT_ID}\""
echo "  export REGION=\"${REGION}\""
echo "  export SERVICE_NAME=\"${SERVICE_NAME}\""
echo "  export BUCKET_NAME=\"${BUCKET_NAME}\""
echo "  export SA_EMAIL=\"${SA_EMAIL}\""
echo ""
echo "  IMAGE=\"${IMAGE}\""
echo "  gcloud builds submit --tag \"\$IMAGE\" ."
echo ""
echo "  gcloud run deploy \"\$SERVICE_NAME\" \\"
echo "    --image \"\$IMAGE\" \\"
echo "    --region \"\$REGION\" \\"
echo "    --platform managed \\"
echo "    --allow-unauthenticated \\"
echo "    --port 8080 \\"
echo "    --memory 2Gi \\"
echo "    --cpu 2 \\"
echo "    --timeout 3600 \\"
echo "    --service-account \"\$SA_EMAIL\" \\"
echo "    --set-env-vars \"GOOGLE_CLOUD_PROJECT=\${PROJECT_ID},GCS_BUCKET=\${BUCKET_NAME},MODEL_REGION=\${REGION},USE_MEMORY_STORE=false,PROCESS_INLINE=true,SKIP_INTERNAL_OIDC=true\""
echo ""
echo "Po deployi nastavte volitelně API_KEY v Cloud Run → proměnné prostředí."
echo "Cloud Tasks + OIDC: viz docs/GCP_SETUP.md"
echo "-------------------------------------------------------------------"
