import json, os, time, requests
from requests_toolbelt.multipart.encoder import MultipartEncoder, MultipartEncoderMonitor


class PCloudStorage:
    def __init__(self, root_name, token_file="pcloud_token.json"):
        with open(token_file, "r", encoding="utf-8") as f:
            token = json.load(f)
        self.api = token["api"]
        self.headers = {"Authorization": "Bearer " + token["access_token"]}
        self.root_id = self.get_or_create_folder(root_name, 0)

    def call(self, method, params):
        url = self.api + "/" + method
        response = requests.get(url, params=params, headers=self.headers)
        data = response.json()
        if data["result"] != 0:
            raise RuntimeError("pCloud error: " + str(data.get("error")))
        return data

    def get_or_create_folder(self, name, parent_id):
        data = self.call("createfolderifnotexists", {"folderid": parent_id, "name": name})
        return data["metadata"]["folderid"]

    def upload_with_progress(self, temp_path, parent_id, file_name):
        total_mb = os.path.getsize(temp_path) / 1024 / 1024
        start = time.time()
        last_print = {"time": 0}

        def on_progress(monitor):
            now = time.time()
            finished = monitor.bytes_read >= monitor.len
            if now - last_print["time"] < 2 and not finished:
                return
            last_print["time"] = now
            done_mb = monitor.bytes_read / 1024 / 1024
            percent = monitor.bytes_read * 100 // monitor.len
            speed = done_mb / (now - start) if now > start else 0
            print(f"  {file_name}: {done_mb:.1f}/{total_mb:.1f} MB ({percent}%) {speed:.2f} MB/s", flush=True)

        print(f"Uploading to pCloud: {file_name} ({total_mb:.1f} MB)", flush=True)
        with open(temp_path, "rb") as f:
            encoder = MultipartEncoder(fields={"file": (file_name, f)})
            monitor = MultipartEncoderMonitor(encoder, on_progress)
            response = requests.post(
                self.api + "/uploadfile",
                params={"folderid": parent_id, "filename": file_name, "nopartial": 1},
                data=monitor,
                headers={**self.headers, "Content-Type": monitor.content_type},
            )

        data = response.json()
        if data["result"] != 0:
            raise RuntimeError("pCloud error: " + str(data.get("error")))
        print(f"Done: {file_name} in {time.time() - start:.1f} s", flush=True)

    def save(self, temp_path, goreli_yol):
        parts = goreli_yol.split("/")
        if any(p in ("", ".", "..") for p in parts):
            raise ValueError("Invalid file path")

        parent_id = self.root_id
        for folder_name in parts[:-1]:
            parent_id = self.get_or_create_folder(folder_name, parent_id)

        file_name = parts[-1]
        self.upload_with_progress(temp_path, parent_id, file_name)
