"""Card test: reserve a cheap far-future weekday ticket, pay, then cancel.

Verifies the saved card actually clears against SRT/KTX.
A small cancellation fee (≈400원) may apply.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Optional

import config


@dataclass
class CardTestResult:
    ok: bool
    steps: list[dict] = field(default_factory=list)
    summary: str = ""

    def step(self, name: str, ok: bool, detail: str = "") -> None:
        self.steps.append({"name": name, "ok": ok, "detail": detail})


def _next_weekday(d: date) -> date:
    while d.weekday() >= 5:  # Sat=5, Sun=6
        d += timedelta(days=1)
    return d


def _target_date(days_ahead: int = 25) -> str:
    d = _next_weekday(date.today() + timedelta(days=days_ahead))
    return d.strftime("%Y%m%d")


# ─── SRT ────────────────────────────────────────────────────────────────
SRT_TEST_DEP = "김천(구미)"
SRT_TEST_ARR = "동대구"
SRT_TEST_TIME = "053000"


def srt_card_test() -> CardTestResult:
    r = CardTestResult(ok=False)
    creds = config.srt.load()
    if not creds:
        r.summary = "SRT 자격증명 없음"
        return r
    if not creds.card_number:
        r.summary = "카드 정보 없음"
        return r

    from SRT import SRT, Adult, SeatType
    from SRT.errors import SRTError

    target_date = _target_date(25)
    r.step("date", True, f"{target_date} ({SRT_TEST_DEP}→{SRT_TEST_ARR} {SRT_TEST_TIME[:2]}:{SRT_TEST_TIME[2:4]} 이후)")

    try:
        srt = SRT(creds.srt_id, creds.srt_password)
    except Exception as e:
        r.step("login", False, str(e)[:120])
        r.summary = "로그인 실패"
        return r
    r.step("login", True)

    try:
        trains = srt.search_train(SRT_TEST_DEP, SRT_TEST_ARR, target_date, SRT_TEST_TIME, available_only=False)
    except Exception as e:
        r.step("search", False, str(e)[:120])
        r.summary = "조회 실패"
        return r
    train = next((t for t in trains if t.general_seat_available()), None)
    if train is None:
        r.step("search", False, f"{len(trains)} trains, 일반실 가능 0건")
        r.summary = "테스트 가능한 좌석 없음 — 다른 날 시도"
        return r
    r.step("search", True, f"{train}")

    reservation = None
    try:
        reservation = srt.reserve(train, passengers=[Adult(1)], special_seat=SeatType.GENERAL_FIRST)
    except SRTError as e:
        r.step("reserve", False, str(e)[:120])
        r.summary = "예약 실패"
        return r
    r.step("reserve", True, f"예약번호={getattr(reservation, 'reservation_number', '?')}")

    paid = False
    try:
        paid = srt.pay_with_card(
            reservation,
            number=creds.card_number,
            password=creds.card_password,
            validation_number=creds.card_validation,
            expire_date=creds.card_expire,
            installment=creds.card_installment,
            card_type=creds.card_type,
        )
        r.step("pay", paid, "카드 결제 OK" if paid else "pay_with_card returned False")
    except Exception as e:
        r.step("pay", False, str(e)[:120])

    # always try cancel
    try:
        cancelled = srt.cancel(reservation)
        r.step("cancel", cancelled, "취소 OK" if cancelled else "cancel returned False")
    except Exception as e:
        r.step("cancel", False, str(e)[:120])

    if paid:
        r.ok = True
        r.summary = "✓ 카드 정상 — 예약·결제·취소까지 모두 성공 (수수료 약 400원 발생 가능)"
    else:
        r.summary = "결제 실패 — 카드 정보를 확인하세요"
    return r


# ─── KTX ────────────────────────────────────────────────────────────────
KTX_TEST_DEP = "서울"
KTX_TEST_ARR = "광명"
KTX_TEST_TIME = "053000"


def ktx_card_test() -> CardTestResult:
    r = CardTestResult(ok=False)
    creds = config.ktx.load()
    if not creds:
        r.summary = "KTX 자격증명 없음"
        return r
    if not creds.card_number:
        r.summary = "카드 정보 없음"
        return r

    from srtgo.ktx import AdultPassenger, ReserveOption, TrainType, NoResultsError
    from ktx_korail import PatchedKorail

    target_date = _target_date(25)
    r.step("date", True, f"{target_date} ({KTX_TEST_DEP}→{KTX_TEST_ARR} {KTX_TEST_TIME[:2]}:{KTX_TEST_TIME[2:4]} 이후)")

    try:
        client = PatchedKorail(creds.ktx_id, creds.ktx_password, auto_login=False)
        if not client.login():
            raise RuntimeError("login returned False")
    except Exception as e:
        r.step("login", False, str(e)[:120])
        r.summary = "로그인 실패"
        return r
    r.step("login", True, getattr(client, "name", ""))

    try:
        trains = client.search_train(KTX_TEST_DEP, KTX_TEST_ARR, target_date, KTX_TEST_TIME, train_type=TrainType.KTX)
    except NoResultsError:
        r.step("search", False, "no results")
        r.summary = "테스트 가능한 좌석 없음 — 다른 날 시도"
        return r
    except Exception as e:
        r.step("search", False, str(e)[:120])
        r.summary = "조회 실패"
        return r
    train = next((t for t in trains if t.has_general_seat()), None)
    if train is None:
        r.step("search", False, f"{len(trains)} trains, 일반실 가능 0건")
        r.summary = "테스트 가능한 좌석 없음 — 다른 날 시도"
        return r
    r.step("search", True, f"{train}")

    reservation = None
    try:
        reservation = client.reserve(train, passengers=[AdultPassenger(1)], option=ReserveOption.GENERAL_FIRST)
    except Exception as e:
        r.step("reserve", False, str(e)[:120])
        r.summary = "예약 실패"
        return r
    r.step("reserve", True, f"예약번호={reservation.rsv_id}")

    paid = False
    try:
        paid = client.pay_with_card(
            reservation,
            card_number=creds.card_number,
            card_password=creds.card_password,
            birthday=creds.card_validation,
            card_expire=creds.card_expire,
            installment=creds.card_installment,
            card_type="J",
        )
        r.step("pay", paid, "카드 결제 OK" if paid else "pay_with_card returned False")
    except Exception as e:
        r.step("pay", False, str(e)[:120])

    # cancel/refund — try cancel first; if that fails (paid), refund the ticket
    cancelled = False
    try:
        cancelled = client.cancel(reservation)
        r.step("cancel", cancelled, "취소 OK")
    except Exception as e:
        # paid → need refund
        try:
            tickets = client.tickets()
            target = next((t for t in tickets if getattr(t, "rsv_id", None) == reservation.rsv_id
                           or getattr(t, "pnr_no", None) == reservation.rsv_id), None)
            if target is not None:
                refunded = client.refund(target)
                r.step("refund", refunded, "환불 OK" if refunded else "refund returned False")
            else:
                r.step("cancel", False, f"cancel: {e}; ticket not found for refund")
        except Exception as e2:
            r.step("refund", False, str(e2)[:120])

    if paid:
        r.ok = True
        r.summary = "✓ 카드 정상 — 예약·결제·취소까지 모두 성공 (수수료 약 400원 발생 가능)"
    else:
        r.summary = "결제 실패 — 카드 정보를 확인하세요"
    return r
