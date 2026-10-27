"""Windows smoke-test helper (CI only).

    python smoke.py make   <root> <manifest.json> <big_mb>
    python smoke.py verify <root> <manifest.json>

`make` fills <root> with random-content files (one big file, nested small ones,
an empty dir) and records every path's SHA-256. `verify` fails unless <root>
holds exactly that tree, byte for byte.
"""

import hashlib
import json
import os
import sys
from pathlib import Path

MB = 1024 * 1024


def _write(path: Path, size: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as f:
        for _ in range(size // MB):
            f.write(os.urandom(MB))
        f.write(os.urandom(size % MB))


def snapshot(root: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for p in sorted(root.rglob("*")):
        rel = p.relative_to(root).as_posix()
        out[rel] = "dir" if p.is_dir() else hashlib.sha256(p.read_bytes()).hexdigest()
    return out


def make(root: Path, manifest: Path, big_mb: int) -> None:
    root.mkdir(parents=True)
    _write(root / "big.bin", big_mb * MB)
    for i in range(6):
        _write(root / "sub" / f"deeper{i % 2}" / f"small{i}.bin", (i + 1) * MB // 2 + i)
    (root / "empty_dir").mkdir()
    (root / "note.txt").write_text(f"tree {root.name}\n")
    manifest.write_text(json.dumps(snapshot(root), indent=1))
    print(f"made {root}: {len(snapshot(root))} entries")


def verify(root: Path, manifest: Path) -> int:
    want = json.loads(manifest.read_text())
    got = snapshot(root)
    if got == want:
        print(f"OK {root}: {len(got)} entries match {manifest.name}")
        return 0
    for rel in sorted(set(want) | set(got)):
        if want.get(rel) != got.get(rel):
            print(f"MISMATCH {rel}: want={want.get(rel)} got={got.get(rel)}")
    return 1


if __name__ == "__main__":
    cmd, root, manifest, *rest = sys.argv[1:]
    if cmd == "make":
        make(Path(root), Path(manifest), int(rest[0]))
    else:
        raise SystemExit(verify(Path(root), Path(manifest)))
