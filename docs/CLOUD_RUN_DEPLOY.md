# Cloud Run — nejjednodušší nasazení

## Nejrychlejší opakovaný deploy (jeden skript)

Z kořene repa (kde je `Dockerfile`), s nainstalovaným `gcloud` a přihlášením (`gcloud auth login` + `gcloud config set project …`):

```bash
chmod +x scripts/deploy_cloud_run.sh scripts/setup_existing_cloudrun.sh
# Jednorázově (API, IAM, bucket, env na službě) — viz níže, sekce „Už máte Cloud Run…“
./scripts/setup_existing_cloudrun.sh PROJECT_ID NÁZEV_SLUŽBY europe-west1

# Při každé změně kódu — build + nasazení nového image
GOOGLE_CLOUD_PROJECT=PROJECT_ID ./scripts/deploy_cloud_run.sh
```

Skript [scripts/deploy_cloud_run.sh](../scripts/deploy_cloud_run.sh): Cloud Build vyrobí image a `gcloud run deploy` ho nasadí. **Služba už existuje** → aktualizuje se jen image (**env zůstane** jako v konzoli). **Služba neexistuje** → první deploy vytvoří službu s rozumným výchozím `set-env-vars` (případně pak uprav env v konzoli).

_Asistent v editoru k tvému GCP nepřistupuje — příkazy musí spustit ty nebo [Cloud Shell](https://shell.cloud.google.com/) ve stejném projektu._

**Máš Cloud Build trigger z GitHubu?** Repozitář si Cloud Build **naklonuje sám** při každém buildu — ruční `git clone` nepotřebuješ. Nasazení = **push do větve**, kterou trigger sleduje (viz sekce [Git → automatický build](#git--automatický-build)). Skript `deploy_cloud_run.sh` používá jen tehdy, když build spouštíš ručně z vlastního klónu.

---

Dvě úrovně: **A) bez Cloud Tasks** (méně kroků, vhodné na začátek) a **B) s Cloud Tasks** (odpovídá plánu, robustnější).

## Předpoklady

- [Google Cloud SDK](https://cloud.google.com/sdk/docs/install) (`gcloud`)
- Projekt GCP s fakturací
- Tento repozitář na disku (kořen s `Dockerfile`)

Proměnné (upravte):

```bash
export PROJECT_ID="váš-projekt-id"
export REGION="europe-west1"
export SERVICE_NAME="meeting-api"
export BUCKET_NAME="${PROJECT_ID}-meeting-audio"
export AR_REPO="meeting-api"
```

### Už máte Cloud Run, chybí bucket / Firestore / IAM / env?

V **Cloud Shell**, na **Macu v kořeni repa**, nebo kde máš `gcloud` s právy na projekt (po `git clone` jen když něco spouštíš ručně; u CI triggeru si repo stáhne sám Cloud Build):

```bash
chmod +x scripts/setup_existing_cloudrun.sh
./scripts/setup_existing_cloudrun.sh fomei2020 fomai-porady europe-west1
```

Argumenty: `PROJECT_ID`, **název služby Cloud Run** (jak v konzoli), volitelně region. Skript zapne API, založí bucket `PROJECT_ID-meeting-audio` (nebo `BUCKET_NAME=…` před spuštěním), zkusí Firestore, **najde účet**, pod kterým služba běží, přidá mu role (Vertex, Firestore, Storage, …) a **doplní env** na službě (`GCS_BUCKET`, `USE_MEMORY_STORE=false`, …).

**Cache u `curl`:** pokud stále hlásí chybu o `--condition=None`, ověřte `curl -sL 'https://raw.githubusercontent.com/.../setup_existing_cloudrun.sh?x=1' | head -5` — musí být řádek `rev: 2026-03-30b`. Jinak přidejte `?t=$(date +%s)` do URL.

**Ruční IAM** (stejné role jako skript), když skript nelze spustit:

```bash
export PROJECT_ID=fomei2020
export RUN_SA=635664358681-compute@developer.gserviceaccount.com   # váš účet z výpisu skriptu
for R in roles/aiplatform.user roles/datastore.user roles/storage.objectAdmin roles/cloudtasks.enqueuer roles/speech.client; do
  gcloud projects add-iam-policy-binding "$PROJECT_ID" --member="serviceAccount:${RUN_SA}" --role="$R" --condition=None --quiet
done
```

Pak dokončete env na Cloud Run (viz skript, sekce `gcloud run services update` / zkopírujte z aktuálního `setup_existing_cloudrun.sh` na GitHubu).

### Rychlá příprava skriptem (kroky 1–3 najednou)

Ze **kořene repozitáře** (s `Dockerfile`), po `gcloud auth login` a zapnuté fakturaci:

```bash
chmod +x scripts/bootstrap_gcp.sh
./scripts/bootstrap_gcp.sh "$PROJECT_ID"
```

Skript zapne API, vytvoří bucket, Artifact Registry, runtime účet, role, Firestore (Native) a zkusí frontu `meeting-jobs`. Na konci vypíše příkazy pro `gcloud builds submit` a `gcloud run deploy`.

---

## Krok 1 — API a bucket (jednou)

```bash
gcloud config set project "$PROJECT_ID"

gcloud services enable \
  run.googleapis.com \
  artifactregistry.googleapis.com \
  storage.googleapis.com \
  firestore.googleapis.com \
  aiplatform.googleapis.com

gsutil mb -l "$REGION" "gs://${BUCKET_NAME}" 2>/dev/null || true

gcloud artifacts repositories create "$AR_REPO" \
  --repository-format=docker \
  --location="$REGION" \
  --description="Meeting API" 2>/dev/null || true
```

## Krok 2 — Firestore (jednou)

V [konzoli Firestore](https://console.cloud.google.com/firestore) vytvořte databázi v **Native** režimu, region např. `europe-west1`.

## Krok 3 — Služební účet pro Cloud Run (jednou)

```bash
gcloud iam service-accounts create meeting-api-run \
  --display-name="Meeting API Cloud Run" 2>/dev/null || true

SA_EMAIL="meeting-api-run@${PROJECT_ID}.iam.gserviceaccount.com"

for R in roles/aiplatform.user roles/datastore.user roles/storage.objectAdmin; do
  gcloud projects add-iam-policy-binding "$PROJECT_ID" \
    --member="serviceAccount:${SA_EMAIL}" --role="$R" --quiet
done
```

Podepisování **signed URL** (volitelné endpointy): přidejte účtu roli `roles/iam.serviceAccountTokenCreator` na sebe (běžný vzor pro V4 URL):

```bash
gcloud iam service-accounts add-iam-policy-binding "$SA_EMAIL" \
  --member="serviceAccount:${SA_EMAIL}" \
  --role="roles/iam.serviceAccountTokenCreator" --quiet
```

---

## Varianta A — nejméně kroků (bez Cloud Tasks)

Zpracování běží na instanci Cloud Run na pozadí (`PROCESS_INLINE=true`). Na Cloud Run je pro to nutné mít **always-on CPU** (`--no-cpu-throttling`), jinak může job zůstat dlouho ve stavu `processing`.

Z kořene repozitáře:

```bash
IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${AR_REPO}/${SERVICE_NAME}:$(date +%Y%m%d-%H%M)"

gcloud builds submit --tag "$IMAGE" .

gcloud run deploy "$SERVICE_NAME" \
  --image "$IMAGE" \
  --region "$REGION" \
  --platform managed \
  --allow-unauthenticated \
  --port 8080 \
  --memory 2Gi \
  --cpu 2 \
  --timeout 3600 \
  --no-cpu-throttling \
  --service-account "$SA_EMAIL" \
  --set-env-vars "\
GOOGLE_CLOUD_PROJECT=${PROJECT_ID},\
GCS_BUCKET=${BUCKET_NAME},\
MODEL_REGION=${REGION},\
USE_MEMORY_STORE=false,\
PROCESS_INLINE=true,\
SKIP_INTERNAL_OIDC=true"
```

Po deployi zkopírujte URL služby z výstupu a otestujte:

```bash
gcloud run services update "$SERVICE_NAME" --region "$REGION" --no-cpu-throttling
```


```bash
curl -sS "https://YOUR-SERVICE-XXXX.run.app/health"
```

Hlavička `X-API-Key` jen pokud v konzoli Cloud Run doplníte proměnnou `API_KEY`.

---

## Varianta B — s Cloud Tasks (dle plánu)

1. Dokončete sekci **6** v [GCP_SETUP.md](GCP_SETUP.md) (fronta `meeting-jobs`, účet `cloud-tasks-invoker`, `roles/run.invoker` na službě).
2. Deploy stejný image jako v A, ale env např.:

```text
PROCESS_INLINE=false
SKIP_INTERNAL_OIDC=false
CLOUD_RUN_SERVICE_URL=https://YOUR-SERVICE-XXXX.run.app
CLOUD_TASKS_LOCATION=europe-west1
CLOUD_TASKS_QUEUE=meeting-jobs
CLOUD_TASKS_INVOKER_SA=cloud-tasks-invoker@PROJECT_ID.iam.gserviceaccount.com
```

A runtime účtu přidejte `roles/cloudtasks.enqueuer` (viz [GCP_SETUP.md](GCP_SETUP.md)).

---

## Git → automatický build

Zdrojový kód: [https://github.com/viteksimek/fomAI_porady](https://github.com/viteksimek/fomAI_porady).

Propoj repo s [Cloud Build Triggers](https://console.cloud.google.com/cloud-build/triggers) a jako konfiguraci zvol [cloudbuild.yaml](../cloudbuild.yaml) v kořeni. **Při spuštění triggeru Cloud Build repozitář sám naklonuje** (do pracovního adresáře buildu) — v Cloud Shellu nemusíš nic klonovat jen kvůli deployi z Gitu.

Typický postup: **push do sledované větve** → build → deploy podle `cloudbuild.yaml`. Jednorázově spusť [setup_existing_cloudrun.sh](../scripts/setup_existing_cloudrun.sh) (IAM, API, env na Cloud Run), aby služba měla `GCS_BUCKET`, `SPEECH_REGION`, `TRANSCRIPTION_PROVIDER` atd. — šablona v `cloudbuild.yaml` často přepisuje jen část env (`PROCESS_INLINE` atd.), zbytek doplň v konzoli nebo přes skript.

---

## Velké soubory a automatizace (doporučený jednotný tok)

**Nepřemýšlejte o limitech ručně** — klient si může vždy vyžádat `GET /v1/meta` (limity + doporučená cesta).

**Standard pro libovolnou velikost** (Power Automate, skripty, …):

**Import do Power Automate:** soubor [meeting-api.swagger.yaml](../integrations/power-automate/meeting-api.swagger.yaml) (OpenAPI/Swagger 2.0) lze v **Power Automate → Data → Custom connectors → Import an OpenAPI file** načíst jako konektor akcí `PrepareUpload`, `FinalizeUpload`, `GetJobStatus`, `GetMeta`. Krok **PUT** binárního souboru na `upload_url` zůstává jako samostatná akce **HTTP** (jiný host než Cloud Run).

1. `POST /v1/jobs/prepare-upload` — JSON `filename`, `content_type`, volitelně `options` — odpověď obsahuje `upload_url`, `finalize_url`, `status_url` a pole **`steps`**.
2. **PUT** raw bajtů souboru na `upload_url` (hlavička `Content-Type` jako v kroku 1).
3. **POST** na `finalize_url`.
4. **GET** `status_url` dokud není hotovo.

Starý alias: `POST /v1/uploads/signed-url` dělá totéž.

**Cloud Run** má limit **cca 32 MB** na přímý multipart **`/v1/jobs/upload`** — pro větší soubory používejte výše uvedený tok nebo soubor nejdřív dejte do GCS a **`POST /v1/jobs`** s `gs://...`.

V **Cloud Shellu** není váš Mac **`~/Desktop`** — soubor nejdřív **Upload** do home, pak např. `gcloud storage cp ~/SLS.m4a gs://...`.

## Finální checklist před testem z Macu (Chirp + Gemini)

1. **Zapnuté API v projektu** (jinak 403 u přepisu):
   - V konzoli: [API knihovna — Speech-to-Text](https://console.cloud.google.com/apis/library/speech.googleapis.com) (vyber projekt) → **Enable**,
   - nebo v Cloud Shell:  
     `gcloud config set project PROJECT_ID`  
     `gcloud services enable speech.googleapis.com`
   - Po zapnutí počkej 1–3 minuty a job spusť znovu.
2. **Runtime účet Cloud Run** (konfigurace služby → účet služby): má mít mimo jiné **`roles/speech.client`**, **`roles/aiplatform.user`**, **`roles/storage.objectAdmin`** — viz [setup_existing_cloudrun.sh](../scripts/setup_existing_cloudrun.sh).
3. **Env na Cloud Run** (nejnovější revize):  
   `TRANSCRIPTION_PROVIDER=chirp_3`, `SPEECH_REGION=eu`,  
   `MODEL_MINUTES` / `MODEL_TRANSCRIPT` = např. **`gemini-2.5-flash`** (ne staré `gemini-2.0-flash-001`, pokud v projektu nejedete).
4. **Test z kořene repa:**
   ```bash
   cd /cesta/k/klonu
   BASE_URL="https://TVOJE-SLUZBA.run.app" ./scripts/upload_and_wait.sh "/Users/ja/Desktop/SLS.m4a"
   ```
   Volitelně `export API_KEY=…`, pokud ho máš na službě.

**Co znamená tvoje chyba `SERVICE_DISABLED` u Speech:** API **speech.googleapis.com** v projektu **není zapnuté** (nebo ještě „nepropadlo“). Není to chyba kódu ani souboru — po **Enable** a krátké prodlevě stejný upload znovu.

## Časté problémy

| Problém | Řešení |
|--------|--------|
| **`403` Cloud Speech-to-Text API … `SERVICE_DISABLED`** | Zapni API: [speech.googleapis.com v API Library](https://console.cloud.google.com/apis/library/speech.googleapis.com) u daného projektu, nebo `gcloud services enable speech.googleapis.com`. Po minutě opakuj job. |
| **Chirp: „audio … too long“, max ~60 min / soubor** | Aplikace dílčí delší soubory automaticky (~55 min úseky) a spojí přepis; nasaď novou revizi služby. |
| Vertex „permission denied“ / 404 modelu | Účet Cloud Run má `roles/aiplatform.user`; v env `MODEL_*` použij dostupný model (např. `gemini-2.5-flash`). |
| GCS access denied | `roles/storage.objectAdmin` na projekt nebo užší role na bucket. |
| Firestore | `USE_MEMORY_STORE=false` a vytvořená Firestore DB. |
| Timeout | U dlouhých nahrávek už máte `--timeout 3600`; případně zvyšte paměť. |

Podrobnosti rolí a OIDC: [GCP_SETUP.md](GCP_SETUP.md).
