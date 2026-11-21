"""Personal commitment calibration loop (Planning-Fallacy bias correction, v3).

Tracks the user's historical ratio of `actual_minutes / estimated_minutes` per work kind
(matching frontend `estimate-memory.js` kinds) and globally, then runs a dual-track
capacity check (Nominal vs Calibrated) against live `/api/v3/calendar` free time so
students are warned when optimism bias would turn a seemingly feasible commitment into
an over-commitment.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
import math
import re
from typing import Any

from .layers import calendar_snapshot, validate_snapshot_v3
from .snapshot import _instant
from .time_utils import get_zone

# Kept identical to campus-action-agent/estimate-memory.js KINDS
KINDS: list[tuple[str, re.Pattern[str]]] = [
    ("展示", re.compile(r"PPT|ppt|幻灯|展示|汇报|演讲|答辩|路演")),
    ("填报", re.compile(r"报名|登记|填表|填写|问卷|申请表|提交表|签到|确认")),
    ("写作", re.compile(r"报告|论文|作文|心得|总结|感想|综述|文案|稿")),
    ("复习", re.compile(r"复习|备考|背诵|考试|测验|期中|期末|四六级")),
    ("作品", re.compile(r"作品|素材|剪辑|拍摄|视频|海报|设计|排版|整理")),
    ("阅读", re.compile(r"阅读|读书|读完|看完|文献")),
    ("作业", re.compile(r"作业|习题|练习|题目|证明题|计算题|大题|实验|上机|高数|线代|微积分|编程|代码")),
]
MIN_RATIO = 0.3
MAX_RATIO = 4.0
KEEP_RECENT = 12


def kind_of(title: str | None) -> str | None:
    text = str(title or "")
    for kind, pattern in KINDS:
        if pattern.search(text):
            return kind
    return None


def _median(values: list[float]) -> float:
    ordered = sorted(values)
    mid = len(ordered) // 2
    return ordered[mid] if len(ordered) % 2 else (ordered[mid - 1] + ordered[mid]) / 2.0


def _minutes(start: datetime, end: datetime) -> int:
    return int((end - start).total_seconds() // 60)


def _round_to_5(minutes: float) -> int:
    return max(5, int(math.ceil(minutes / 5.0) * 5))


def compute_calibration_profile(history: list[dict], target_kind: str | None = None) -> dict[str, Any]:
    """Compute robust per-kind and global personal duration inflation factors."""
    by_kind: dict[str, list[float]] = {}
    all_ratios: list[float] = []
    for entry in history:
        if not isinstance(entry, dict):
            continue
        est = entry.get("estimated_minutes")
        act = entry.get("actual_minutes")
        if not isinstance(est, (int, float)) or not isinstance(act, (int, float)) or isinstance(est, bool) or isinstance(act, bool):
            continue
        if est < 5 or act < 5 or est > 24 * 60 or act > 24 * 60:
            continue
        ratio = max(MIN_RATIO, min(MAX_RATIO, float(act) / float(est)))
        k = entry.get("kind") if isinstance(entry.get("kind"), str) and entry["kind"] else kind_of(entry.get("title"))
        if k:
            by_kind.setdefault(k, []).append(ratio)
        all_ratios.append(ratio)

    # Keep only most recent samples per bucket
    by_kind = {k: vals[-KEEP_RECENT:] for k, vals in by_kind.items()}
    all_ratios = all_ratios[-KEEP_RECENT:]

    source = "none"
    used_ratios: list[float] = []
    if target_kind and len(by_kind.get(target_kind, [])) >= 2:
        source = f"kind:{target_kind}"
        used_ratios = by_kind[target_kind]
    elif len(all_ratios) >= 2:
        source = "global"
        used_ratios = all_ratios

    factor = round(_median(used_ratios), 2) if used_ratios else 1.0
    sample_count = len(used_ratios)
    confidence = "none" if sample_count < 2 else "low" if sample_count == 2 else "medium" if sample_count <= 4 else "high"
    return {
        "target_kind": target_kind,
        "source": source,
        "factor": factor,
        "sample_count": sample_count,
        "confidence": confidence,
        "per_kind": {k: {"factor": round(_median(v), 2), "count": len(v)} for k, v in by_kind.items()},
    }


def _evaluate_fit(total_free: int, longest_free: int, needed: int, reserve: int, splittable: bool) -> dict[str, Any]:
    missing = needed + reserve - total_free
    if missing > 0:
        return {
            "fits": False,
            "reason_code": "total_shortage",
            "needed_minutes": needed,
            "reserve_minutes": reserve,
            "shortage_minutes": missing,
            "surplus_minutes": 0,
            "summary": f"按 {needed} 分钟 + 预留休息 {reserve} 分钟算，总量还差 {missing} 分钟。",
        }
    if not splittable and longest_free < needed:
        return {
            "fits": False,
            "reason_code": "contiguous_shortage",
            "needed_minutes": needed,
            "reserve_minutes": reserve,
            "shortage_minutes": needed - longest_free,
            "surplus_minutes": total_free - reserve - needed,
            "summary": f"预留 {reserve} 分钟后总量够，但最长连续空档仅 {longest_free} 分钟，放不下一口气 {needed} 分钟。",
        }
    surplus = total_free - reserve - needed
    return {
        "fits": True,
        "reason_code": "fits",
        "needed_minutes": needed,
        "reserve_minutes": reserve,
        "shortage_minutes": 0,
        "surplus_minutes": surplus,
        "summary": f"按 {needed} 分钟 + 预留休息 {reserve} 分钟算排得下，扣完还剩 {surplus} 分钟。",
    }


def calibrate_capacity(payload: object) -> dict[str, Any]:
    """POST /api/v3/calibrate-capacity: personal bias calibration + dual-track commitment check."""
    if not isinstance(payload, dict):
        raise ValueError("请求JSON必须是对象")
    history = payload.get("history", [])
    if not isinstance(history, list):
        raise ValueError("history 必须是包含 estimated_minutes 和 actual_minutes 的数组")
    title = payload.get("title")
    target_kind = payload.get("kind") if isinstance(payload.get("kind"), str) and payload["kind"] else kind_of(title)
    profile = compute_calibration_profile(history, target_kind)

    nominal = payload.get("nominal_minutes")
    if nominal is None:
        return {"schema_version": 1, "calibration": profile}
    if not isinstance(nominal, int) or isinstance(nominal, bool) or not (1 <= nominal <= 24 * 60):
        raise ValueError("nominal_minutes 必须是 1 到 1440 之间的整数")

    reserve = payload.get("reserve_minutes", 0)
    if not isinstance(reserve, int) or isinstance(reserve, bool) or not (0 <= reserve <= 24 * 60):
        raise ValueError("reserve_minutes 必须是 0 到 1440 之间的整数")
    splittable = bool(payload.get("splittable", False))

    calibrated_minutes = _round_to_5(nominal * profile["factor"]) if profile["sample_count"] >= 2 else nominal
    response: dict[str, Any] = {
        "schema_version": 1,
        "calibration": profile,
        "nominal_minutes": nominal,
        "calibrated_minutes": calibrated_minutes,
        "reserve_minutes": reserve,
        "splittable": splittable,
    }

    snapshot = payload.get("snapshot")
    if snapshot is not None:
        if not isinstance(snapshot, dict):
            raise ValueError("snapshot 必须是 v3 快照对象")
        validate_snapshot_v3(snapshot)
        ref = _instant(snapshot["reference_time"], "reference_time")
        ref_day = ref.astimezone(get_zone()).date()
        date_range = payload.get("range") or {
            "from": ref_day.isoformat(),
            "to": ref_day.isoformat(),
        }
        cal = calendar_snapshot({"snapshot": snapshot, "range": date_range})
        # Compute remaining free intervals after deducting already scheduled work blocks inside the range
        range_start = max(ref, datetime.combine(date.fromisoformat(date_range["from"]), datetime.min.time(), tzinfo=get_zone()))
        range_end = datetime.combine(date.fromisoformat(date_range["to"]) + timedelta(days=1), datetime.min.time(), tzinfo=get_zone())
        free_spans = [
            (max(range_start, _instant(f["start"], "s")), min(range_end, _instant(f["end"], "e")))
            for f in cal["layers"]["free"]
        ]
        free_spans = [(s, e) for s, e in free_spans if e > s]
        work_spans = [
            (_instant(w["start"], "s"), _instant(w["end"], "e"))
            for w in cal["layers"]["work"]
            if w.get("kind") != "appointment"
        ]
        # Cut work from free
        remaining: list[tuple[datetime, datetime]] = []
        for fs, fe in free_spans:
            pieces = [(fs, fe)]
            for ws, we in work_spans:
                nxt = []
                for ps, pe in pieces:
                    if we <= ps or ws >= pe:
                        nxt.append((ps, pe))
                        continue
                    if ps < ws:
                        nxt.append((ps, ws))
                    if we < pe:
                        nxt.append((we, pe))
                pieces = nxt
            remaining.extend((s, e) for s, e in pieces if e > s)
        total_free = sum(_minutes(s, e) for s, e in remaining)
        longest_free = max((_minutes(s, e) for s, e in remaining), default=0)

        nominal_verdict = _evaluate_fit(total_free, longest_free, nominal, reserve, splittable)
        calibrated_verdict = _evaluate_fit(total_free, longest_free, calibrated_minutes, reserve, splittable)
        optimism_risk = nominal_verdict["fits"] and not calibrated_verdict["fits"]
        kind_label = f"「{target_kind}」" if profile["source"].startswith("kind:") and target_kind else "同类任务"
        if optimism_risk:
            warning = (
                f"乐观偏差预警：按名义 {nominal} 分钟看似排得下，但你过去 {profile['sample_count']} 次{kind_label}"
                f"实际用时中位为预估的 {profile['factor']} 倍（约 {calibrated_minutes} 分钟），"
                f"算上 {reserve} 分钟休息后存在违约风险（{calibrated_verdict['summary']}）。"
            )
        elif profile["sample_count"] >= 2 and profile["factor"] != 1.0:
            warning = (
                f"已参考你过去 {profile['sample_count']} 次{kind_label}的用时偏差（{profile['factor']} 倍），"
                f"将 {nominal} 分钟校准为约 {calibrated_minutes} 分钟进行复核。"
            )
        else:
            warning = "历史完成样本不足 2 条，本次按名义时长原样试算；完成后记录实际耗时即可逐步校准。"

        response.update({
            "range": date_range,
            "remaining_free_minutes": total_free,
            "longest_free_minutes": longest_free,
            "nominal_verdict": nominal_verdict,
            "calibrated_verdict": calibrated_verdict,
            "optimism_risk": optimism_risk,
            "explanation": warning,
            "snapshot_mutated": False,
        })
    return response
