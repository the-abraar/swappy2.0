"""Turn the Windows smoke artifact into demo/data/swap_run.json.

    gh run download 40988697805 -R the-abraar/swappy2.0 -D <dir>
    python demo/export_run.py <dir>/windows-smoke/work1/swap.db > demo/data/swap_run.json

Only work1 (the CLI swap X:\\A <-> Y:\\B) is exported; work2 is the same swap
back through the TUI. Free-space numbers are copied from the run log
(the two Get-Volume tables: after format, and after the test folders were made).
"""

import json
import sqlite3
import sys

MB = 1_000_000  # decimal MB, as the speeds below are quoted

db = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
db.row_factory = sqlite3.Row
session = dict(db.execute("select * from session").fetchone())
summary = dict(db.execute("select * from progress_summary").fetchone())
t0 = summary["started_at"]
files = [
    {
        "side": e["side"], "path": h["rel_path"], "size": h["size"], "mode": h["mode"],
        "worker": h["worker_id"], "start": round(h["started_at"] - t0, 4),
        "end": round(h["finished_at"] - t0, 4), "mbps": round(h["throughput_bps"] / MB, 1),
    }
    for h in db.execute("select * from progress_file_history order by started_at, id")
    for e in db.execute("select side from entries where id = ?", (h["entry_id"],))
]


def mean(xs: list[float]) -> float:
    return round(sum(xs) / len(xs), 1)


whole = [f for f in files if f["mode"] == "whole-file"]
wall = max(f["end"] for f in whole) - min(f["start"] for f in whole)

out = {
    "source": {
        "run": "https://github.com/the-abraar/swappy2.0/actions/runs/40988697805",
        "commit": "9297fd7", "runner": "windows-latest, Python 3.12.10",
        "volumes": "two 128 MB VHDX NTFS volumes, X: and Y:",
        "artifact_expires": "2027-01-28",
    },
    "volume_size": 133099520,  # Get-Volume Size, both volumes
    "free_empty": 118333440,  # Get-Volume SizeRemaining right after format
    "free_after_setup": {"X": 54874112, "Y": 60116992},
    "session": {k: session[k] for k in
                ("folder_a", "folder_b", "chunk_size", "safety_margin", "worker_count", "backend")},
    "summary": {k: summary[k] for k in
                ("total_files", "total_bytes", "elapsed_seconds", "chunk_rounds")},
    "speeds_mbps": {  # per-file means are plain averages of each file's own rate
        "sparse_mean_per_file": mean([f["mbps"] for f in files if f["mode"] == "sparse"]),
        "whole_file_mean_per_file": mean([f["mbps"] for f in whole]),
        "whole_file_wall_clock": round(sum(f["size"] for f in whole) / wall / MB, 1),
        "overall_wall_clock": round(summary["total_bytes"] / summary["elapsed_seconds"] / MB, 1),
    },
    "files": files,
}
assert len(files) == summary["total_files"] and sum(f["size"] for f in files) == summary["total_bytes"]
print(json.dumps(out, indent=1))
