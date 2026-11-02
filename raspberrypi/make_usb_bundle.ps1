# 라즈베리파이로 옮길 파일만 골라 USB에 담는다 (make_usb_bundle.bat 을 더블클릭하면 실행된다)
#  - 꽂혀 있는 USB를 찾아 그 안에 TheEclipse_RaspberryPi 폴더를 만든다 (USB가 없으면 dist 폴더에 만든다)
#  - 실행에 필요한 파일만 담는다 (저장소의 InsightFace 원본 소스·.git 등은 빼서 약 6MB)
#  - 출결 기록(faces.db)·출퇴근 사진도 같이 담아서 라즈베리파이에서 그대로 이어 쓸 수 있게 한다
#  - .sh 파일은 줄바꿈을 LF로 바꿔 담는다 (CRLF면 라즈베리파이에서 실행되지 않는다)
#  - 구글 시트 서비스 계정 키·설정(settings.json)도 담아서 라즈베리파이에서 구글폼 명단을 바로 읽게 한다
#  -Zip 을 주면 USB 대신 dist 폴더에 만들고 TheEclipse_RaspberryPi.zip 으로 압축한다 (make_zip_bundle.bat)
param([switch]$Zip)

$ErrorActionPreference = "Stop"
$AppDir = Split-Path -Parent $PSScriptRoot
$BundleName = "TheEclipse_RaspberryPi"

function Step($text) { Write-Host ""; Write-Host "▶ $text" -ForegroundColor Cyan }
function Warn($text) { Write-Host "⚠ $text" -ForegroundColor Yellow }

Write-Host "=== 라즈베리파이 설치용 USB 만들기 ===" -ForegroundColor Green

# ---------- 1. 담을 위치 ----------
Step "USB 찾기"
$usbDrives = @(Get-CimInstance Win32_LogicalDisk -Filter "DriveType=2")
if ($Zip) {
    $usbDrives = @()
    $root = Join-Path $AppDir "dist"
    Write-Host "압축 파일로 만듭니다: $root"
} elseif ($usbDrives.Count -eq 1) {
    $root = $usbDrives[0].DeviceID + "\"
    Write-Host "USB 드라이브: $root ($($usbDrives[0].VolumeName))"
} elseif ($usbDrives.Count -gt 1) {
    Write-Host "USB가 여러 개 꽂혀 있습니다:"
    $usbDrives | ForEach-Object { Write-Host "  $($_.DeviceID)  $($_.VolumeName)" }
    $letter = (Read-Host "담을 드라이브 문자를 입력하세요 (예: E)").Trim().TrimEnd(":")
    $root = "${letter}:\"
    if (-not (Test-Path $root)) { throw "드라이브 $root 를 찾을 수 없습니다." }
} else {
    $root = Join-Path $AppDir "dist"
    Warn "꽂혀 있는 USB를 찾지 못해 $root 에 만듭니다. 만든 폴더를 USB로 직접 복사하세요."
}
New-Item -ItemType Directory -Path $root -Force | Out-Null
$Dest = Join-Path $root $BundleName

# ---------- 2. 실행 중인지 확인 ----------
# 출퇴근을 기록하는 도중에 faces.db를 복사하면 깨진 사본이 될 수 있다
$running = Get-CimInstance Win32_Process -Filter "Name LIKE 'python%' OR Name LIKE 'TheEclipse%'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -like "*attendance_app*" -or $_.Name -like "TheEclipse*" }
$python = Get-Command python -ErrorAction SilentlyContinue
if ($running -and -not $python) {
    Warn "출석체크 프로그램이 실행 중입니다. 출결 기록이 안전하게 복사되도록 프로그램을 먼저 닫아 주세요."
    Read-Host "닫은 뒤 Enter를 누르세요" | Out-Null
}

# ---------- 3. 파일 담기 ----------
Step "파일 담기 → $Dest"
if (Test-Path $Dest) { Remove-Item $Dest -Recurse -Force }
New-Item -ItemType Directory -Path (Join-Path $Dest "raspberrypi") -Force | Out-Null

# 프로그램 실행에 필요한 것 (없는 항목은 건너뛴다 — face_landmarker.task는 없으면 라즈베리파이에서 받는다)
foreach ($item in @("attendance_app.py", "core.py", "requirements.txt", "README.md", "assets", "face_landmarker.task")) {
    $src = Join-Path $AppDir $item
    if (Test-Path $src) { Copy-Item $src -Destination $Dest -Recurse -Force }
}

# 설치 스크립트는 줄바꿈을 LF로 맞춰서 담는다 (BOM 없는 UTF-8)
$utf8NoBom = New-Object System.Text.UTF8Encoding $false
Get-ChildItem (Join-Path $PSScriptRoot "*.sh") | ForEach-Object {
    $text = [IO.File]::ReadAllText($_.FullName) -replace "`r`n", "`n"
    [IO.File]::WriteAllText((Join-Path $Dest "raspberrypi\$($_.Name)"), $text, $utf8NoBom)
}

# 출결 데이터 — 라즈베리파이에서 학생을 다시 등록하지 않아도 되게 같이 담는다
$dataItems = @()
foreach ($item in @("faces.db", "attendance_photos", "settings.json")) {
    $src = Join-Path $AppDir $item
    if (-not (Test-Path $src)) { continue }
    if ($item -eq "faces.db" -and $python) {
        # 프로그램이 켜져 있어도 기록 중간 상태가 섞이지 않게 SQLite 백업 기능으로 복사한다
        & $python.Source -c "import sqlite3,sys; s=sqlite3.connect(sys.argv[1]); d=sqlite3.connect(sys.argv[2]); s.backup(d); d.close(); s.close()" $src (Join-Path $Dest $item)
        if ($LASTEXITCODE -ne 0) { throw "faces.db 복사에 실패했습니다." }
    } else {
        Copy-Item $src -Destination $Dest -Recurse -Force
    }
    $dataItems += $item
}

# 구글 시트 서비스 계정 키 — 비밀 키라서 git에는 안 올리지만, 라즈베리파이에서 구글폼 명단을 읽으려면 필요하다
$keySrc = Join-Path $AppDir "google_service_account.json"
$hasKey = Test-Path $keySrc
if ($hasKey) {
    Copy-Item $keySrc -Destination $Dest -Force
    Write-Host "구글 시트 서비스 계정 키를 담았습니다."
} else {
    Warn "구글 시트 서비스 계정 키가 없어 담지 않았습니다 (관리자 [설정]에서 키를 등록하면 다음부터 같이 담깁니다)."
}

# 얼굴 인식 모델(buffalo_l, 약 330MB) — 이 PC에 받아 둔 것을 같이 담아서 라즈베리파이는 인터넷 없이 설치한다
# (학교·회사 와이파이는 모델이 있는 GitHub 다운로드 서버를 막는 경우가 있다)
Step "얼굴 인식 모델 담기"
$modelSrc = Join-Path $env:USERPROFILE ".insightface\models\buffalo_l"
$modelSizes = @{ "1k3d68.onnx" = 143607619; "2d106det.onnx" = 5030888; "det_10g.onnx" = 16923827;
                 "genderage.onnx" = 1322532; "w600k_r50.onnx" = 174383860 }
$modelOk = (Test-Path $modelSrc)
foreach ($name in $modelSizes.Keys) {
    $f = Join-Path $modelSrc $name
    if (-not (Test-Path $f) -or (Get-Item $f).Length -lt $modelSizes[$name]) { $modelOk = $false }
}
if ($modelOk) {
    $modelDest = Join-Path $Dest "models\buffalo_l"
    New-Item -ItemType Directory -Path $modelDest -Force | Out-Null
    Copy-Item (Join-Path $modelSrc "*.onnx") -Destination $modelDest -Force
    Write-Host "얼굴 인식 모델을 담았습니다 — 라즈베리파이에서 모델을 다운로드하지 않습니다."
} else {
    Warn "이 PC에 온전한 얼굴 인식 모델이 없어 담지 못했습니다. 라즈베리파이에서 인터넷으로 받습니다."
    Warn "(이 PC에서 출석체크 프로그램을 한 번 실행하면 모델이 받아집니다)"
}

# ---------- 4. 압축 ----------
$zipPath = $null
if ($Zip) {
    Step "압축하기"
    $zipPath = Join-Path $root "$BundleName.zip"
    if (Test-Path $zipPath) { Remove-Item $zipPath -Force }
    Compress-Archive -Path $Dest -DestinationPath $zipPath -CompressionLevel Optimal
    Write-Host "압축 파일: $zipPath ($([math]::Round((Get-Item $zipPath).Length / 1MB, 1)) MB)"
}

# ---------- 5. 결과 ----------
$sizeMB = [math]::Round(((Get-ChildItem $Dest -Recurse -File | Measure-Object Length -Sum).Sum / 1MB), 1)
Write-Host ""
Write-Host "=== 완료 ($sizeMB MB) ===" -ForegroundColor Green
Write-Host "담은 위치: $Dest"
if ($dataItems.Count -gt 0) {
    Write-Host "출결 데이터도 담았습니다: $($dataItems -join ', ')"
} else {
    Write-Host "출결 데이터(faces.db)는 없어서 담지 않았습니다. 라즈베리파이에서 새로 시작합니다."
}
if ($modelOk) { Write-Host "얼굴 인식 모델도 담았습니다 (인터넷 없이 설치 가능)." }
if ($hasKey) { Warn "구글 비밀 키와 학생 출결 데이터가 들어 있습니다. 라즈베리파이로 옮긴 뒤에는 다른 사람과 공유하지 마세요." }
if ($zipPath) {
    Write-Host ""
    Write-Host "압축 파일로 옮길 때:" -ForegroundColor Cyan
    Write-Host "  1) $BundleName.zip 을 USB나 메일 등으로 라즈베리파이에 옮깁니다"
    Write-Host "  2) 라즈베리파이 홈 폴더에서 압축을 풉니다:  unzip $BundleName.zip" -ForegroundColor White
    Write-Host "  3) cd $BundleName && bash raspberrypi/install.sh" -ForegroundColor White
}
Write-Host ""
Write-Host "라즈베리파이에서 할 일:" -ForegroundColor Cyan
Write-Host "  1) USB를 꽂고 파일 관리자에서 $BundleName 폴더를 엽니다"
Write-Host "  2) 폴더 안 빈 곳에서 [F4] 또는 [도구 → 현재 폴더에서 터미널 열기]"
Write-Host "  3) 아래 명령을 입력합니다 — 홈 폴더로 복사한 뒤 설치가 진행되고, 끝나면 USB를 뽑아도 됩니다"
Write-Host "       bash raspberrypi/install.sh" -ForegroundColor White
Write-Host ""
if ($usbDrives.Count -ge 1) { Write-Host "USB를 뺄 때는 작업 표시줄의 [하드웨어 안전하게 제거]를 먼저 눌러 주세요." }
