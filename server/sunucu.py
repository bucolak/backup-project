from fastapi import FastAPI, UploadFile, File, Form, HTTPException
import os, shutil, hashlib, tempfile, json

from local_storage import LocalStorage
from drive_storage import DriveStorage
from pcloud_storage import PCloudStorage

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

app = FastAPI()

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

