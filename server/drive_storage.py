import os
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

SCOPES = ["https://www.googleapis.com/auth/drive.file"]
FOLDER_MIME = "application/vnd.google-apps.folder"


def get_credentials():
    creds = None
    if os.path.exists("token.json"):
        creds = Credentials.from_authorized_user_file("token.json", SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file("credentials.json", SCOPES)
            creds = flow.run_local_server(port=0)
        with open("token.json", "w", encoding="utf-8") as f:
            f.write(creds.to_json())

    return creds


def escape(name):
    return name.replace("\\", "\\\\").replace("'", "\\'")


class DriveStorage:
    def __init__(self, root_name):
        self.service = build("drive", "v3", credentials=get_credentials())
        self.root_id = self.get_or_create_folder(root_name, "root")

    def find(self, name, parent_id, mime=None):
        query = f"name = '{escape(name)}' and '{parent_id}' in parents and trashed = false"
        if mime:
            query += f" and mimeType = '{mime}'"
        result = self.service.files().list(q=query, fields="files(id)").execute()
        files = result.get("files", [])
        if files:
            return files[0]["id"]
        return None

    def get_or_create_folder(self, name, parent_id):
        folder_id = self.find(name, parent_id, FOLDER_MIME)
        if folder_id:
            return folder_id
        body = {"name": name, "mimeType": FOLDER_MIME, "parents": [parent_id]}
        folder = self.service.files().create(body=body, fields="id").execute()
        return folder["id"]

    def save(self, temp_path, goreli_yol):
        parts = goreli_yol.split("/")
        if any(p in ("", ".", "..") for p in parts):
            raise ValueError("Invalid file path")

        parent_id = self.root_id
        for folder_name in parts[:-1]:
            parent_id = self.get_or_create_folder(folder_name, parent_id)

        file_name = parts[-1]
        media = MediaFileUpload(temp_path, resumable=True)
        existing_id = self.find(file_name, parent_id)

        if existing_id:
            self.service.files().update(fileId=existing_id, media_body=media).execute()
        else:
            body = {"name": file_name, "parents": [parent_id]}
            self.service.files().create(body=body, media_body=media).execute()
