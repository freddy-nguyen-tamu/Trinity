import os
import json

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
folder_path = os.path.join(BASE_DIR, "finished")
output_file = os.path.join(BASE_DIR, "filenames.json")

file_names = [
    name for name in os.listdir(folder_path)
    if os.path.isfile(os.path.join(folder_path, name))
]

with open(output_file, "w", encoding="utf-8") as f:
    json.dump(file_names, f, ensure_ascii=False, indent=2)

print(f"Saved to: {output_file}")
