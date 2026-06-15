#!/usr/bin/env python3
"""Generate gcode and copy it plus the firmware onto 20 microSD cards."""

import os
import shutil
import subprocess
import sys
import time

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MEDIA_ROOT = "/media/cfbolz"
FIRMWARE = os.path.join(REPO_ROOT, ".pio", "build", "STM32H723ZE_btt", "firmware.bin")
AUTO0 = os.path.join(REPO_ROOT, "auto0.g")


def find_card_dir():
    entries = [
        os.path.join(MEDIA_ROOT, name)
        for name in os.listdir(MEDIA_ROOT)
        if os.path.isdir(os.path.join(MEDIA_ROOT, name))
    ]
    if len(entries) == 0:
        sys.exit(f"Error: no folder found in {MEDIA_ROOT}. Is the SD card mounted?")
    if len(entries) > 1:
        sys.exit(
            f"Error: multiple folders found in {MEDIA_ROOT}:\n"
            + "\n".join(f"  {e}" for e in entries)
        )
    return entries[0]


def find_device(card_dir):
    result = subprocess.run(
        ["findmnt", "-n", "-o", "SOURCE", "--target", card_dir],
        capture_output=True, text=True, check=True,
    )
    return result.stdout.strip()


def main():
    for i in range(1, 21):
        print(f"=== Card {i}/20 ===")
        input(f"Insert SD card {i} and press Enter when ready...")

        print(f"Generating G-code for permutation-seed {i}...")
        subprocess.run(
            [
                "python3",
                "_insects/generate_gcode.py",
                "--total-length=10h",
                "--action-length=25m",
                "--break-length=5m",
                "--seed=1",
                "--num-blocks=5",
                "--extra-sleep=20s",
                "--permutation-seed", str(i),
            ],
            cwd=REPO_ROOT,
            check=True,
        )

        card_dir = find_card_dir()
        device = find_device(card_dir)
        print(f"Found SD card at {card_dir} ({device})")

        print("Copying files...")
        shutil.copy(AUTO0, card_dir)
        shutil.copy(FIRMWARE, card_dir)

        time.sleep(5)

        #print(f"Unmounting {card_dir}...")
        #subprocess.run(["udisksctl", "unmount", "-b", device], check=True)

        #print(f"Ejecting {card_dir}...")
        #subprocess.run(["udisksctl", "power-off", "-b", device], check=True)

        print(f"Card {i} done.\n")

    print("All 20 cards processed.")


if __name__ == "__main__":
    main()
