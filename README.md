# K-Rail 매크로 (K-Rail Macro)

**SRT + KTX 통합** 매크로. 한 화면에 두 탭, 동시 실행 가능.

> ⚠ **개인용 한정.** 본인 SRT/코레일 계정·본인 카드로만 사용하세요. 자격증명·카드정보는 **macOS Keychain**에 암호화 저장됩니다. 서버는 `127.0.0.1:8912`에만 바인딩됩니다.

---

## 친구한테 보낼 1줄 가이드 (설치)

친구가 본인 Mac에서 **터미널을 열어** 아래 한 줄 붙여넣고 엔터:

```bash
curl -fsSL https://raw.githubusercontent.com/Chihun-Lee/k-rail-macro/main/install.sh | bash
```

> 또는 [`K-Rail_매크로_설치.command`](https://github.com/Chihun-Lee/k-rail-macro/raw/main/K-Rail_매크로_설치.command) 다운로드 → Finder에서 **우클릭 → 열기**

설치 끝나면 **Launchpad → "K-Rail 매크로"** 검색 → 더블클릭. 종료는 **"K-Rail 매크로 종료"**.

---

## 기능

- **하나의 웹앱에 SRT 탭 + KTX 탭** — 둘이 완전히 독립, 동시 실행 가능
- 폴링 간격: **1~30초 균등 랜덤**
- 결제 모드: 수동 (사용자 확인) / 자동 (즉시 결제)
- anti-bot 자동 회복:
  - SRT NetFunnel "Wrong Server ID" → 캐시 무효화 + 클라이언트 재생성
  - KTX MACRO ERROR → 클라이언트 재생성 (Dynapath 우회 토큰 자동 갱신)
- **표 잡을 때까지 안 멈춤** (세션 중단 방지 4중 장치):
  - 로그인 실패·인터넷 끊김 → 백오프 후 무한 재시도 (ERROR로 죽지 않음)
  - 감시자(watchdog)가 30초마다 검사 → 죽거나 멈춘 폴링 스레드 자동 재시작
  - 활성 잡을 `~/.k-rail-macro/jobs.json`에 저장 → 서버가 죽어도 재시작 시 자동 복원
  - macOS: 서버 크래시 시 2초 후 자동 재기동(`run_supervised.sh`) + 유휴 절전 방지(`caffeinate`)
  - 수동결제 확인 시간초과(~9분)로 예약이 자동취소되면 → 폴링 자동 재개
- **뚜껑 닫아도 계속** (macOS, 선택): `bash setup_lid_mode.sh` 를 한 번 실행하면
  (관리자 비밀번호 1회) 이후 **활성 잡이 도는 동안만** `pmset disablesleep`을 자동으로
  켜서 뚜껑을 닫아도 폴링이 계속된다. 잡이 없으면 자동으로 꺼져 평소 배터리엔 영향 없음.
  ⚠ 잡 도는 중 뚜껑 닫은 채 가방에 넣으면 발열 주의. 해제:
  `sudo rm /etc/sudoers.d/k-rail-pmset && sudo pmset -a disablesleep 0`
- KTX는 KTX/ITX-새마을/무궁화호/누리로/ITX-청춘 모두 지원
- 토스트 알림 + 실시간 로그
- 자격증명/잡 모두 SRT·KTX 별도 관리 (Keychain 항목 분리)

### 카드 테스트
둘 다 25일 뒤 평일 첫차를 reserve→pay→refund 하며, 4겹 안전장치 (snapshot · PNR 일치 · route/date 검증 · post-audit)로 **남의 표 환불을 차단**한다. 위약금 약 400원/회.
- **KTX**: 활성. 서울→광명. 자동 환불 신뢰성 높음.
- **SRT**: 활성(주의). 김천(구미)→동대구. SRT `reserve_info`가 referer를 무시하고 다른 표를 돌려줄 수 있어 **자동 환불이 실패할 수 있음** — 그 경우 결제만 되고, 화면의 PNR을 SRT 앱에서 직접 환불해야 한다(안전장치가 잘못된 표 환불은 막음). 같은 카드를 KTX로 검증하면 더 안전.

## 기존 SRT/KTX 단독 사용자

- Keychain 항목 이름이 같음 (`srt-macro` / `ktx-macro`) → **저장한 자격증명 그대로 마이그레이션됨**
- 단독 매크로(8910 / 8911)와 통합 매크로(8912)는 다른 포트라 동시에 실행해도 충돌 없음
- 단독 매크로 안 쓸 거면 `~/Applications/SRT 매크로.app` / `KTX 매크로.app` 삭제 + `kill $(lsof -ti tcp:8910 -sTCP:LISTEN)` 등으로 정리

---

## 직접 빌드 / 개발

```bash
git clone https://github.com/Chihun-Lee/k-rail-macro.git
cd k-rail-macro
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python server.py
# → http://127.0.0.1:8912
```

### 파일 구조

| 파일 | 용도 |
|------|------|
| `server.py` | FastAPI 엔트리, `/api/srt/*` + `/api/ktx/*` 라우팅 |
| `srt_worker.py` | SRT polling/reserve/pay (SRTrain) |
| `ktx_worker.py` | KTX polling/reserve/pay (srtgo) |
| `ktx_korail.py` | srtgo Korail + Dynapath bypass |
| `config.py` | 두 namespace (`config.srt`, `config.ktx`) Keychain 저장 |
| `jobstore.py` | 활성 잡 디스크 저장/복원 (서버 재시작 시 자동 재개) |
| `run_supervised.sh` | macOS 서버 감시 루프 (죽으면 자동 재시작) |
| `setup_lid_mode.sh` | 뚜껑 닫아도 잡 유지용 1회 설정 (pmset sudoers) |
| `static/index.html` | 탭 UI, 두 서비스 공통 JS |
| `install.sh` | 친구용 원클릭 설치 |

### 라이선스 / 출처

- [SRTrain](https://github.com/ryanking13/SRT) (MIT) — SRT 클라이언트
- [srtgo](https://github.com/lapis42/srtgo) (MIT) — KTX `pay_with_card` 구현
- Dynapath bypass — [nomadamas/k-skill](https://github.com/nomadamas/k-skill) (MIT)
