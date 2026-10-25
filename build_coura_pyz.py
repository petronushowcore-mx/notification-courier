"""Build a reproducible coura.pyz with the standard library.

The archive is stored, not deflated, and every entry uses the same timestamp.
Running this twice on the same sources produces the same bytes.
"""
import argparse
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parent
MODULES = (
    "coura_mcp.py",
    "coura_pair.py",
    "coura_remote.py",
    "coura_ux.py",
    "courier.py",
    "meta_coura.py",
    "observer.py",
)
MAIN = b"import coura_ux\nraise SystemExit(coura_ux.main())\n"
STAMP = (1980, 1, 1, 0, 0, 0)
SHEBANG = b"#!/usr/bin/env python3\n"


def build(target):
    target = Path(target)
    entries = [("__main__.py", MAIN)]
    for name in MODULES:
        entries.append((name, (ROOT / name).read_bytes()))
    entries.sort()
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("wb") as stream:
        stream.write(SHEBANG)
        with zipfile.ZipFile(stream, "w") as archive:
            for name, data in entries:
                info = zipfile.ZipInfo(name, STAMP)
                info.compress_type = zipfile.ZIP_STORED
                info.external_attr = (0o644 & 0xFFFF) << 16
                info.create_system = 0
                info.flag_bits = 0
                info.extra = b""
                archive.writestr(info, data)
    return target


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("target", nargs="?", default="coura.pyz")
    args = parser.parse_args(argv)
    path = build(args.target)
    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
