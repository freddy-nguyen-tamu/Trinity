import os
from mutagen.easyid3 import EasyID3
from mutagen.mp3 import MP3

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MP3_FOLDER = os.path.join(BASE_DIR, "downloads")

# Hardcoded metadata for newly downloaded MP3 files
metadata_list = []


# Loop through the metadata and update MP3 tags
for metadata in metadata_list:
    file_path = os.path.join(MP3_FOLDER, metadata["file_name"])
    
    if os.path.exists(file_path):
        try:
            audio = MP3(file_path, ID3=EasyID3)
        except Exception:
            audio = MP3(file_path)
            audio.add_tags()
        
        # Update title and artist
        if "title" in metadata:
            audio["title"] = metadata["title"]
        if "artist" in metadata:
            audio["artist"] = metadata["artist"]
        
        audio.save()
        print(f"Updated: {metadata['file_name']}")
    else:
        print(f"File not found: {metadata['file_name']}")