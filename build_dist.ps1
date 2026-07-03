<#
==========================================================================
 배포판 빌드 스크립트 (Windows PowerShell 에서 1회 실행).

 결과: dist\convert-tool\  (포터블 파이썬 + 앱 + run.bat) 와 압축본
        dist\convert-tool.zip  → 사람들에게 전달.

 사용:
   # 인터넷 되는 PC:
   powershell -ExecutionPolicy Bypass -File build_dist.ps1

   # 폐쇄망 PC (미리 반입한 wheels 폴더 사용):
   #   1) 인터넷 PC 에서 아래를 먼저 받아 이 폴더에 둔다:
   #      - build\python-embed.zip
   #        (https://www.python.org/ftp/python/3.11.9/python-3.11.9-embed-amd64.zip)
   #      - build\get-pip.py  (https://bootstrap.pypa.io/get-pip.py)
   #      - wheels\  (python -m pip download streamlit -r requirements.txt
   #                   -d wheels --platform win_amd64 --python-version 311
   #                   --only-binary=:all:)
   #   2) powershell -ExecutionPolicy Bypass -File build_dist.ps1 -WheelsDir .\wheels

 ⚠ Windows 전용. 리눅스/맥에서는 win_amd64 파이썬을 만들 수 없습니다.
==========================================================================
#>
param(
  [string]$PyVersion = "3.11.9",
  [string]$WheelsDir = ""      # 폐쇄망이면 미리 받은 wheels 폴더 경로
)
$ErrorActionPreference = "Stop"
$root  = $PSScriptRoot
$build = Join-Path $root "build"
$dist  = Join-Path $root "dist\convert-tool"
New-Item -ItemType Directory -Force -Path $build | Out-Null

Write-Host "==> 클린" -ForegroundColor Cyan
if (Test-Path $dist) { Remove-Item $dist -Recurse -Force }
New-Item -ItemType Directory -Force -Path $dist | Out-Null

# 1) 포터블(embeddable) 파이썬 --------------------------------------------
$pyzip = Join-Path $build "python-embed.zip"
if (-not (Test-Path $pyzip)) {
  $url = "https://www.python.org/ftp/python/$PyVersion/python-$PyVersion-embed-amd64.zip"
  Write-Host "==> embeddable python 다운로드: $url" -ForegroundColor Cyan
  Invoke-WebRequest -Uri $url -OutFile $pyzip
}
$pydir = Join-Path $dist "python"
Write-Host "==> python 압축 해제 → $pydir" -ForegroundColor Cyan
Expand-Archive -Path $pyzip -DestinationPath $pydir -Force

# 2) site-packages 활성화 (._pth 편집) ------------------------------------
$pth = Get-ChildItem $pydir -Filter "python*._pth" | Select-Object -First 1
Write-Host "==> site 활성화: $($pth.Name)" -ForegroundColor Cyan
$content = Get-Content $pth.FullName | ForEach-Object { $_ -replace '^\s*#\s*import site', 'import site' }
if ($content -notcontains "Lib\site-packages") { $content += "Lib\site-packages" }
Set-Content -Path $pth.FullName -Value $content -Encoding ASCII

# 3) pip 설치 --------------------------------------------------------------
$getpip = Join-Path $build "get-pip.py"
if (-not (Test-Path $getpip)) {
  Write-Host "==> get-pip.py 다운로드" -ForegroundColor Cyan
  Invoke-WebRequest -Uri "https://bootstrap.pypa.io/get-pip.py" -OutFile $getpip
}
$py = Join-Path $pydir "python.exe"
Write-Host "==> pip 설치" -ForegroundColor Cyan
$getpipArgs = @($getpip, "--no-warn-script-location")
if ($WheelsDir -ne "") { $getpipArgs += @("--no-index", "--find-links", (Resolve-Path $WheelsDir)) }
& $py @getpipArgs

# 4) 의존성 설치 (streamlit + requirements) --------------------------------
Write-Host "==> 의존성 설치 (streamlit + requirements.txt)" -ForegroundColor Cyan
$pipArgs = @("-m", "pip", "install", "--no-warn-script-location",
             "streamlit", "-r", (Join-Path $root "requirements.txt"))
if ($WheelsDir -ne "") { $pipArgs += @("--no-index", "--find-links", (Resolve-Path $WheelsDir)) }
& $py @pipArgs

# 5) 앱 파일 복사 ----------------------------------------------------------
Write-Host "==> 앱 파일 복사" -ForegroundColor Cyan
$appdir = Join-Path $dist "app"
New-Item -ItemType Directory -Force -Path $appdir | Out-Null
Copy-Item (Join-Path $root "webui")             (Join-Path $appdir "webui")             -Recurse
Copy-Item (Join-Path $root "oracle_embeddings") (Join-Path $appdir "oracle_embeddings") -Recurse
Copy-Item (Join-Path $root "main.py")           $appdir
Copy-Item (Join-Path $root "requirements.txt")  $appdir
foreach ($f in @("config.yaml", ".env.example")) {
  $p = Join-Path $root $f
  if (Test-Path $p) { Copy-Item $p $appdir }
}
# __pycache__ 제거 (용량/혼선 방지)
Get-ChildItem $appdir -Recurse -Directory -Filter "__pycache__" |
  Remove-Item -Recurse -Force -ErrorAction SilentlyContinue

# 6) 런처 / 사용법 ---------------------------------------------------------
Copy-Item (Join-Path $root "packaging\run.bat")   $dist
Copy-Item (Join-Path $root "packaging\사용법.txt") $dist

# 7) 압축 -----------------------------------------------------------------
$zip = Join-Path $root "dist\convert-tool.zip"
Write-Host "==> 압축: $zip" -ForegroundColor Cyan
if (Test-Path $zip) { Remove-Item $zip -Force }
Compress-Archive -Path $dist -DestinationPath $zip -Force

Write-Host ""
Write-Host "완료!  →  $zip" -ForegroundColor Green
Write-Host "이 zip 을 전달하면, 받는 사람은 압축 풀고 run.bat 더블클릭만 하면 됩니다." -ForegroundColor Green
