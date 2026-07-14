@echo off
REM ==========================================================================
REM  배포판 실행 런처 (더블클릭).
REM  포터블 파이썬(python\)으로 로컬 Streamlit 서버를 띄우고 브라우저를 연다.
REM  대상 PC 에 파이썬 설치 불필요. 인터넷 없이 localhost 에서만 동작.
REM ==========================================================================
REM  콘솔 코드페이지를 UTF-8(65001)로. 기본 cp949 상태에서 이 파일(UTF-8)의
REM  한글 echo 와 파이썬 UTF-8 출력이 깨지므로 반드시 먼저 전환한다.
chcp 65001 >nul
setlocal
cd /d "%~dp0"
set "PYTHONPATH=%~dp0app"
REM 파이썬 stdout/stderr 도 UTF-8 강제 → 65001 콘솔과 일치(한글 안 깨짐).
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"

echo ============================================================
echo   SQL 마이그레이션 / 스키마 / ERD / 용어 도구
echo ------------------------------------------------------------
echo   잠시 후 웹브라우저가 자동으로 열립니다.
echo   안 열리면 브라우저 주소창에 다음을 입력하세요:
echo        http://localhost:8501
echo   종료: 이 검은 창을 닫거나 Ctrl+C
echo   상세 사용법: 같은 폴더의 guide.html 을 더블클릭
echo ============================================================
echo.

"%~dp0python\python.exe" -m streamlit run "%~dp0app\webui\app.py" ^
    --server.port 8501 --browser.gatherUsageStats false

echo.
echo (서버가 종료되었습니다. 창을 닫아도 됩니다.)
pause
