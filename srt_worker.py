"""Background polling/booking worker.

Each Job:
- searches the target SRT train at randomized intervals (1~30s)
- when a seat opens, reserves
- if mode=auto: pays immediately with stored card
- if mode=manual: stops and waits for user "결제 진행" command
"""
from __future__ import annotations

import random
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import Enum
from typing import Deque, Optional

from SRT import SRT, Adult, SeatType
from SRT.errors import SRTError, SRTNotLoggedInError, SRTNetFunnelError
from SRT.netfunnel import NetFunnelHelper

import config
import jobstore
from recovery import RecoveryController

# 정상 폴링 간격(랜덤). 속도제한을 덜 건드리도록 보수적으로 잡는다(안정성 우선 1.5배).
MIN_INTERVAL = 3.0
MAX_INTERVAL = 90.0
# 세션을 만료 전에 미리 갱신해 만료발(發) 오류를 예방한다(선제 재로그인).
SESSION_MAX_AGE = 600.0
# 정상 검색이 이 시간 이상 끊기면 세션이 꼬인 것으로 보고 강제로 새 세션을 만든다.
STALL_LIMIT = 240.0
LOG_LIMIT = 500
# 감시자(watchdog): 활성 잡의 스레드가 죽거나 멈추면 자동 재시작한다.
WATCHDOG_PERIOD = 30.0
# 최대 정상 sleep(90s) + HTTP 타임아웃(25s) 몇 번을 크게 웃도는 값 — 이보다
# 오래 심장박동이 없으면 스레드가 되살아날 수 없는 상태로 멈춘 것으로 본다.
HEARTBEAT_STALE = 480.0


def _safe_err(e: BaseException) -> str:
    """예외를 안전하게 문자열로 변환한다.

    SRT 라이브러리는 네트워크 오류를 `raise SRTNetFunnelError(e)`로 감싸서
    .msg에 예외 객체(문자열 아님)를 넣는다. 이 경우 SRTNetFunnelError.__str__가
    비문자열을 반환해 `str(e)` 호출 자체가 TypeError를 던진다(폴링 스레드 사망).
    그래서 str(e)를 직접 부르지 않고 안전하게 변환한다.
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


@dataclass
class JobSpec:
    dep: str
    arr: str
    date: str  # YYYYMMDD
    time: str  # HHMMSS
    train_number: Optional[str]  # if set, only match this train
    passengers: int
    seat_pref: str  # "general" | "special" | "any"
    pay_mode: PayMode


@dataclass
class Job:
    id: str
    spec: JobSpec
    status: JobStatus = JobStatus.PENDING
    created_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    attempts: int = 0
    recoveries: int = 0
    reservation_summary: Optional[str] = None
    payment_deadline: Optional[str] = None
    error: Optional[str] = None
    logs: Deque[str] = field(default_factory=lambda: deque(maxlen=LOG_LIMIT))
    last_beat: float = field(default_factory=time.monotonic)
    _stop: threading.Event = field(default_factory=threading.Event)
    _thread: Optional[threading.Thread] = None
    _gen: int = 0  # 감시자 재시작 세대. 옛 스레드는 세대가 바뀌면 스스로 종료.
    _reservation: object = None  # SRTReservation when reserved
    _pay_event: threading.Event = field(default_factory=threading.Event)

    def log(self, msg: str) -> None:
        self.logs.append(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")

    def beat(self) -> None:
        self.last_beat = time.monotonic()


class JobManager:
    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()
        self._counter = 0
        self._watchdog: Optional[threading.Thread] = None

    def list(self) -> list[Job]:
        with self._lock:
            return list(self._jobs.values())

    def get(self, job_id: str) -> Optional[Job]:
        return self._jobs.get(job_id)

    def create(self, spec: JobSpec, persist: bool = True) -> Job:
        with self._lock:
            self._counter += 1
            jid = f"j{self._counter}"
        job = Job(id=jid, spec=spec)
        self._jobs[jid] = job
        self._ensure_watchdog()
        self._spawn(job)
        if persist:
            self._persist()
        return job

    def stop(self, job_id: str) -> bool:
        job = self._jobs.get(job_id)
        if not job:
            return False
        job._stop.set()
        job._pay_event.set()
        self._persist()
        return True

    def _spawn(self, job: Job) -> None:
        job._gen += 1
        job.beat()
        t = threading.Thread(
            target=self._run, args=(job, job._gen), daemon=True,
            name=f"srt-{job.id}-g{job._gen}",
        )
        job._thread = t
        t.start()

    def _ensure_watchdog(self) -> None:
        if self._watchdog is not None and self._watchdog.is_alive():
            return
        self._watchdog = threading.Thread(target=self._watch, daemon=True, name="srt-watchdog")
        self._watchdog.start()

    def _watch(self) -> None:
        """스레드가 죽었거나(예외) 멈춘(심장박동 정지) 활성 잡을 자동 재시작한다.

        세대 토큰(_gen)을 올리고 새 스레드를 띄우면 옛 스레드는 다음 루프에서
        세대 불일치를 보고 스스로 빠진다. 진짜로 시스템콜에 갇힌 스레드는 죽일
        수 없지만, 모든 HTTP 호출에 타임아웃이 강제돼 있어 결국 풀려나 종료된다.
        """
        while True:
            time.sleep(WATCHDOG_PERIOD)
            for job in self.list():
                if job._stop.is_set() or job.status not in (JobStatus.PENDING, JobStatus.POLLING):
                    continue
                dead = job._thread is None or not job._thread.is_alive()
                stuck = time.monotonic() - job.last_beat > HEARTBEAT_STALE
                if dead or stuck:
                    job.recoveries += 1
                    job.log(f"감시자: {'스레드 사망' if dead else '응답 없음(멈춤)'} 감지 → 자동 재시작")
                    self._spawn(job)

    def _persist(self) -> None:
        active = [
            asdict(j.spec) for j in self.list()
            if not j._stop.is_set()
            and j.status in (JobStatus.PENDING, JobStatus.POLLING, JobStatus.RESERVED)
        ]
        jobstore.save("srt", active)

    def restore(self) -> int:
        """이전 서버 프로세스가 남긴 활성 잡을 되살린다(서버 시작 시).

        같은 프로세스에서 서버가 재기동돼 이미 동일 잡이 돌고 있으면
        건너뛴다(중복 폴링/중복 예매 방지).
        """
        existing = [
            asdict(j.spec) for j in self.list()
            if not j._stop.is_set()
            and j.status in (JobStatus.PENDING, JobStatus.POLLING, JobStatus.RESERVED)
        ]
        n = 0
        for d in jobstore.load("srt"):
            if d in existing:
                continue
            try:
                d["pay_mode"] = PayMode(d["pay_mode"])
                job = self.create(JobSpec(**d), persist=False)
            except Exception:
                continue
            job.log("서버 재시작 감지 → 저장된 작업 자동 복원")
            n += 1
        self._persist()
        return n

    def confirm_pay(self, job_id: str) -> bool:
        job = self._jobs.get(job_id)
        if not job or job.status != JobStatus.RESERVED:
            return False
        job._pay_event.set()
        return True

    def _run(self, job: Job, gen: int) -> None:
        creds = config.srt.load()
        if not creds:
            job.status = JobStatus.ERROR
            job.error = "credentials not configured"
            job.log("ERROR: credentials missing")
            self._persist()
            return

        def active() -> bool:
            return not job._stop.is_set() and job._gen == gen

        # 어떤 예외로 폴링 루프가 깨져도 표를 잡기 전에는 스레드가 죽지 않는다.
        # 종료는 사용자 정지, 결제 흐름 종료(성공/오류), 감시자 교체뿐이다.
        while active():
            try:
                if self._poll_loop(job, gen, creds, active):
                    break
            except Exception as e:
                job.recoveries += 1
                job.log(f"워커 오류 → 15s 후 자동 재시작: {_safe_err(e)}")
                job._stop.wait(15.0)

        if job._gen != gen:
            return  # 감시자가 새 스레드로 교체함 — 상태는 새 스레드가 관리
        if job.status in (JobStatus.PENDING, JobStatus.POLLING):
            job.status = JobStatus.STOPPED
            job.log("stopped")
        self._persist()

    def _poll_loop(self, job: Job, gen: int, creds: config.SRTCredentials, active) -> bool:
        """폴링 본체. True면 작업 종료(결제 흐름 완료/정지), False면 재시작 대상."""

        def _new_client() -> SRT:
            # 자동로그인 생성자는 타임아웃 없는 로그인 호출을 해서 인터넷이
            # 불안정하면 스레드가 영원히 멈춘다 → 로그인 전에 세션 타임아웃부터
            # 강제한다. (폴링 루프의 모든 HTTP 호출에도 같은 타임아웃 적용 —
            # 없으면 서버가 연결을 물고 안 놓을 때 search_train 무한 대기.)
            c = SRT(creds.srt_id, creds.srt_password, auto_login=False)
            _force_session_timeout(c._session, 25)
            helper = getattr(c, "netfunnel_helper", None)
            if helper is not None and hasattr(helper, "session"):
                _force_session_timeout(helper.session, 25)
            c.login()
            return c

        # 로그인은 실패해도 포기하지 않는다(인터넷 불안정 대비, 표 잡을 때까지).
        login_rc = RecoveryController(base=5.0, cap=120.0, fresh_login_every=10 ** 9)
        srt: Optional[SRT] = None
        while active() and srt is None:
            job.beat()
            try:
                srt = _new_client()
            except Exception as e:
                rec = login_rc.on_error()
                job.log(f"로그인 실패 #{rec.streak} → {rec.sleep:.0f}s 후 재시도: {_safe_err(e)}")
                job._stop.wait(rec.sleep)
        if srt is None:
            return False  # 정지/교체 요청됨 → 상위에서 정리
        session_started = time.monotonic()

        job.log(f"login ok ({creds.srt_id}); polling {job.spec.dep}->{job.spec.arr} {job.spec.date} {job.spec.time}")
        job.status = JobStatus.POLLING

        seat_choice = self._seat_pref_to_enum(job.spec.seat_pref)
        # netfunnel 차단은 두 양상이 섞여 있다: ① 대부분은 '일시적 세션 거부'라
        # helper 세션만 새로 만들면 바로 회복된다 ② 일부는 IP 속도제한이라 빠른
        # 재시도가 차단을 연장한다. 그래서 base를 짧게(1.5s) 잡아 초반엔 빠르게
        # 재시도하되, 연속 실패가 쌓이면 지수적으로 물러나고 5회째 완전 새 세션으로
        # 에스컬레이션한다(두 이론의 절충).
        rc = RecoveryController(base=1.5, cap=30.0, fresh_login_every=5)
        last_ok = time.monotonic()

        def _handle_netfunnel(e: Exception) -> float:
            """NetFunnel 차단(gRtype=4999 등) 처리.

            캐시 키만 비워서는 회복되지 않는다(거부된 쿠키가 helper 세션에 남기
            때문). 그래서 helper 자체를 매번 새로 만들어 requests 세션(쿠키)과
            캐시 키를 모두 초기화한다 — 재로그인보다 훨씬 가볍다. 그 위에 연속
            실패 비례 백오프를 더하고, 일정 횟수마다 완전 새 세션으로 올린다."""
            nonlocal srt, session_started
            try:
                srt.netfunnel_helper = NetFunnelHelper()
                h = srt.netfunnel_helper
                if hasattr(h, "session"):
                    _force_session_timeout(h.session, 25)
            except Exception as e2:
                job.log(f"netfunnel helper 재생성 실패: {_safe_err(e2)}")
            rec = rc.on_error()
            job.recoveries += 1
            if rec.fresh_login:
                job.log(
                    f"netfunnel 차단 {rec.streak}회 연속 → 완전 새 세션 + {rec.sleep:.0f}s 대기"
                )
                try:
                    srt = _new_client()
                    session_started = time.monotonic()
                except Exception as e2:
                    job.log(f"새 세션 실패(대기 후 재시도): {_safe_err(e2)}")
            else:
                job.log(
                    f"netfunnel 차단 #{rec.streak} → helper 재생성 + "
                    f"{rec.sleep:.0f}s 백오프 후 재시도: {_safe_err(e)[:60]}"
                )
            return rec.sleep

        while active():
            job.beat()
            job.attempts += 1
            next_sleep: Optional[float] = None

            # 선제 세션 갱신: 오래된 세션은 만료로 오류 나기 전에 미리 새로 로그인
            if time.monotonic() - session_started > SESSION_MAX_AGE:
                job.log("세션 선제 갱신(만료 예방) → 재로그인")
                try:
                    srt = _new_client()
                    session_started = time.monotonic()
                except Exception as e:
                    job.log(f"선제 재로그인 실패: {_safe_err(e)}")

            try:
                trains = srt.search_train(
                    job.spec.dep, job.spec.arr, job.spec.date, job.spec.time,
                    available_only=False,
                )
                rc.on_success()
                last_ok = time.monotonic()
                target = self._pick_target(trains, job.spec)
                if target is None:
                    job.log(f"#{job.attempts} target not found")
                else:
                    gen = target.general_seat_available()
                    spc = target.special_seat_available()
                    job.log(f"#{job.attempts} {target.train_number} general={gen} special={spc}")
                    if self._can_take(gen, spc, job.spec.seat_pref):
                        seat = self._reserve_seat(gen, spc, seat_choice)
                        passengers = [Adult(job.spec.passengers)]
                        try:
                            res = srt.reserve(target, passengers=passengers, special_seat=seat)
                        except SRTError as e:
                            # raced with another buyer; keep polling
                            job.log(f"reserve race lost: {_safe_err(e)}")
                        else:
                            job._reservation = res
                            job.reservation_summary = str(res)
                            job.payment_deadline = (
                                f"{getattr(res, 'payment_date', '?')} {getattr(res, 'payment_time', '')}".strip()
                            )
                            job.status = JobStatus.RESERVED
                            job.log(f"RESERVED: {res}")
                            job.log(f"deadline: {job.payment_deadline}")
                            if self._handle_payment(srt, job, creds):
                                return True
                            # 결제확인 시간초과 → SRT가 예약을 자동취소하므로
                            # 처음 상태로 되돌려 표잡기를 재개한다(잡을 때까지).
                            job._pay_event.clear()
                            job._reservation = None
                            job.reservation_summary = None
                            job.payment_deadline = None
                            job.status = JobStatus.POLLING
            except SRTNotLoggedInError:
                job.log("세션 만료 감지 → 재로그인")
                try:
                    srt = _new_client()
                    session_started = time.monotonic()
                    last_ok = time.monotonic()
                    rc.on_success()
                except Exception as e:
                    job.log(f"재로그인 실패: {_safe_err(e)}")
            except SRTNetFunnelError as e:
                next_sleep = _handle_netfunnel(e)
            except Exception as e:
                # SRTNetFunnelError가 SRTResponseError 등으로 감싸여 올 수 있어
                # 타입과 메시지를 함께 본다. str(e)를 직접 부르면 비문자열 msg를
                # 감싼 예외에서 TypeError가 나 스레드가 죽으므로 _safe_err를 쓴다.
                err_text = _safe_err(e)
                if isinstance(e, SRTNetFunnelError) or "NetFunnel" in err_text:
                    next_sleep = _handle_netfunnel(e)
                else:
                    job.log(f"poll error: {err_text}")

            # 정상 검색이 너무 오래 끊기면 세션이 꼬인 것 → 강제로 새 세션
            if next_sleep is None and time.monotonic() - last_ok > STALL_LIMIT:
                job.log(f"검색 {int(STALL_LIMIT)}s+ 정체 → 강제 새 세션")
                job.recoveries += 1
                try:
                    srt = _new_client()
                    session_started = time.monotonic()
                    last_ok = time.monotonic()
                    rc.on_success()
                except Exception as e:
                    job.log(f"강제 재로그인 실패: {_safe_err(e)}")

            sleep_for = next_sleep if next_sleep is not None else random.uniform(MIN_INTERVAL, MAX_INTERVAL)
            job.log(f"sleep {sleep_for:.1f}s")
            job.beat()
            if job._stop.wait(sleep_for):
                break

        return False  # 정지/교체 요청 → 상위에서 정리

    def _handle_payment(self, srt: SRT, job: Job, creds: config.SRTCredentials) -> bool:
        """결제 흐름. True=작업 종료(결제/오류/정지), False=폴링 재개(확인 시간초과)."""
        if job.spec.pay_mode == PayMode.AUTO:
            job.log("auto-pay enabled, charging card now")
            self._pay(srt, job, creds)
            return True

        job.log("manual mode: waiting for user '결제 진행' command (or stop)")
        # wait up to 9 minutes (SRT gives ~10 min, leave a margin)
        if job._pay_event.wait(timeout=540):
            if job._stop.is_set():
                job.log("stopped before payment")
                return True
            job.log("user confirmed, charging card now")
            self._pay(srt, job, creds)
            return True
        job.log("결제확인 시간초과(~9분) → 예약은 자동취소됨. 표잡기 폴링 재개")
        return False

    def _pay(self, srt: SRT, job: Job, creds: config.SRTCredentials) -> None:
        try:
            ok = srt.pay_with_card(
                job._reservation,
                number=creds.card_number,
                password=creds.card_password,
                validation_number=creds.card_validation,
                expire_date=creds.card_expire,
                installment=creds.card_installment,
                card_type=creds.card_type,
            )
            if ok:
                job.status = JobStatus.PAID
                job.log("PAID OK")
            else:
                job.status = JobStatus.ERROR
                job.error = "pay_with_card returned False"
                job.log("ERROR: pay_with_card returned False")
        except Exception as e:
            job.status = JobStatus.ERROR
            job.error = f"payment error: {_safe_err(e)}"
            job.log(f"ERROR: payment failed: {_safe_err(e)}")

    @staticmethod
    def _pick_target(trains, spec: JobSpec):
        if spec.train_number:
            for t in trains:
                if t.train_number == spec.train_number:
                    return t
            return None
        # else first train at/after the requested time
        return trains[0] if trains else None

    @staticmethod
    def _can_take(gen: bool, spc: bool, pref: str) -> bool:
        if pref == "general":
            return gen
        if pref == "special":
            return spc
        return gen or spc

    @staticmethod
    def _seat_pref_to_enum(pref: str) -> SeatType:
        if pref == "special":
            return SeatType.SPECIAL_FIRST
        if pref == "general":
            return SeatType.GENERAL_FIRST
        return SeatType.GENERAL_FIRST

    @staticmethod
    def _reserve_seat(gen: bool, spc: bool, fallback: SeatType) -> SeatType:
        if fallback == SeatType.GENERAL_FIRST and gen:
            return SeatType.GENERAL_FIRST
        if fallback == SeatType.SPECIAL_FIRST and spc:
            return SeatType.SPECIAL_FIRST
        # any-mode or fallback: pick whichever is open
        if gen and not spc:
            return SeatType.GENERAL_FIRST
        if spc and not gen:
            return SeatType.SPECIAL_FIRST
        return SeatType.GENERAL_FIRST


manager = JobManager()


def search_preview(dep: str, arr: str, date: str, time_: str) -> list[dict]:
    creds = config.srt.load()
    if not creds:
        raise RuntimeError("credentials not configured")
    # SRTrain's session has no timeout by default; force one BEFORE login so a
    # hanging NetFunnel/login call can't lock the search endpoint forever.
    srt = SRT(creds.srt_id, creds.srt_password, auto_login=False)
    _force_session_timeout(srt._session, 25)
    if hasattr(srt, "netfunnel_helper") and hasattr(srt.netfunnel_helper, "session"):
        _force_session_timeout(srt.netfunnel_helper.session, 25)
    srt.login()
    trains = srt.search_train(dep, arr, date, time_, available_only=False)
    out = []
    for t in trains[:25]:
        out.append({
            "train_number": t.train_number,
            "label": str(t),
            "general": t.general_seat_available(),
            "special": t.special_seat_available(),
        })
    return out


def _force_session_timeout(session, seconds: float) -> None:
    """Wrap session.request so every HTTP call has a default timeout."""
    if getattr(session, "_kt_timeout_patched", False):
        return
    orig = session.request
    def request(method, url, **kw):
        kw.setdefault("timeout", seconds)
        return orig(method, url, **kw)
    session.request = request
    session._kt_timeout_patched = True
