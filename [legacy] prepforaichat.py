import os
import io
import subprocess

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
folder_path = os.path.join(BASE_DIR, "downloads")

# StringIO buffer to capture everything we print
buffer = io.StringIO()

def tee_print(*args, **kwargs):
    """Print to console AND store the same text in a buffer."""
    print(*args, **kwargs)                # normal print to terminal
    print(*args, **kwargs, file=buffer)   # also write to buffer


# List all files in the folder
file_names = [
    f for f in os.listdir(folder_path)
    if os.path.isfile(os.path.join(folder_path, f))
]

# Print all file names (and capture them)
for file in file_names:
    tee_print(file)

tee_print("these are the actual mp3 files names in the folders. Give me their json format similar to the one below")
tee_print("""metadata_list = []""")

# Copy everything we printed to the Windows clipboard (Unicode-safe)
text_to_copy = buffer.getvalue()

# Encode as UTF-16LE so Windows clip can handle all Unicode chars
data = text_to_copy.encode("utf-16le")

# when passing bytes, do not use text=True
subprocess.run(["clip"], input=data, check=True)

print("All printed text has been copied to the clipboard. Press Ctrl+V to paste it.")
