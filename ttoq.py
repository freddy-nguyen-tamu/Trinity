import shutil
from pathlib import Path

# Source and destination paths
src = Path(__file__).resolve().parent / "finished"
dst = Path(r"\\wsl.localhost\Ubuntu\home\qacer6973\projects\qshare_server\shared")

# Ensure destination exists
dst.mkdir(parents=True, exist_ok=True)

# Copy files and folders
for item in src.iterdir():
    target = dst / item.name
    if item.is_dir():
        shutil.copytree(item, target, dirs_exist_ok=True)
    else:
        shutil.copy2(item, target)

print("Copy completed successfully.")
