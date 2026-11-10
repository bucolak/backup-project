from fastapi import FastAPI, UploadFile, File, Form, HTTPException
import os, shutil, hashlib, tempfile, json

from local_storage import LocalStorage
from drive_storage import DriveStorage
from pcloud_storage import PCloudStorage

from contextlib import asynccontextmanager
from apscheduler.schedulers.background import BackgroundScheduler

from database import initialize_database
from schedule_manager import process_due_schedules

from pydantic import BaseModel
from database import get_connection

PROVIDERS = {
    "local": LocalStorage,
    "gdrive": DriveStorage,
    "pcloud": PCloudStorage,
}

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "server_config.json")
with open(CONFIG_PATH, "r", encoding="utf-8") as f:
    config = json.load(f)

provider_class = PROVIDERS[config["provider"]]
storage = provider_class(**config.get("options", {}))

backup_scheduler = BackgroundScheduler(
    timezone="Europe/Istanbul"
)

@asynccontextmanager
async def lifespan(app: FastAPI):

    # Sunucu başlarken veritabanını hazırla
    initialize_database()

    # Sunucu kapalıyken zamanı gelen görevleri kontrol et
    process_due_schedules()

    # Zamanlama kontrolünü her 30 saniyede bir çalıştır
    backup_scheduler.add_job(
        process_due_schedules,
        trigger="interval",
        seconds=30,
        id="process_due_schedules",
        replace_existing=True,
        max_instances=1,
        coalesce=True
    )

    # Zamanlayıcıyı başlat
    backup_scheduler.start()

    yield

    # Sunucu kapanırken zamanlayıcıyı durdur
    backup_scheduler.shutdown(wait=False)


app = FastAPI(lifespan=lifespan)

class JobRequest(BaseModel):
    client_id: str

class JobCompletion(BaseModel):
    client_id: str
    success: bool

@app.post("/jobs/claim")
def claim_job(request: JobRequest):

    with get_connection() as connection:

        # Aynı bilgisayar için çalışan bir görev var mı?
        running_job = connection.execute("""
            SELECT *
            FROM backup_jobs
            WHERE client_id = ?
            AND status = 'running' 
            LIMIT 1
        """, (request.client_id,)).fetchone()

        if running_job:
            return {
                "job": None,
                "message": "A backup is already running."
            }

        # Bekleyen bir görev var mı?
        pending_job = connection.execute("""
            SELECT *
            FROM backup_jobs
            WHERE client_id = ?
            AND status = 'pending'
            ORDER BY scheduled_for
            LIMIT 1
        """, (request.client_id,)).fetchone()

        if pending_job is None:
            return {
                "job": None,
                "message": "No pending backup job."
            }

        # Görevi çalışıyor olarak işaretle
        connection.execute("""
            UPDATE backup_jobs
            SET status = 'running'
            WHERE id = ?
        """, (pending_job["id"],))

        # Güncellenmiş görevi getir
        job = connection.execute("""
            SELECT *
            FROM backup_jobs
            WHERE id = ?
        """, (pending_job["id"],)).fetchone()

        return {
            "job": dict(job)
        }

@app.post("/jobs/{job_id}/complete")
def complete_job(job_id: int, request: JobCompletion):

    status = "success" if request.success else "failed"

    with get_connection() as connection:

        cursor = connection.execute("""
            UPDATE backup_jobs
            SET status = ?
            WHERE id = ?
            AND client_id = ?
            AND status = 'running'
        """, (
            status,
            job_id,
            request.client_id
        ))

        if cursor.rowcount == 0:
            raise HTTPException(
                status_code=404,
                detail="Running job not found."
            )

    return {
        "job_id": job_id,
        "status": status
    }

@app.get("/")
def anasayfa():
    return {"mesaj": "merhaba"}

@app.post("/upload")
async def upload(dosya: UploadFile = File(...), goreli_yol = Form(...)):
    with tempfile.NamedTemporaryFile(delete=False) as tmp:
        shutil.copyfileobj(dosya.file, tmp)
        temp_path = tmp.name

    try:
        sha256 = hashlib.sha256()
        with open(temp_path, "rb") as f: 
            while True:
                parca = f.read(4096)

                if not parca:
                    break
                sha256.update(parca)
        size = os.path.getsize(temp_path)
        try:
            storage.save(temp_path, goreli_yol)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)

    return {
         "message": "File saved", 
         "file_name": goreli_yol,
         "sha256": sha256.hexdigest(),
         "size": size  }

