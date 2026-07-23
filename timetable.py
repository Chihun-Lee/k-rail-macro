"""시간표/환승 조합 순수 로직 (네트워크 없음 — SRT/KTX 워커 공용).

공식 '환승 조회'를 쓰지 않고 구간별 검색 결과를 직접 조합한다(구간별 예약
방식 전제). 규칙: 환승 대기시간이 min_gap_min(기본 6분) 이상인 연결만 유효.
각 1구간 열차마다 '가장 빨리 탈 수 있는 2구간 열차' 하나를 붙여 조합을
만든다 — 같은 1구간에 더 늦은 2구간을 붙인 조합은 항상 열등하므로 제외.
"""
from __future__ import annotations


def hhmmss_to_min(s: str) -> int:
    return int(s[:2]) * 60 + int(s[2:4])


def fmt_min(m: int) -> str:
    return f"{m // 60}시간 {m % 60:02d}분" if m >= 60 else f"{m}분"


def combine(leg1_rows: list[dict], leg2_rows: list[dict], via: str, min_gap_min: int) -> list[dict]:
    """1구간(출발→환승역)·2구간(환승역→도착) 시간표 행을 환승 조합으로 묶는다.

    행 형식(두 워커 공통): {train_number, train_name, dep, arr, dep_time,
    arr_time(HHMMSS), general, special}. 자정을 넘기는 비정상 조합은 제외한다.
    """
    leg2_sorted = sorted(leg2_rows, key=lambda r: r["dep_time"])
    out = []
    for l1 in sorted(leg1_rows, key=lambda r: r["dep_time"]):
        arr_m = hhmmss_to_min(l1["arr_time"])
        best = next(
            (l2 for l2 in leg2_sorted if hhmmss_to_min(l2["dep_time"]) >= arr_m + min_gap_min),
            None,
        )
        if best is None:
            continue
        dep_m = hhmmss_to_min(l1["dep_time"])
        arr2_m = hhmmss_to_min(best["arr_time"])
        if arr2_m <= dep_m:
            continue
        out.append({
            "via": via,
            "legs": [l1, best],
            "dep_time": l1["dep_time"],
            "arr_time": best["arr_time"],
            "gap_min": hhmmss_to_min(best["dep_time"]) - arr_m,
            "total_min": arr2_m - dep_m,
            # 지금 당장 두 구간 다 좌석이 있는 조합인지 (없어도 매크로 폴링 대상은 됨)
            "bookable_now": (l1["general"] or l1["special"]) and (best["general"] or best["special"]),
        })
    return out


def sort_and_limit(transfers: list[dict], limit: int) -> list[dict]:
    """도착 빠른 순 → 총소요 짧은 순으로 정렬해 상위 limit개만 남긴다."""
    return sorted(transfers, key=lambda x: (x["arr_time"], x["total_min"]))[:limit]
