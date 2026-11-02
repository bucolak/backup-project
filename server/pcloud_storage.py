import json, requests


class PCloudStorage:
    def __init__(self, root_name, token_file="pcloud_token.json"):
        with open(token_file, "r", encoding="utf-8") as f:
            token = json.load(f)
        self.api = token["api"]
        self.headers = {"Authorization": "Bearer " + token["access_token"]}
        self.root_id = self.get_or_create_folder(root_name, 0)

    def call(self, method, params, files=None):
        url = self.api + "/" + method
        if files:
            response = requests.post(url, params=params, files=files, headers=self.headers)
        else:
            response = requests.get(url, params=params, headers=self.headers)
        data = response.json()
        if data["result"] != 0:
            raise RuntimeError("pCloud error: " + str(data.get("error")))
        return data

    def get_or_create_folder(self, name, parent_id):
        data = self.call("createfolderifnotexists", {"folderid": parent_id, "name": name})
        return data["metadata"]["folderid"]

    def save(self, temp_path, goreli_yol):
        parts = goreli_yol.split("/")
        if any(p in ("", ".", "..") for p in parts):
            raise ValueError("Invalid file path")

        parent_id = self.root_id
        for folder_name in parts[:-1]:
            parent_id = self.get_or_create_folder(folder_name, parent_id)

        file_name = parts[-1]
        with open(temp_path, "rb") as f:
            self.call(
                "uploadfile",
                {"folderid": parent_id, "filename": file_name, "nopartial": 1},
                files={"file": (file_name, f)},
            )
