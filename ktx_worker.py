"""KTX/Korail polling/booking/payment worker.

Mirrors srt-macro/srt_worker.py but uses the patched srtgo Korail.
"""
from __future__ import annotations

import random
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Deque, Optional

from srtgo.ktx import (
    AdultPassenger,
    KorailError,
    NeedToLoginError,
    NoResultsError,
    ReserveOption,
    SoldOutError,
    TrainType,
)

import config
from ktx_korail import PatchedKorail
from recovery import RecoveryController

# 정상 폴링 간격(랜덤). 속도제한을 덜 건드리도록 보수적으로 잡는다(안정성 우선 1.5배).
MIN_INTERVAL = 3.0
MAX_INTERVAL = 90.0
# 세션을 만료 전에 미리 갱신해 만료발(發) 오류를 예방한다(선제 재로그인).
SESSION_MAX_AGE = 600.0
# 정상 검색이 이 시간 이상 끊기면 세션이 꼬인 것으로 보고 강제로 새 세션을 만든다.
STALL_LIMIT = 240.0
LOG_LIMIT = 500


def _safe_err(e: BaseException) -> str:
    """예외를 안전하게 문자열로 변환한다.

    일부 라이브러리는 네트워크 오류를 예외객체(문자열 아님)로 .msg에 감싸 던져,
    예외의 __str__가 비문자열을 반환해 `str(e)` 호출 자체가 TypeError를 던지는
    경우가 있다(폴링 스레드 사망). SRT 워커와 동일하게 안전 변환으로 막는다.
    """
    msg = getattr(e, "msg", None)
    if msg is not None and not isinstance(msg, str):
        return f"{type(e).__name__}: {msg!r}"
    try:
        return str(e)
    except Exception:
        return repr(e)


class JobStatus(str, Enum):
    PENDING = "pending"
    POLLING = "polling"
    RESERVED = "reserved"
    PAID = "paid"
    STOPPED = "stopped"
    ERROR = "error"


class PayMode(str, Enum):
    AUTO = "auto"
    MANUAL = "manual"


TRAIN_TYPE_MAP = {
    "ktx": TrainType.KTX,
    "itx-saemaeul": TrainType.ITX_SAEMAEUL,
    "mugunghwa": TrainType.MUGUNGHWA,
    "nuriro": TrainType.NURIRO,
    "tonggeun": TrainType.TONGGUEN,
    "itx-cheongchun": TrainType.ITX_CHEONGCHUN,
    "all": TrainType.ALL,
}


def _train_id(t) -> str:
    """Stable selector: train_type + train_no + dep_date."""
    return f"{t.train_type}|{t.train_no}|{t.dep_date}"


@dataclass
class JobSpec:
    dep: str
    arr: str
    date: str
    time: str
    train_id: Optional[str]
    train_type: str
    passengers: int
    seat_pref: str  # "general" | "special" | "any"
    pay_mode: PayMode
    include_waiting: bool = False


@dataclass
class Job:
    id: str
    spec: JobSpec
    status: JobStatus = JobStatus.PENDING
    created_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    attempts: int = 0
    recoveries: int = 0
    reservation_summary: Optional[str] = None
    reservation_id: Optional[str] = None
    payment_deadline: Optional[str] = None
    error: Optional[str] = None
    logs: Deque[str] = field(default_factory=lambda: deque(maxlen=LOG_LIMIT))
    _stop: threading.Event = field(default_factory=threading.Event)
    _thread: Optional[threading.Thread] = None
    _reservation: object = None
    _pay_event: threading.Event = field(default_factory=threading.Event)

    def log(self, msg: str) -> None:
        self.logs.append(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")


class JobManager:
    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()
        self._counter = 0

    def list(self) -> list[Job]:
        with self._lock:
            return list(self._jobs.values())

    def get(self, job_id: str) -> Optional[Job]:
        return self._jobs.get(job_id)

    def create(self, spec: JobSpec) -> Job:
        with self._lock:
            self._counter += 1
            jid = f"k{self._counter}"
        job = Job(id=jid, spec=spec)
        self._jobs[jid] = job
        t = threading.Thread(target=self._run, args=(job,), daemon=True, name=f"ktx-{jid}")
        job._thread = t
        t.start()
        return job

    def stop(self, job_id: str) -> bool:
        job = self._jobs.get(job_id)
        if not job:
            return False
        job._stop.set()
        job._pay_event.set()
        return True

    def confirm_pay(self, job_id: str) -> bool:
        job = self._jobs.get(job_id)
        if not job or job.status != JobStatus.RESERVED:
            return False
        job._pay_event.set()
        return True

    def _run(self, job: Job) -> None:
        creds = config.ktx.load()
        if not creds:
            job.status = JobStatus.ERROR
            job.error = "credentials not configured"
            job.log("ERROR: credentials missing")
            return

        def _new_client() -> PatchedKorail:
            c = PatchedKorail(creds.ktx_id, creds.ktx_password, auto_login=False)
            if not c.login():
                raise RuntimeError("login returned False")
            # 폴링 루프의 모든 HTTP 호출에 타임아웃 강제 (없으면 서버가 연결을
            # 물고 있을 때 search_train 무한 대기 → 스레드 멈춤).
            _force_session_timeout(c._session, 25)
            return c

        try:
            client = _new_client()
            session_started = time.monotonic()
        except Exception as e:
            job.status = JobStatus.ERROR
            job.error = f"login failed: {_safe_err(e)}"
            job.log(f"login failed: {_safe_err(e)}")
            return

        job.log(
            f"login ok ({getattr(client, 'name', creds.ktx_id)}); "
            f"polling {job.spec.dep}->{job.spec.arr} {job.spec.date} {job.spec.time} "
            f"type={job.spec.train_type}"
        )
        job.status = JobStatus.POLLING

        seat_option = self._seat_pref_to_option(job.spec.seat_pref)
        train_type = TRAIN_TYPE_MAP.get(job.spec.train_type.lower(), TrainType.KTX)
        passengers = [AdultPassenger(job.spec.passengers)]
        rc = RecoveryController()
        last_ok = time.monotonic()

        def _handle_antibot(msg: str) -> float:
            """안티봇/속도제한 차단 처리. 연속 실패에 비례한 백오프를 돌려준다.
            빠른 재시도는 차단을 연장하므로 즉시 재시도하지 않는다. 일정 횟수마다
            완전 새 세션으로 재로그인한다."""
            nonlocal client, session_started
            rec = rc.on_error()
            job.recoveries += 1
            if rec.fresh_login:
                job.log(f"안티봇 차단 {rec.streak}회 연속 → 완전 새 세션 + {rec.sleep:.0f}s 대기")
                try:
                    client = _new_client()
                    session_started = time.monotonic()
                except Exception as e2:
                    job.log(f"새 세션 실패(대기 후 재시도): {_safe_err(e2)}")
            else:
                job.log(
                    f"안티봇 차단 #{rec.streak} (속도제한) → "
                    f"{rec.sleep:.0f}s 백오프 후 재시도: {msg[:60]}"
                )
            return rec.sleep

        while not job._stop.is_set():
            job.attempts += 1
            next_sleep: Optional[float] = None

            # 선제 세션 갱신: 오래된 세션은 만료로 오류 나기 전에 미리 새로 로그인
            if time.monotonic() - session_started > SESSION_MAX_AGE:
                job.log("세션 선제 갱신(만료 예방) → 재로그인")
                try:
                    client = _new_client()
                    session_started = time.monotonic()
                except Exception as e:
                    job.log(f"선제 재로그인 실패: {_safe_err(e)}")

            try:
                trains = client.search_train(
                    job.spec.dep, job.spec.arr,
                    job.spec.date, job.spec.time,
                    train_type=train_type,
                    include_no_seats=True,
                    include_waiting_list=job.spec.include_waiting,
                )
                rc.on_success()
                last_ok = time.monotonic()
                target = self._pick_target(trains, job.spec)
                if target is None:
                    job.log(f"#{job.attempts} target not found")
                else:
                    gen = target.has_general_seat()
                    spc = target.has_special_seat()
                    job.log(f"#{job.attempts} {target.train_no} general={gen} special={spc}")
                    if self._can_take(gen, spc, job.spec.seat_pref):
                        try:
                            res = client.reserve(target, passengers=passengers, option=seat_option)
                        except SoldOutError:
                            job.log("reserve race lost (sold out)")
                        except KorailError as e:
                            job.log(f"reserve error: {_safe_err(e)}")
                        else:
                            job._reservation = res
                            job.reservation_summary = str(res)
                            job.reservation_id = getattr(res, "rsv_id", None)
                            d = getattr(res, "buy_limit_date", None)
                            t = getattr(res, "buy_limit_time", None)
                            if d and t and d != "00000000":
                                job.payment_deadline = (
                                    f"{d[:4]}-{d[4:6]}-{d[6:8]} {t[:2]}:{t[2:4]}:{t[4:6]}"
                                )
                            job.status = JobStatus.RESERVED
                            job.log(f"RESERVED: {res}")
                            job.log(f"deadline: {job.payment_deadline}")
                            self._handle_payment(client, job, creds)
                            return
            except NoResultsError:
                job.log(f"#{job.attempts} no results")
                rc.on_success()
                last_ok = time.monotonic()
            except NeedToLoginError:
                job.log("세션 만료 감지 → 재로그인")
                try:
                    client = _new_client()
                    session_started = time.monotonic()
                    last_ok = time.monotonic()
                    rc.on_success()
                except Exception as e:
                    job.log(f"재로그인 실패: {_safe_err(e)}")
            except KorailError as e:
                msg = _safe_err(e)
                if any(p in msg for p in ("MACRO", "원활한 서비스", "최신 버전")):
                    next_sleep = _handle_antibot(msg)
                else:
                    job.log(f"korail error: {msg[:120]}")
            except Exception as e:
                # str(e)가 비문자열 msg를 감싼 예외에서 TypeError를 던져 스레드가
                # 죽는 것을 막는다(SRT 워커와 동일 가드).
                job.log(f"poll error: {_safe_err(e)}")

            # 정상 검색이 너무 오래 끊기면 세션이 꼬인 것 → 강제로 새 세션
            if next_sleep is None and time.monotonic() - last_ok > STALL_LIMIT:
                job.log(f"검색 {int(STALL_LIMIT)}s+ 정체 → 강제 새 세션")
                job.recoveries += 1
                try:
                    client = _new_client()
                    session_started = time.monotonic()
                    last_ok = time.monotonic()
                    rc.on_success()
                except Exception as e:
                    job.log(f"강제 재로그인 실패: {_safe_err(e)}")

            sleep_for = next_sleep if next_sleep is not None else random.uniform(MIN_INTERVAL, MAX_INTERVAL)
            job.log(f"sleep {sleep_for:.1f}s")
            if job._stop.wait(sleep_for):
                break

        if job.status == JobStatus.POLLING:
            job.status = JobStatus.STOPPED
            job.log("stopped")

    def _handle_payment(self, client: PatchedKorail, job: Job, creds: config.KTXCredentials) -> None:
        if job.spec.pay_mode == PayMode.MANUAL:
            job.log("manual mode: waiting for user '결제 진행' (~9min)")
            if job._pay_event.wait(timeout=540):
                if job._stop.is_set():
                    job.log("stopped before payment")
                    return
                job.log("user confirmed → charging card")
                self._pay(client, job, creds)
            else:
                job.status = JobStatus.ERROR
                job.error = "payment confirmation timeout"
                job.log("ERROR: confirm timeout (~9min); reservation likely auto-cancelled")
            return

        if not creds.card_number:
            job.status = JobStatus.ERROR
            job.error = "auto pay requested but card not configured"
            job.log("ERROR: auto pay requires card info")
            return
        job.log("auto-pay → charging card")
        self._pay(client, job, creds)

    def _pay(self, client: PatchedKorail, job: Job, creds: config.KTXCredentials) -> None:
        if not creds.card_number:
            job.status = JobStatus.ERROR
            job.error = "card info missing"
            job.log("ERROR: card info not in keychain")
            return
        try:
            ok = client.pay_with_card(
                job._reservation,
                card_number=creds.card_number,
                card_password=creds.card_password,
                birthday=creds.card_validation,
                card_expire=creds.card_expire,
                installment=creds.card_installment,
                card_type="J",
            )
        except Exception as e:
            job.status = JobStatus.ERROR
            job.error = f"pay error: {_safe_err(e)}"
            job.log(f"ERROR: pay error: {_safe_err(e)}")
            return
        if ok:
            job.status = JobStatus.PAID
            job.log("PAID OK")
        else:
            job.status = JobStatus.ERROR
            job.error = "pay_with_card returned False"
            job.log("ERROR: pay_with_card returned False")

    @staticmethod
    def _pick_target(trains, spec: JobSpec):
        if spec.train_id:
            for t in trains:
                if _train_id(t) == spec.train_id:
                    return t
            return None
        return trains[0] if trains else None

    @staticmethod
    def _can_take(gen: bool, spc: bool, pref: str) -> bool:
        if pref == "general":
            return gen
        if pref == "special":
            return spc
        return gen or spc

    @staticmethod
    def _seat_pref_to_option(pref: str):
        if pref == "special":
            return ReserveOption.SPECIAL_FIRST
        if pref == "general":
            return ReserveOption.GENERAL_FIRST
        return ReserveOption.GENERAL_FIRST


manager = JobManager()


def _force_session_timeout(session, seconds: float) -> None:
    if getattr(session, "_kt_timeout_patched", False):
        return
    orig = session.request
    def request(method, url, **kw):
        kw.setdefault("timeout", seconds)
        return orig(method, url, **kw)
    session.request = request
    session._kt_timeout_patched = True


def search_preview(dep: str, arr: str, date: str, time_: str, train_type: str = "ktx") -> list[dict]:
    creds = config.ktx.load()
    if not creds:
        raise RuntimeError("credentials not configured")
    client = PatchedKorail(creds.ktx_id, creds.ktx_password, auto_login=False)
    _force_session_timeout(client._session, 25)
    if not client.login():
        raise RuntimeError("login failed")
    tt = TRAIN_TYPE_MAP.get(train_type.lower(), TrainType.KTX)
    try:
        trains = client.search_train(dep, arr, date, time_, train_type=tt, include_no_seats=True)
    except NoResultsError:
        return []
    out = []
    for t in trains[:25]:
        out.append({
            "train_id": _train_id(t),
            "train_no": t.train_no,
            "label": str(t),
            "general": t.has_general_seat(),
            "special": t.has_special_seat(),
        })
    return out
