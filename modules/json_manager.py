import json
from pathlib import Path


class JsonManager:
    def __init__(self, filename: str):
        self.filename = Path(filename)

    def get_json(self):
        with self.filename.open('r', encoding='utf-8') as file:
            return json.load(file)

    def save_json(self, data):
        self.filename.parent.mkdir(parents=True, exist_ok=True)
        with self.filename.open('w', encoding='utf-8') as file:
            json.dump(data, file, ensure_ascii=False, indent=2)
