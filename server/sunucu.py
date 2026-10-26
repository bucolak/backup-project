from fastapi import FastAPI, UploadFile, File, Form
import os, shutil, hashlib

app = FastAPI()

@app.get("/")
def anasayfa():
    return {"mesaj": "merhaba"}

@app.post("/upload")
async def upload(dosya: UploadFile = File(...), goreli_yol = Form(...)):
    file_path = os.path.join("received_files", goreli_yol)
    os.makedirs(os.path.dirname(file_path), exist_ok = True)
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

// deneme bak
