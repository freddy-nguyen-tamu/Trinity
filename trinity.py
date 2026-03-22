import subprocess
import sys
import os

# Path to Python interpreter
PYTHON_EXE = r"C:\Users\qacer\AppData\Local\Python\pythoncore-3.14-64\python.exe"

# Folder where all scripts live
SCRIPTS_DIR = r"C:\Users\qacer\Downloads\ytb"


def run_script(script_name):
    # Run each python script till finished
    script_path = os.path.join(SCRIPTS_DIR, script_name)

    print(f"\nRUNNING: {script_path}\n{'-' * 50}")

    if not os.path.exists(script_path):
        print(f"Missing script: {script_path}")
        sys.exit(1)

    # Use the real python.exe to run the child script
    result = subprocess.run([PYTHON_EXE, script_path], text=True)

    if result.returncode == 0:
        print(f"FINISHED: {script_name}")
    else:
        print(f"ERROR: {script_name} exited with code {result.returncode}")
        sys.exit(result.returncode)


if __name__ == "__main__":
    # Change this list if need a different order or more/less scripts
    scripts = [
        "thumbnail.py",
        "mediahuman.py",
        "prepforaichat.py",
        "test.py",
    ]

    # Run each script in order
    for script in scripts:
        run_script(script)

    print("\nALL SCRIPTS COMPLETED SUCCESSFULLY")
    input("Press Enter to close...")
