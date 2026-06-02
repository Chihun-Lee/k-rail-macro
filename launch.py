"""Windows 단일 exe 진입점 (SRT + KTX 통합).

서버를 127.0.0.1:8912에 띄우고, 잠시 뒤 기본 브라우저로 GUI를 연다.
이 콘솔 창을 닫으면(또는 Ctrl+C) 서버가 종료된다.

PyInstaller --onefile로 묶일 때를 위한 엔트리이며, import string 대신
app 객체를 직접 넘겨 frozen 환경에서 안전하게 동작한다.
"""
from __future__ import annotations

import threading
import webbrowser

import uvicorn

from server import app

URL = "http://127.0.0.1:8912"


def _open_browser() -> None:
    webbrowser.open(URL)


if __name__ == "__main__":
    print("=" * 56)
    print("  기차 매크로 (SRT + KTX) 실행 중")
    print(f"  브라우저에서 {URL} 가 열립니다.")
    print("  종료하려면 이 창을 닫거나 Ctrl+C 를 누르세요.")
    print("=" * 56)
    threading.Timer(1.5, _open_browser).start()
    uvicorn.run(app, host="127.0.0.1", port=8912, log_level="warning")
