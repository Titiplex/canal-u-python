import json
from typing import Any


class JsonManager:
    def __init__(self, filename: str):
        self.filename = filename

    def get_json(self) -> Any:
        with open(self.filename, 'r') as json_file:
            data = json.load(json_file)
            return data

    def save_json(self, data):
        with open(self.filename, 'w') as outfile:
            json.dump(data, outfile)
