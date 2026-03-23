import os
import json

folder_path = r"C:\Users\qacer\Downloads\ytb\downloads"
output_file = r"C:\Users\qacer\Downloads\ytb\filenames.json"

file_names = [
    name for name in os.listdir(folder_path)
    if os.path.isfile(os.path.join(folder_path, name))
]

with open(output_file, "w", encoding="utf-8") as f:
    json.dump(file_names, f, ensure_ascii=False, indent=2)

print(f"Saved to: {output_file}")