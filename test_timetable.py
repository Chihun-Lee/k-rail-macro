"""timetable.py 환승 조합 순수 로직 검증 (네트워크 불필요).

실행:  venv/bin/python test_timetable.py
"""
from __future__ import annotations

import timetable as tt


def row(no: str, dep_time: str, arr_time: str, gen: bool = True, spc: bool = False) -> dict:
    return {
        "train_number": no, "train_name": "SRT", "dep": "수서", "arr": "대전",
        "dep_time": dep_time, "arr_time": arr_time, "general": gen, "special": spc,
    }


def test_min_gap_rule():
    """환승 대기 6분 미만 연결은 제외, 6분 이상만 채택."""
    leg1 = [row("101", "080000", "090000")]
    leg2 = [
        row("201", "090500", "110000"),  # 5분 간격 → 제외
        row("202", "090600", "110500"),  # 6분 간격 → 채택 (가장 빠른 유효 연결)
        row("203", "100000", "120000"),  # 더 늦음 → 무시
    ]
    out = tt.combine(leg1, leg2, "대전", 6)
    assert len(out) == 1, out
    assert out[0]["legs"][1]["train_number"] == "202", out[0]
    assert out[0]["gap_min"] == 6
    print("  [ok] 6분 규칙: 5분 간격 제외, 6분 간격 채택(최속 연결)")


def test_earliest_connection_per_leg1():
    """1구간 열차마다 '가장 빨리 탈 수 있는' 2구간 하나만 붙인다."""
    leg1 = [row("101", "080000", "090000"), row("102", "083000", "093000")]
    leg2 = [row("201", "091000", "110000"), row("202", "094500", "113000")]
    out = tt.combine(leg1, leg2, "대전", 6)
    assert len(out) == 2
    assert out[0]["legs"][1]["train_number"] == "201"   # 101 → 201 (10분 대기)
    assert out[1]["legs"][1]["train_number"] == "202"   # 102 → 202 (201은 6분 미만)
    assert out[1]["gap_min"] == 15
    print("  [ok] 1구간별 최속 유효 연결 1개씩 조합")


def test_no_connection():
    """유효 연결이 없으면 빈 결과."""
    leg1 = [row("101", "220000", "230000")]
    leg2 = [row("201", "080000", "100000")]  # 이미 떠남
    assert tt.combine(leg1, leg2, "대전", 6) == []
    print("  [ok] 유효 연결 없음 → 빈 결과 (자정 넘김 조합 방지)")


def test_bookable_and_totals():
    leg1 = [row("101", "080000", "090000", gen=False, spc=False)]  # 매진
    leg2 = [row("201", "091000", "110000", gen=True)]
    out = tt.combine(leg1, leg2, "대전", 6)
    assert out[0]["bookable_now"] is False   # 한 구간 매진이면 지금은 못 삼(폴링 대상)
    assert out[0]["total_min"] == 180
    assert out[0]["dep_time"] == "080000" and out[0]["arr_time"] == "110000"
    print("  [ok] bookable_now(구간 매진 반영)·총소요 계산")


def test_sort_and_limit():
    a = {"arr_time": "120000", "total_min": 200}
    b = {"arr_time": "110000", "total_min": 250}
    c = {"arr_time": "120000", "total_min": 150}
    out = tt.sort_and_limit([a, b, c], 2)
    assert out == [b, c], out   # 도착 빠른 순, 같으면 총소요 짧은 순
    print("  [ok] 정렬(도착순→소요순) + 상위 N 제한")


if __name__ == "__main__":
    print("환승 조합 로직 (구간별 예약 방식):")
    test_min_gap_rule()
    test_earliest_connection_per_leg1()
    test_no_connection()
    test_bookable_and_totals()
    test_sort_and_limit()
    print("\nALL PASS ✅")
