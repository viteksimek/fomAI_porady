# Meeting API — přepis a zápis z porady (Chirp 3 + Gemini)

Repozitář: [github.com/viteksimek/fomAI_porady](https://github.com/viteksimek/fomAI_porady)

FastAPI služba: přijme audio (GCS, multipart nebo signed upload), výchozí přepis přes **Speech-to-Text v2 (Chirp 3)** v multiregionu `eu`/`us`, zápis porady přes **Vertex AI Gemini**. Režim `TRANSCRIPTION_PROVIDER=vertex_gemini` vrací přepis čistě z modelu s audiem. Asynchronní zpracování přes **Cloud Tasks** nebo `PROCESS_INLINE` pro vývoj.

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
- `GET /v1/meta` — limity a doporučený tok (bez hardcodování u klienta)
- **`POST /v1/jobs/prepare-upload`** — doporučený vstup pro **libovolnou velikost**: dostanete `upload_url` → **PUT** souboru → **POST** `finalize_url` → **GET** `status_url` (viz pole `steps` v odpovědi)
- `POST /v1/jobs` — soubor už v GCS: `{ "source": { "gcs_uri": "gs://..." }, "options": { "language_hint": "cs" } }`
- `POST /v1/jobs/upload` — jen **malé** soubory (řádově pod cca 32 MB); větší vždy přes **prepare-upload** nebo `gs://`
- `GET /v1/jobs/{id}?include_signed_urls=true`
- `POST /v1/uploads/signed-url` — totéž co `prepare-upload` (zpětná kompatibilita)

Hlavička `X-API-Key` pokud je nastaveno `API_KEY`.

Volitelný počet mluvčích: v těle jobu `options.speaker_count` (1–32), nebo v názvu nahraného souboru `*_sN.*` (např. `zapis_s4.m4a`) / `*_Nmluvcich.*`. `GET /v1/meta` vrací pole `speaker_count_hint`.

## Docker

```bash
docker build -t meeting-api .
docker run --rm -p 8080:8080 --env-file .env meeting-api
```

## Nasazení (GCP)

- **Jeden příkaz (build + deploy):** `GOOGLE_CLOUD_PROJECT=… ./scripts/deploy_cloud_run.sh` — popis v [docs/CLOUD_RUN_DEPLOY.md](docs/CLOUD_RUN_DEPLOY.md).
- **Nejkratší cesta na Cloud Run:** [docs/CLOUD_RUN_DEPLOY.md](docs/CLOUD_RUN_DEPLOY.md) (build + `gcloud run deploy`, varianta bez Cloud Tasks).
- Úplný popis zdrojů a rolí: [docs/GCP_SETUP.md](docs/GCP_SETUP.md). CI/CD: [cloudbuild.yaml](cloudbuild.yaml) + trigger na Git.
