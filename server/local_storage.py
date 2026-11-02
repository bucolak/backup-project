from pathlib import Path
import os, shutil


class LocalStorage:
    def __init__(self, base_dir):
        self.base = Path(base_dir).resolve()

    def save(self, temp_path, goreli_yol):
        target = (self.base / goreli_yol).resolve()
        if not target.is_relative_to(self.base):
            raise ValueError("Invalid file path")
        os.makedirs(target.parent, exist_ok=True)
        shutil.move(temp_path, target)
