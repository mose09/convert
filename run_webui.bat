@echo off
REM ==========================================================================
REM 로컬 실행 화면 (SQL 마이그레이션 + 용어검색) 실행 런처.
REM 더블클릭하면 로컬 Streamlit 서버가 뜨고 기본 브라우저가 자동으로 열린다.
REM 외부 네트워크로 아무것도 전송하지 않는다 (localhost 전용).
REM
REM 사전 준비 (최초 1회):
REM   python -m pip install --no-index --find-links=.\wheels streamlit
REM   (폐쇄망이 아니면: python -m pip install streamlit)
REM ==========================================================================
cd /d "%~dp0"

REM 외부 사용량 수집 끄고 headless(자동 브라우저 오픈은 아래 start) 로 기동
python -m streamlit run webui\app.py --server.port 8501 --browser.gatherUsageStats false

pause
