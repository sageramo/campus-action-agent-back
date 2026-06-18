from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .extractor import parse_notice_file
from .text_io import collect_documents


ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"


def load_multitime_samples(data_dir: Path = DATA_DIR) -> list[dict[str, Any]]:
    path = data_dir / "多时间点通知" / "多时间点通知_标注数据.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def load_conflict_cases(data_dir: Path = DATA_DIR) -> dict[str, Any]:
    path = data_dir / "冲突案例" / "冲突案例_演示数据.json"
    if not path.exists():
        return {"cases": []}
    return json.loads(path.read_text(encoding="utf-8"))


def dataset_summary(data_dir: Path = DATA_DIR) -> dict[str, Any]:
    docs = collect_documents(data_dir)
    folders: dict[str, int] = {}
    suffixes: dict[str, int] = {}
    for doc in docs:
        folders[doc["category"]] = folders.get(doc["category"], 0) + 1
        suffixes[doc["suffix"]] = suffixes.get(doc["suffix"], 0) + 1
    multitime = load_multitime_samples(data_dir)
    conflicts = load_conflict_cases(data_dir).get("cases", [])
    return {
        "total_files": len(docs),
        "folders": folders,
        "suffixes": suffixes,
        "multi_time_samples": len(multitime),
        "multi_time_points": sum(len(item.get("time_points", [])) for item in multitime),
        "conflict_cases": len(conflicts),
        "conflict_tasks": sum(len(case.get("tasks", [])) for case in conflicts),
    }


def parse_sample_documents(data_dir: Path = DATA_DIR, limit: int = 12) -> list[dict[str, Any]]:
    targets: list[Path] = []
    for folder in ["办事通知", "活动策划", "学校规章"]:
        base = data_dir / folder
        if not base.exists():
            continue
        targets.extend(path for path in base.rglob("*") if path.is_file() and path.suffix.lower() in {".txt", ".pdf", ".docx", ".xlsx"})
    parsed = []
    for path in targets[:limit]:
        try:
            parsed.append(parse_notice_file(path).to_dict())
        except Exception as exc:
            parsed.append({"source": str(path), "error": str(exc)})
    return parsed
