"""Expand the Windows runtime pins to official PyPI hashes for Linux installs."""
import concurrent.futures
import json
from pathlib import Path
import urllib.request

ROOT = Path(__file__).resolve().parents[1]


def resolve(pin):
    name, version = pin.split("==")
    with urllib.request.urlopen(f"https://pypi.org/pypi/{name}/{version}/json", timeout=30) as response:
        metadata = json.load(response)
    hashes = sorted({item["digests"]["sha256"] for item in metadata["urls"] if not item.get("yanked")})
    if not hashes:
        raise RuntimeError(f"No non-yanked distributions: {pin}")
    return pin + " \\\n" + " \\\n".join("    --hash=sha256:" + value for value in hashes)


def main():
    pins = [line.split()[0] for line in (ROOT / "requirements.lock").read_text().splitlines()
            if line.strip() and not line.startswith("#")]
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        entries = list(pool.map(resolve, pins))
    (ROOT / "deploy/requirements-linux.lock").write_text(
        "# Same runtime versions as requirements.lock; official PyPI distribution SHA256 hashes.\n"
        "# Regenerate with python deploy/lock_linux.py; review changes before building.\n"
        + "\n".join(entries) + "\n", encoding="utf-8")
    print(f"Locked {len(pins)} runtime packages with official PyPI hashes")


if __name__ == "__main__":
    main()
