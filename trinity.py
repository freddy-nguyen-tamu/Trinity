import subprocess
import sys
import os
import shutil

# Path to Python interpreter
PYTHON_EXE = r"C:\Users\qacer\AppData\Local\Python\pythoncore-3.14-64\python.exe"

# Folder where all scripts live
SCRIPTS_DIR = r"C:\Users\qacer\Downloads\ytb"
OUTPUT_BASE_DIR = r"C:\ytb"
WORKING_DOWNLOADS_DIR = os.path.join(OUTPUT_BASE_DIR, "_working_downloads")
FINISHED_DIR = os.path.join(OUTPUT_BASE_DIR, "finished")


def prepare_output_dirs():
    os.makedirs(WORKING_DOWNLOADS_DIR, exist_ok=True)
    os.makedirs(FINISHED_DIR, exist_ok=True)

    if os.name == "nt":
        subprocess.run(
            ["attrib", "+h", WORKING_DOWNLOADS_DIR],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )


def unique_destination(path):
    if not os.path.exists(path):
        return path

    root, ext = os.path.splitext(path)
    counter = 1
    while True:
        candidate = f"{root} ({counter}){ext}"
        if not os.path.exists(candidate):
            return candidate
        counter += 1


def move_working_items_to_finished():
    moved_count = 0

    if not os.path.isdir(WORKING_DOWNLOADS_DIR):
        return moved_count

    for item_name in sorted(os.listdir(WORKING_DOWNLOADS_DIR)):
        source_path = os.path.join(WORKING_DOWNLOADS_DIR, item_name)

        destination_path = unique_destination(os.path.join(FINISHED_DIR, item_name))
        shutil.move(source_path, destination_path)
        moved_count += 1

    return moved_count


def run_script(script_name):
    # Run each python script till finished
    script_path = os.path.join(SCRIPTS_DIR, script_name)

    print(f"\nRUNNING: {script_path}\n{'-' * 50}")

    if not os.path.exists(script_path):
        print(f"Missing script: {script_path}")
        sys.exit(1)

    # Use the real python.exe to run the child script
    result = subprocess.run(
        [PYTHON_EXE, script_path],
        cwd=SCRIPTS_DIR,
        text=True,
    )

    if result.returncode == 0:
        print(f"FINISHED: {script_name}")
    else:
        print(f"ERROR: {script_name} exited with code {result.returncode}")
        sys.exit(result.returncode)


if __name__ == "__main__":
    prepare_output_dirs()
    exit_code = 0
    moved_count = 0

    # Change this list if need a different order or more/less scripts
    scripts = [
        "thumbnail.py",
        "mediahuman.py",
        "prepforaichat.py",
        "test.py",
    ]

    try:
        # Run each script in order
        for script in scripts:
            run_script(script)
    except KeyboardInterrupt:
        print("\nInterrupted. Moving staged files before closing...")
        exit_code = 130
    except SystemExit as e:
        exit_code = e.code if isinstance(e.code, int) else 1
    finally:
        try:
            moved_count = move_working_items_to_finished()
        except Exception as e:
            print(f"\nERROR: Could not move working files to {FINISHED_DIR}: {e}")
            exit_code = 1

    print(f"\nMOVED {moved_count} WORKING ITEM(S) TO: {FINISHED_DIR}")

    if exit_code == 0:
        print("\nALL SCRIPTS COMPLETED SUCCESSFULLY")
    else:
        print(f"\nSTOPPED WITH EXIT CODE {exit_code}")

    input("Press Enter to close...")

    if exit_code:
        sys.exit(exit_code)
