# Meeting API — přepis a zápis z porady (Vertex Gemini)

Repozitář: [github.com/viteksimek/fomAI_porady](https://github.com/viteksimek/fomAI_porady)

FastAPI služba: přijme audio (GCS, multipart nebo signed upload), přes **Vertex AI Gemini** vytvoří strukturovaný přepis a zápis porady. Asynchronní zpracování přes **Cloud Tasks** nebo režim `PROCESS_INLINE` pro vývoj.

## Rychlý start (lokálně)

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# upravte .env — ADC: gcloud auth application-default login
export PYTHONPATH=src
uvicorn app.main:app --reload --host 0.0.0.0 --port 8080
```

- `GET /health` — kontrola běhu
- `POST /v1/jobs` — JSON `{ "source": { "gcs_uri": "gs://..." }, "options": { "language_hint": "cs" } }`
- `POST /v1/jobs/upload` — `multipart/form-data` pole `file`, volitelně `options` (JSON string)
- `GET /v1/jobs/{id}?include_signed_urls=true`
- Volitelně `POST /v1/uploads/signed-url` pak `POST /v1/jobs/{id}/finalize-upload`

Hlavička `X-API-Key` pokud je nastaveno `API_KEY`.

## Docker

```bash
docker build -t meeting-api .
docker run --rm -p 8080:8080 --env-file .env meeting-api
```

## Nasazení (GCP)

- **Nejkratší cesta na Cloud Run:** [docs/CLOUD_RUN_DEPLOY.md](docs/CLOUD_RUN_DEPLOY.md) (build + `gcloud run deploy`, varianta bez Cloud Tasks).
- Úplný popis zdrojů a rolí: [docs/GCP_SETUP.md](docs/GCP_SETUP.md). CI/CD: [cloudbuild.yaml](cloudbuild.yaml) + trigger na Git.
