"""Offline setup checks. Does not scan Bluetooth or open/create a session."""
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    missing = []
    for line in (ROOT / "requirements-lock.txt").read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name, expected = line.split("==", 1)
        try:
            if version(name) != expected:
                missing.append(line)
        except PackageNotFoundError:
            missing.append(line)
    if missing:
        print("Dependencies to install: " + ", ".join(missing))
        return 1
    if "--dependencies-only" in sys.argv:
        return 0
    import app.main  # noqa: F401
    from bleak.backends.winrt.client import BleakClientWinRT  # noqa: F401
    from app.configuration import load_settings, validate_manifest, media_catalog
    load_settings(ROOT)
    validate_manifest(ROOT)
    catalog = media_catalog(ROOT)
    if len(catalog) != 16:
        raise ValueError(f"Expected 16 videos, got {len(catalog)}")
    print(f"Application imports, configuration and videos: OK ({len(catalog)}/16)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
