import os
import json
import requests
from mutagen.easyid3 import EasyID3
from mutagen.mp3 import MP3

# Config
import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MP3_FOLDER = os.path.join(BASE_DIR, "downloads")

# GROQ API config
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL = "llama-3.1-8b-instant"

if not GROQ_API_KEY:
    raise SystemExit("GROQ_API_KEY is not set in the environment.")

# Collect MP3 file names
file_names = [
    f for f in os.listdir(MP3_FOLDER)
    if os.path.isfile(os.path.join(MP3_FOLDER, f)) and f.lower().endswith(".mp3")
]

if not file_names:
    print("No MP3 files found in folder.")
    raise SystemExit

print("Found MP3 files:")
for f in file_names:
    print(" -", f)


# -----------------------------
# 2) Ask Groq to generate metadata_list JSON
# -----------------------------
system_message = (
    "You are helping tag MP3 files. "
    "You must output strictly valid JSON and nothing else."
)

user_prompt = f"""
Given this list of MP3 file names (as a JSON array):

{json.dumps(file_names, ensure_ascii=False, indent=2)}

Return a JSON object with a single key "metadata_list", like this:

{{
  "metadata_list": [
    {{
      "file_name": "<exact file name from input>",
      "title": "<best-guess song title>",
      "artist": "<best-guess artist or 'Unknown'>"
    }}
  ]
}}

Rules:
- Keep file_name EXACT
- Clean title (remove words like 'Official Music Video', 'Lyric Video', etc.)
- If unsure, use artist='Unknown'
- Output ONLY valid JSON. No markdown, no ``` fences, no notes.
"""

payload = {
    "model": GROQ_MODEL,
    "messages": [
        {"role": "system", "content": system_message},
        {"role": "user", "content": user_prompt}
    ],
    "temperature": 0
}

headers = {
    "Authorization": f"Bearer {GROQ_API_KEY}",
    "Content-Type": "application/json"
}

print("\nCalling Groq API...")
response = requests.post(GROQ_API_URL, headers=headers, json=payload)

if response.status_code != 200:
    print("Groq API error:", response.status_code, response.text)
    raise SystemExit

resp_json = response.json()
json_text = resp_json["choices"][0]["message"]["content"].strip()

print("\nRaw JSON from model:\n", json_text)

# -----------------------------
# Clean up markdown fences / notes and extract pure JSON
# -----------------------------
text = json_text.strip()

if "```" in text:
    # Take only the first fenced code block
    first_fence = text.find("```")
    second_fence = text.find("```", first_fence + 3)
    if second_fence != -1:
        inner = text[first_fence + 3:second_fence].strip()
        # Strip "json" language tag if present
        if inner.lower().startswith("json"):
            inner = inner[4:].lstrip("\n\r ")
        text = inner

# Now text should be plain JSON
try:
    data = json.loads(text)
except json.JSONDecodeError as e:
    print("Failed to parse JSON from model:", e)
    print("Text that failed to parse:\n", text)
    raise SystemExit

metadata_list = data.get("metadata_list", [])

if not metadata_list:
    print("Model did not return any metadata_list entries.")
    raise SystemExit

# Apply tags to MP3 files
for metadata in metadata_list:
    file_name = metadata.get("file_name")
    title = metadata.get("title")
    artist = metadata.get("artist")

    file_path = os.path.join(MP3_FOLDER, file_name)

    if not file_name:
        print("Missing file_name in metadata entry, skipping.")
        continue

    if not os.path.exists(file_path):
        print(f"File not found (skipping): {file_name}")
        continue

    try:
        try:
            audio = MP3(file_path, ID3=EasyID3)
        except Exception:
            audio = MP3(file_path)
            audio.add_tags()

        if title:
            audio["title"] = title
        if artist:
            audio["artist"] = artist

        audio.save()
        print(f"Updated: {file_name} | title='{title}' | artist='{artist}'")

    except Exception as e:
        print(f"Error updating {file_name}: {e}")
