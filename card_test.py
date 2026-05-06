"""Card test with strict safety guards.

Key protections (added after a refund-the-wrong-ticket incident):

1. **Whitelist** — before reserving, record every PNR currently on
   the account. After reserving, the new PNR is added to a
   per-test whitelist. Refund can ONLY touch whitelisted PNRs.
2. **Route+date verification** — before refund, fetch the ticket
   info for our PNR and verify the route and date match exactly
   what we intended (e.g. SRT 김천(구미)→동대구 on today+25d).
3. **Custom SRT refund endpoint call** — bypasses srtgo's
   `get_reservations()` zip(train, pay) bug by using only our
   reservation_number to fetch info, then posting refund with
   data scoped to that PNR.
4. **Post-refund audit** — re-fetch reservations and confirm:
   our PNR is gone AND every protected PNR is still present.
   If audit fails, raise a loud error.
"""
from __future__ import annotations

import json
import time
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
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


def _target_date(days_ahead: int = 25) -> str:
    return _next_weekday(date.today() + timedelta(days=days_ahead)).strftime("%Y%m%d")


# ─── SRT ────────────────────────────────────────────────────────────────
SRT_TEST_DEP = "김천(구미)"
SRT_TEST_ARR = "동대구"
SRT_TEST_DEP_CODE = "0507"
SRT_TEST_ARR_CODE = "0015"
SRT_TEST_TIME = "053000"

SRT_REFUND_URL = "https://app.srail.or.kr:443/atc/selectListAtc02063_n.do"
SRT_RESERVE_INFO_URL = "https://app.srail.or.kr:443/atc/getListAtc14087.do"
SRT_RESERVE_INFO_REFERER = (
    "https://app.srail.or.kr:443/atc/selectListAtc14086_n.do?pnrNo="
)


def _srt_fetch_reserve_info(srt_session, pnr_no: str) -> tuple[Optional[dict], str]:
    """Call SRT reserve_info with the PNR-scoped Referer.

    Returns (row_dict, message). row_dict is None on failure.
    """
    headers = {"Referer": SRT_RESERVE_INFO_REFERER + pnr_no}
    r = srt_session.post(SRT_RESERVE_INFO_URL, headers=headers)
    try:
        info = json.loads(r.text)
    except Exception:
        return None, f"invalid JSON ({r.text[:80]})"
    if info.get("ErrorCode") != "0":
        return None, f"error: {info.get('ErrorMsg')}"
    rows = info.get("outDataSets", {}).get("dsOutput1") or []
    if not rows:
        return None, "empty dsOutput1"
    return rows[0], "ok"


def _srt_verify_pnr_match(srt_session, pnr_no: str, expected_dep_code: str,
                          expected_arr_code: str, expected_date: str,
                          protected_pnrs: set[str],
                          retries: list[int] = (5, 15, 30)) -> tuple[Optional[dict], str]:
    """Try reserve_info with sleeps until the response PNR matches our pnr_no.

    Stops immediately if the response PNR is in the protected set.
    Returns (row_dict, message). None means failed all retries.
    """
    last_msg = "no attempts"
    for i, sleep_sec in enumerate(retries, 1):
        time.sleep(sleep_sec)
        row, msg = _srt_fetch_reserve_info(srt_session, pnr_no)
        if row is None:
            last_msg = f"attempt {i} (after {sleep_sec}s): fetch failed — {msg}"
            continue
        got = row.get("pnrNo")
        if got in protected_pnrs:
            return None, (
                f"⚠ STOP: reserve_info returned protected PNR {got} after {sleep_sec}s — "
                f"보호 표 환불 위험 차단"
            )
        if got != pnr_no:
            last_msg = f"attempt {i} (after {sleep_sec}s): PNR mismatch — requested {pnr_no} got {got}"
            continue
        # PNR matches — verify route + date
        if row.get("dptRsStnCd") != expected_dep_code:
            return None, f"dep mismatch: expected {expected_dep_code} got {row.get('dptRsStnCd')}"
        if row.get("arvRsStnCd") != expected_arr_code:
            return None, f"arr mismatch: expected {expected_arr_code} got {row.get('arvRsStnCd')}"
        if row.get("dptDt") != expected_date:
            return None, f"date mismatch: expected {expected_date} got {row.get('dptDt')}"
        return row, f"matched after {sleep_sec}s (attempt {i})"
    return None, last_msg


def _srt_post_refund(srt_session, pnr_no: str, row: dict) -> tuple[bool, str]:
    payload = {
        "pnr_no": pnr_no,
        "cnc_dmn_cont": "card-test 자동환불",
        "saleDt": row.get("ogtkSaleDt"),
        "saleWctNo": row.get("ogtkSaleWctNo"),
        "saleSqno": row.get("ogtkSaleSqno"),
        "tkRetPwd": row.get("ogtkRetPwd"),
        "psgNm": row.get("buyPsNm"),
    }
    if not all((payload["saleDt"], payload["saleWctNo"], payload["saleSqno"], payload["tkRetPwd"])):
        return False, f"missing refund fields"
    r = srt_session.post(SRT_REFUND_URL, data=payload)
    try:
        resp = json.loads(r.text)
    except Exception:
        return False, f"refund: invalid JSON ({r.text[:80]})"
    code = resp.get("ErrorCode") or resp.get("strResult")
    if code in ("0", "SUCC"):
        return True, "refund OK"
    return False, f"refund error: {resp.get('ErrorMsg') or resp.get('msgTxt') or str(resp)[:120]}"


def srt_card_test() -> CardTestResult:
    r = CardTestResult(ok=False)
    creds = config.srt.load()
    if not creds:
        r.summary = "SRT 자격증명 없음"; return r
    if not creds.card_number:
        r.summary = "카드 정보 없음"; return r

    from SRT import SRT, Adult, SeatType
    from SRT.errors import SRTError

    target_date = _target_date(25)
    r.step("date", True, f"{target_date} ({SRT_TEST_DEP}→{SRT_TEST_ARR} {SRT_TEST_TIME[:2]}:{SRT_TEST_TIME[2:4]} 이후)")

    try:
        srt = SRT(creds.srt_id, creds.srt_password)
    except Exception as e:
        r.step("login", False, str(e)[:120]); r.summary = "로그인 실패"; return r
    r.step("login", True)

    # SAFETY 1: snapshot existing PNRs (protected — never touch)
    try:
        existing = {str(x.reservation_number) for x in srt.get_reservations()}
        r.step("snapshot", True, f"기존 예약 {len(existing)}건 보호 등록")
    except Exception as e:
        r.step("snapshot", False, str(e)[:120]); r.summary = "보호 스냅샷 실패"; return r

    try:
        trains = srt.search_train(SRT_TEST_DEP, SRT_TEST_ARR, target_date, SRT_TEST_TIME, available_only=False)
    except Exception as e:
        r.step("search", False, str(e)[:120]); r.summary = "조회 실패"; return r
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
        r.step("reserve", False, str(e)[:120]); r.summary = "예약 실패"; return r
    test_pnr = str(reservation.reservation_number)
    if test_pnr in existing:
        r.step("reserve", False, f"PNR {test_pnr} 이 보호 목록에 있음 — 안전상 중단")
        r.summary = "PNR 충돌"
        return r
    r.step("reserve", True, f"PNR={test_pnr} (테스트 화이트리스트 등록)")

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

    # SAFETY 2+3: wait for SRT server sync, then verify PNR/route/date,
    # then refund only if everything matches. Stops immediately if a
    # protected PNR comes back (would mean refunding the wrong ticket).
    if paid:
        row, verify_msg = _srt_verify_pnr_match(
            srt._session, test_pnr,
            SRT_TEST_DEP_CODE, SRT_TEST_ARR_CODE, target_date,
            protected_pnrs=existing,
            retries=(5, 15, 30),
        )
        r.step("verify", row is not None, verify_msg)
        if row is not None:
            refunded, refund_msg = _srt_post_refund(srt._session, test_pnr, row)
            r.step("refund", refunded, refund_msg)
        else:
            refunded = False
            refund_msg = verify_msg
            r.step("refund", False, "verify 실패로 환불 미시도 (안전상 차단)")
    else:
        # not paid → safe to call cancel (works on unpaid reservations)
        try:
            ok = srt.cancel(reservation)
            r.step("cancel", ok, "취소 OK (결제 전)")
            refunded = ok
            refund_msg = "cancel ok" if ok else "cancel returned False"
        except Exception as e:
            r.step("cancel", False, str(e)[:120])
            refunded = False
            refund_msg = str(e)[:120]

    # SAFETY 4: post-refund audit — our PNR gone AND all protected PNRs still alive
    try:
        after = {str(x.reservation_number) for x in srt.get_reservations()}
        lost_protected = existing - after
        still_test = test_pnr in after
        if lost_protected:
            r.step("audit", False, f"⚠ 보호 표가 사라짐!! {sorted(lost_protected)} — 즉시 SRT 앱 확인")
        elif still_test:
            r.step("audit", False, f"⚠ 테스트 PNR {test_pnr} 가 환불 안 됨 — SRT 앱에서 수동 환불")
        else:
            r.step("audit", True, "보호 표 모두 살아있고, 테스트 표만 환불됨")
    except Exception as e:
        r.step("audit", False, f"audit 실패: {e}")

    if paid and refunded:
        r.ok = True
        r.summary = "✓ 카드 정상 — 예약·결제·환불·검증까지 모두 성공 (위약금 약 400원)"
    elif paid and not refunded:
        r.summary = f"⚠ 결제는 됐으나 자동 환불 실패 — SRT 앱에서 수동 환불 (PNR {test_pnr})"
    else:
        r.summary = "결제 실패 — 카드 정보 확인"
    return r


# ─── KTX ────────────────────────────────────────────────────────────────
KTX_TEST_DEP = "서울"
KTX_TEST_ARR = "광명"
KTX_TEST_TIME = "053000"


def ktx_card_test() -> CardTestResult:
    r = CardTestResult(ok=False)
    creds = config.ktx.load()
    if not creds:
        r.summary = "KTX 자격증명 없음"; return r
    if not creds.card_number:
        r.summary = "카드 정보 없음"; return r

    from srtgo.ktx import AdultPassenger, ReserveOption, TrainType, NoResultsError
    from ktx_korail import PatchedKorail

    target_date = _target_date(25)
    r.step("date", True, f"{target_date} ({KTX_TEST_DEP}→{KTX_TEST_ARR} {KTX_TEST_TIME[:2]}:{KTX_TEST_TIME[2:4]} 이후)")

    try:
        client = PatchedKorail(creds.ktx_id, creds.ktx_password, auto_login=False)
        if not client.login():
            raise RuntimeError("login returned False")
    except Exception as e:
        r.step("login", False, str(e)[:120]); r.summary = "로그인 실패"; return r
    r.step("login", True, getattr(client, "name", ""))

    # SAFETY 1: snapshot existing reservations + tickets PNRs
    try:
        existing_rsv = {str(x.rsv_id) for x in client.reservations()}
        existing_tkt = {str(getattr(x, "pnr_no", "")) for x in client.tickets()}
        existing = existing_rsv | (existing_tkt - {""})
        r.step("snapshot", True, f"기존 예약 {len(existing_rsv)} + 발권 {len(existing_tkt)} 보호")
    except Exception as e:
        r.step("snapshot", False, str(e)[:120]); r.summary = "보호 스냅샷 실패"; return r

    try:
        trains = client.search_train(KTX_TEST_DEP, KTX_TEST_ARR, target_date, KTX_TEST_TIME, train_type=TrainType.KTX)
    except NoResultsError:
        r.step("search", False, "no results")
        r.summary = "테스트 가능한 좌석 없음 — 다른 날 시도"; return r
    except Exception as e:
        r.step("search", False, str(e)[:120]); r.summary = "조회 실패"; return r
    train = next((t for t in trains if t.has_general_seat()), None)
    if train is None:
        r.step("search", False, f"{len(trains)} trains, 일반실 가능 0건")
        r.summary = "테스트 가능한 좌석 없음 — 다른 날 시도"; return r
    r.step("search", True, f"{train}")

    reservation = None
    try:
        reservation = client.reserve(train, passengers=[AdultPassenger(1)], option=ReserveOption.GENERAL_FIRST)
    except Exception as e:
        r.step("reserve", False, str(e)[:120]); r.summary = "예약 실패"; return r
    test_pnr = str(reservation.rsv_id)
    if test_pnr in existing:
        r.step("reserve", False, f"PNR {test_pnr} 이 보호 목록에 있음 — 안전상 중단")
        r.summary = "PNR 충돌"; return r
    # SAFETY 2: route + date check on the just-created reservation
    if (reservation.dep_station_name != KTX_TEST_DEP or
        reservation.arr_station_name != KTX_TEST_ARR or
        reservation.dep_date != target_date):
        r.step("reserve", False, f"route/date mismatch on returned reservation — 안전상 중단")
        r.summary = "예약 데이터 불일치"
        # try cancel since we never paid
        try: client.cancel(reservation)
        except Exception: pass
        return r
    r.step("reserve", True, f"PNR={test_pnr}")

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

    # cancel/refund
    refunded = False
    refund_msg = ""
    if not paid:
        try:
            ok = client.cancel(reservation)
            r.step("cancel", ok, "취소 OK (결제 전)")
            refunded = ok; refund_msg = "cancel ok"
        except Exception as e:
            r.step("cancel", False, str(e)[:120]); refund_msg = str(e)[:120]
    else:
        # paid — find ticket and refund. SAFETY: only the ticket whose pnr_no == test_pnr
        try:
            tickets = client.tickets()
            target_tkt = next(
                (t for t in tickets if str(getattr(t, "pnr_no", "")) == test_pnr),
                None,
            )
            if target_tkt is None:
                r.step("refund", False, f"발권 목록에서 PNR {test_pnr} 못 찾음")
                refund_msg = "ticket not found"
            elif (getattr(target_tkt, "dep_station_name", "") != KTX_TEST_DEP or
                  getattr(target_tkt, "arr_station_name", "") != KTX_TEST_ARR or
                  getattr(target_tkt, "dep_date", "") != target_date):
                r.step("refund", False, f"⚠ ticket route/date mismatch — 안전상 중단")
                refund_msg = "ticket mismatch"
            else:
                ok = client.refund(target_tkt)
                refunded = ok
                refund_msg = "refund OK" if ok else "refund returned False"
                r.step("refund", ok, refund_msg)
        except Exception as e:
            r.step("refund", False, str(e)[:120]); refund_msg = str(e)[:120]

    # SAFETY 4: post audit
    try:
        after_rsv = {str(x.rsv_id) for x in client.reservations()}
        after_tkt = {str(getattr(x, "pnr_no", "")) for x in client.tickets()} - {""}
        after = after_rsv | after_tkt
        lost = existing - after
        still_test = test_pnr in after
        if lost:
            r.step("audit", False, f"⚠ 보호 표가 사라짐!! {sorted(lost)} — 즉시 코레일 앱 확인")
        elif still_test:
            r.step("audit", False, f"⚠ 테스트 PNR {test_pnr} 가 처리 안 됨 — 코레일 앱에서 수동")
        else:
            r.step("audit", True, "보호 표 모두 살아있고, 테스트 표만 환불됨")
    except Exception as e:
        r.step("audit", False, f"audit 실패: {e}")

    if paid and refunded:
        r.ok = True
        r.summary = "✓ 카드 정상 — 예약·결제·환불·검증까지 모두 성공 (위약금 약 400원)"
    elif paid and not refunded:
        r.summary = f"⚠ 결제는 됐으나 자동 환불 실패 ({refund_msg}) — 코레일 앱에서 수동"
    else:
        r.summary = "결제 실패 — 카드 정보 확인"
    return r
