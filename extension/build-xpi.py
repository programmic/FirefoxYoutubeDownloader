from pathlib import Path
import json
import re
import zipfile

# Folder containing this script

ROOT = Path(__file__).resolve().parent

# Version pattern:

# extension_v1-1-0.xpi

# extension_v1-1-1.xpi

# extension_v1-1-2.xpi

VERSION_PATTERN = re.compile(
    r"^extension_v1-1-(\d+)\.xpi$",
    re.IGNORECASE
)

def get_next_version():
    highest = -1


    for file in ROOT.iterdir():
        if not file.is_file():
            continue

        match = VERSION_PATTERN.match(file.name)

        if match:
            version = int(match.group(1))
            highest = max(highest, version)

    return highest + 1


def change_manifest_version_number(number):
    manifest_path = ROOT / "manifest.json"


    if not manifest_path.is_file():
        raise FileNotFoundError(
            f"Manifest file not found: {manifest_path}"
        )

    with manifest_path.open("r", encoding="utf-8") as f:
        manifest_data = json.load(f)

    new_version = f"1.1.{number}"
    manifest_data["version"] = new_version

    with manifest_path.open("w", encoding="utf-8") as f:
        json.dump(
            manifest_data,
            f,
            ensure_ascii=False,
            indent=4
        )
        f.write("\n")

    print(f"Updated manifest version to {new_version}")

    return new_version


def create_xpi():
    # Determine the next version first.
    version_number = get_next_version()


    # Update manifest BEFORE creating the XPI.
    version = change_manifest_version_number(version_number)

    output_name = f"extension_v1-1-{version_number}.xpi"
    output_path = ROOT / output_name

    print(f"Creating: {output_name}")

    with zipfile.ZipFile(
        output_path,
        "w",
        zipfile.ZIP_DEFLATED
    ) as xpi:

        for path in ROOT.rglob("*"):

            # Skip directories
            if not path.is_file():
                continue

            # Never include existing XPI files
            if path.suffix.lower() == ".xpi":
                continue

            # Never include this build script
            if path.resolve() == Path(__file__).resolve():
                continue

            archive_path = path.relative_to(ROOT)

            print(f"  + {archive_path}")

            xpi.write(
                path,
                archive_path
            )

    print()
    print(f"Done: {output_path}")
    print(f"Extension version: {version}")


if __name__ == "__main__":
    create_xpi()
