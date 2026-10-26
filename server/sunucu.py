from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from pathlib import Path
import os, shutil, hashlib

app = FastAPI()

@app.get("/")
def anasayfa():
    return {"mesaj": "merhaba"}

@app.post("/upload")
async def upload(dosya: UploadFile = File(...), goreli_yol = Form(...)):
    base = Path("received_files").resolve()
    file_path = (base / goreli_yol).resolve()
    print("burada: ", file_path)
    if not file_path.is_relative_to(base):
        raise HTTPException(status_code=400, detail="Invalid file path")
    os.makedirs(file_path.parent, exist_ok = True)
    with open(file_path, "wb") as dst:
        shutil.copyfileobj(dosya.file, dst)

    sha256 = hashlib.sha256()
    with open(file_path, "rb") as saved_file: 
        while True:
            parca = saved_file.read(4096)

            if not parca:
                break
            sha256.update(parca)
    size = os.path.getsize(file_path)
    return {
         "message": "File saved", 
         "file_name": dosya.filename,
         "sha256": sha256.hexdigest(),
         "size": size  }

