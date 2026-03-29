# GCP zdroje pro Meeting API

Shrnutí prostředků a rolí odpovídajících plánu (GCS, Firestore, Cloud Tasks, Cloud Run, Vertex AI).

## 1. Projekt a API

```bash
export PROJECT_ID=your-project
export REGION=europe-west1
gcloud config set project "$PROJECT_ID"
gcloud services enable \
  run.googleapis.com \
  cloudbuild.googleapis.com \
  artifactregistry.googleapis.com \
  storage.googleapis.com \
  firestore.googleapis.com \
  cloudtasks.googleapis.com \
  aiplatform.googleapis.com
```

## 2. Firestore

V konzoli vytvořte databázi Firestore (Native mode) v tom istém regionu jako Cloud Run (doporučeno `europe-west1`). Kolekce `meeting_jobs` vznikne při prvním zápisu z aplikace.

## 3. Bucket GCS

```bash
gsutil mb -l "$REGION" "gs://${PROJECT_ID}-meeting-audio"
```

## 4. Artifact Registry

```bash
gcloud artifacts repositories create meeting-api \
  --repository-format=docker \
  --location="$REGION" \
  --description="Meeting API images"
```

## 5. Služební účet pro Cloud Run

```bash
gcloud iam service-accounts create meeting-api-run \
  --display-name="Meeting API Cloud Run"

SA="meeting-api-run@${PROJECT_ID}.iam.gserviceaccount.com"

for R in \
  roles/aiplatform.user \
  roles/datastore.user \
  roles/storage.objectAdmin \
  roles/cloudtasks.enqueuer
do
  gcloud projects add-iam-policy-binding "$PROJECT_ID" \
    --member="serviceAccount:${SA}" --role="$R"
done
```

U účtu, který podepisuje **V4 signed URL** (generování upload/download odkazů), musí být dostatečné oprávnění k bucketu a ideálně `roles/iam.serviceAccountTokenCreator` na sebe samého jen pokud používáte specifický vzor; typicky `storage.objectAdmin` na bucket + správný `GOOGLE_APPLICATION_CREDENTIALS` nebo výchozí identita na Run stačí pro `signBlob` přes služební účet Run.

## 6. Cloud Tasks — fronta a volající (OIDC)

```bash
gcloud tasks queues create meeting-jobs --location="$REGION"
```

Invoker účet (token pro POST na `/internal/tasks/process`):

```bash
gcloud iam service-accounts create cloud-tasks-invoker \
  --display-name="Cloud Tasks -> Run OIDC"

INVOKER="cloud-tasks-invoker@${PROJECT_ID}.iam.gserviceaccount.com"

gcloud run services add-iam-policy-binding meeting-api \
  --region="$REGION" \
  --member="serviceAccount:${INVOKER}" \
  --role="roles/run.invoker"
```

V prostředí Cloud Run nastavte:

- `CLOUD_TASKS_INVOKER_SA` = `$INVOKER`
- `CLOUD_RUN_SERVICE_URL` = URL služby (např. `https://meeting-api-...run.app`)
- `PROCESS_INLINE` = `false`
- `SKIP_INTERNAL_OIDC` = `false`

## 7. Cloud Build trigger

- Propojte Git repozitář v [Cloud Build Triggers](https://cloud.google.com/build/docs/automating-builds/create-github-app-triggers).
- Přiřaďte Cloud Build service account oprávnění k push do Artifact Registry a deploy na Cloud Run (`roles/run.admin`, `roles/artifactregistry.writer`, `roles/iam.serviceAccountUser` na runtime SA).

V `cloudbuild.yaml` odkomentujte / doplňte `--service-account` u `gcloud run deploy` na váš runtime SA (`meeting-api-run@...`).

## 8. Proměnné prostředí Cloud Run (doplnění po deployi)

Minimálně:

| Proměnná | Popis |
|----------|--------|
| `GOOGLE_CLOUD_PROJECT` | ID projektu |
| `GCS_BUCKET` | Název bucketu s audio |
| `MODEL_REGION` | Region Vertex AI (např. `europe-west1`) |
| `GCS_JOBS_PREFIX` | Volitelné, výchozí `jobs` |
| `PROCESS_INLINE` | `false` v produkci s Tasks |
| `CLOUD_RUN_SERVICE_URL` | Veřejná URL služby (audience OIDC) |
| `CLOUD_TASKS_QUEUE` | `meeting-jobs` |
| `CLOUD_TASKS_LOCATION` | Shodně s frontou |
| `CLOUD_TASKS_INVOKER_SA` | E-mail invoker SA |
| `SKIP_INTERNAL_OIDC` | `false` v produkci |
| `API_KEY` | Doporučeno pro `/v1/*`; hlavička `X-API-Key` |

Viz také [.env.example](../.env.example).

## 9. Lokální vývoj

- `gcloud auth application-default login`
- `USE_MEMORY_STORE=true`, `PROCESS_INLINE=true`, vyplnit `GCS_BUCKET` a `GOOGLE_CLOUD_PROJECT`
- Spuštění: `PYTHONPATH=src uvicorn app.main:app --reload --port 8080`

Signed URL ve vývoji vyžadují funkční identitu služby s oprávněním podepisovat (ADC).
