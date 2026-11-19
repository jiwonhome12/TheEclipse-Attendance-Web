# 창의공간 출석체크 — 웹 서버 실행 (프로젝트 폴더의 "웹서버_실행.bat"을 더블클릭하면 실행된다)
#  · 이 파일이 있는 폴더를 기준으로 찾아 들어가므로, 폴더를 다른 컴퓨터·다른 위치로 옮겨도 그대로 된다
#  · 파이썬과 필요한 패키지가 없으면 알려 주고, 패키지는 설치할지 물어본다
#  · 서버가 켜지면 브라우저로 출석 화면을 연다. 이 창을 닫으면 서버도 꺼진다

$AppDir = Split-Path -Parent $PSScriptRoot
$PythonPathFile = Join-Path $PSScriptRoot "python_path.txt"
$Url = "http://127.0.0.1:8000"
$Host.UI.RawUI.WindowTitle = "창의공간 출석체크 웹 서버"

function Get-PythonExe {
    # install.bat 으로 설치했으면 그때 찾은 파이썬, 아니면 py 런처 → PATH의 python → 기본 설치 위치 순서로 찾는다
    if (Test-Path $PythonPathFile) {
        $saved = (Get-Content $PythonPathFile -Raw).Trim()
        if ($saved -and (Test-Path $saved)) { return $saved }
    }
    $tries = @(
        { py -3 -c "import sys; print(sys.executable)" },
        { python -c "import sys; print(sys.executable)" }
    )
    foreach ($try in $tries) {
        try {
            $out = & $try 2>$null
            if ($LASTEXITCODE -eq 0 -and $out -and (Test-Path $out.Trim())) { return $out.Trim() }
        } catch {}
    }
    $candidates = Get-ChildItem "$env:LOCALAPPDATA\Programs\Python\Python3*\python.exe" -ErrorAction SilentlyContinue |
        Sort-Object FullName -Descending
    if ($candidates) { return $candidates[0].FullName }
    return $null
}

Write-Host "=== 창의공간 출석체크 웹 서버 ===" -ForegroundColor Green
Write-Host "프로그램 폴더: $AppDir"

# ---------- GitHub에서 최신 코드 받기 ----------
# 다른 PC에서 고쳐 올린 것도 실행할 때마다 자동으로 반영된다 (학생 DB·사진·설정은 git에 없어서 그대로 남는다).
# 인터넷이 없거나 이 PC에서 고친 파일이 있어서 못 받으면 지금 있는 코드로 그냥 켠다.
$Updated = $false
$Git = Get-Command git -ErrorAction SilentlyContinue
if ($Git -and (Test-Path (Join-Path $AppDir ".git"))) {
    Write-Host "GitHub에서 최신 코드를 확인하는 중..."
    $before = (git -C $AppDir rev-parse HEAD 2>$null)
    $out = git -C $AppDir pull --ff-only 2>&1
    if ($LASTEXITCODE -eq 0) {
        $after = (git -C $AppDir rev-parse HEAD 2>$null)
        if ($before -ne $after) {
            $Updated = $true
            Write-Host "최신 코드를 받았습니다:" -ForegroundColor Green
            git -C $AppDir log --oneline "$before..$after"
        } else {
            Write-Host "이미 최신 코드입니다."
        }
    } else {
        Write-Host "최신 코드를 받지 못해 지금 코드로 실행합니다. (인터넷 연결 또는 이 PC에서 고친 파일 확인)" -ForegroundColor Yellow
        $out | ForEach-Object { Write-Host "  $_" }
    }
} else {
    Write-Host "git이 없거나 ZIP으로 받은 폴더라 최신 코드 확인은 건너뜁니다."
}

# ---------- 이미 켜져 있으면 ----------
$running = Get-CimInstance Win32_Process -Filter "Name LIKE 'python%'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -match 'server\.py' -or $_.CommandLine -match 'attendance_app\.py' }
if ($running) {
    if ($running | Where-Object { $_.CommandLine -match 'attendance_app\.py' }) {
        Write-Host "앱 버전(attendance_app.py)이 켜져 있어서 카메라를 같이 쓸 수 없습니다." -ForegroundColor Yellow
        Write-Host "앱 버전을 먼저 끈 뒤 다시 실행하세요."
        exit 1
    }
    if (-not $Updated) {
        # 코드가 그대로면 켜진 서버를 그대로 쓴다
        Write-Host "웹 서버가 이미 켜져 있습니다. 브라우저로 출석 화면을 엽니다."
        Start-Process $Url
        Start-Sleep -Seconds 2
        exit 0
    }
    # 새 코드를 받았으면 예전 코드로 돌고 있는 서버를 끄고 새로 켠다 (안 그러면 고친 내용이 안 보인다)
    Write-Host "예전 코드로 켜진 웹 서버를 끄고 새 코드로 다시 켭니다." -ForegroundColor Cyan
    $running | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    Start-Sleep -Seconds 2
}

# ---------- 파이썬 ----------
$Python = Get-PythonExe
if (-not $Python) {
    Write-Host ""
    Write-Host "파이썬이 설치되어 있지 않습니다." -ForegroundColor Red
    Write-Host "windows 폴더의 install.bat 을 먼저 실행하거나,"
    Write-Host "https://www.python.org/downloads/ 에서 파이썬을 설치하세요. (설치 화면에서 'Add python.exe to PATH' 체크)"
    exit 1
}
Write-Host "사용할 파이썬: $Python"

# ---------- Visual C++ 런타임 ----------
# onnxruntime(얼굴 인식)은 최신 Visual C++ 런타임이 필요하다. 오래된 msvcp140.dll(14.40 미만)이면
# 패키지는 설치돼 있어도 불러오는 순간 프로그램이 죽어서 얼굴 인식이 안 된다.
$Msvcp = Get-Item "$env:SystemRoot\System32\msvcp140.dll" -ErrorAction SilentlyContinue
if (-not $Msvcp -or $Msvcp.VersionInfo.FileMajorPart -lt 14 -or
    ($Msvcp.VersionInfo.FileMajorPart -eq 14 -and $Msvcp.VersionInfo.FileMinorPart -lt 40)) {
    $ver = if ($Msvcp) { $Msvcp.VersionInfo.FileVersion } else { "없음" }
    Write-Host ""
    Write-Host "Visual C++ 런타임이 오래되어 얼굴 인식이 동작하지 않습니다. (지금 버전: $ver)" -ForegroundColor Red
    Write-Host "아래 주소의 설치 파일을 받아 설치한 뒤 다시 실행하세요:"
    Write-Host "  https://aka.ms/vs/17/release/vc_redist.x64.exe" -ForegroundColor Cyan
    $answer = Read-Host "지금 다운로드 페이지를 열까요? (Y/N)"
    if ($answer -match '^[Yy]') { Start-Process "https://aka.ms/vs/17/release/vc_redist.x64.exe" }
    exit 1
}

# ---------- 패키지 ----------
& $Python -c "import fastapi, uvicorn, websockets, cv2, numpy, PIL, insightface, onnxruntime" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host ""
    Write-Host "웹 서버에 필요한 패키지가 설치되어 있지 않습니다." -ForegroundColor Yellow
    $answer = Read-Host "지금 설치할까요? 처음이면 몇 분 걸립니다 (Y/N)"
    if ($answer -notmatch '^[Yy]') { exit 1 }
    & $Python -m pip install -r (Join-Path $AppDir "requirements.txt")
    if ($LASTEXITCODE -ne 0) { Write-Host "패키지 설치에 실패했습니다. 위 오류를 확인하세요." -ForegroundColor Red; exit 1 }
}

# ---------- 서버가 뜨면 브라우저 열기 (뒤에서 기다린다) ----------
Start-Job -ArgumentList $Url -ScriptBlock {
    param($url)
    for ($i = 0; $i -lt 120; $i++) {
        try { Invoke-WebRequest -UseBasicParsing -Uri "$url/api/state" -TimeoutSec 2 | Out-Null; Start-Process $url; return }
        catch { Start-Sleep -Seconds 1 }
    }
} | Out-Null

# ---------- 서버 실행 (이 창에서 계속 돈다) ----------
Write-Host ""
Write-Host "서버를 켭니다. 처음에는 얼굴 인식 모델을 불러오느라 10초 정도 걸립니다." -ForegroundColor Cyan
Write-Host "출석 화면: $Url    관리자 화면: $Url/admin"
Write-Host "끄려면 이 창을 닫거나 Ctrl+C 를 누르세요." -ForegroundColor Cyan
Write-Host ""
Set-Location $AppDir
& $Python (Join-Path $AppDir "web\server.py") @args
exit $LASTEXITCODE
