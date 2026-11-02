# 윈도우 배포판 zip 만들기 — exe 빌드 후 설치 파일을 넣어 압축한다.
#   powershell -ExecutionPolicy Bypass -File packaging\build_release.ps1
# 결과: dist\TheEclipseAttendance-windows.zip
#   -WithData : 이 PC의 구글 시트 키·설정·학생 DB·출퇴근 사진·얼굴 인식 모델까지 담는다
#               → dist\TheEclipseAttendance-windows-with-data.zip (압축 풀고 install.bat 만 누르면 바로 이어서 쓴다)
#               비밀 키와 학생 정보가 들어 있으니 GitHub 릴리스에는 올리지 않는다
# (GitHub에 v* 태그를 올리면 .github/workflows/release-windows.yml 이 이 스크립트로 자동 빌드한다)
param([string]$OutDir = "", [switch]$WithData)
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
# 경로가 길면(260자 제한) 빌드가 실패해서 작업 폴더는 짧은 임시 경로를 쓴다
$Work = Join-Path $env:TEMP "teb-work"
$Dist = if ($OutDir) { $OutDir } else { Join-Path $Root "dist" }
$AppOut = Join-Path $Dist "TheEclipseAttendance"
$Zip = Join-Path $Dist ($(if ($WithData) { "TheEclipseAttendance-windows-with-data.zip" } else { "TheEclipseAttendance-windows.zip" }))

python -m PyInstaller (Join-Path $PSScriptRoot "TheEclipseAttendance.spec") --noconfirm --distpath $Dist --workpath $Work
if ($LASTEXITCODE -ne 0) { throw "PyInstaller 빌드 실패" }

# 설치 파일 복사 (.ps1 은 한글이 깨지지 않도록 UTF-8 BOM 그대로, .bat 은 ASCII)
Copy-Item (Join-Path $PSScriptRoot "release\*") $AppOut -Force
foreach ($name in @("install", "uninstall")) {
    $bat = "@echo off`r`npowershell -NoProfile -ExecutionPolicy Bypass -File `"%~dp0$name.ps1`"`r`npause`r`n"
    [IO.File]::WriteAllText((Join-Path $AppOut "$name.bat"), $bat, [Text.Encoding]::ASCII)
}

if ($WithData) {
    # 학생 DB — 프로그램이 켜져 있어도 기록 중간 상태가 섞이지 않게 SQLite 백업 기능으로 복사한다
    $db = Join-Path $Root "faces.db"
    if (Test-Path $db) {
        python -c "import sqlite3,sys; s=sqlite3.connect(sys.argv[1]); d=sqlite3.connect(sys.argv[2]); s.backup(d); d.close(); s.close()" $db (Join-Path $AppOut "faces.db")
        if ($LASTEXITCODE -ne 0) { throw "faces.db 복사 실패" }
    }
    foreach ($item in @("attendance_photos", "settings.json", "google_service_account.json", "camera.txt")) {
        $src = Join-Path $Root $item
        if (Test-Path $src) { Copy-Item $src -Destination $AppOut -Recurse -Force }
    }
    # 얼굴 인식 모델(buffalo_l) — 설치할 때 사용자 폴더로 옮겨서 인터넷 없이 바로 쓴다 (install.ps1)
    $modelSrc = Join-Path $env:USERPROFILE ".insightface\models\buffalo_l"
    if (Test-Path (Join-Path $modelSrc "w600k_r50.onnx")) {
        $modelDest = Join-Path $AppOut "models\buffalo_l"
        New-Item -ItemType Directory -Path $modelDest -Force | Out-Null
        Copy-Item (Join-Path $modelSrc "*.onnx") -Destination $modelDest -Force
    }
    Write-Host "키·설정·학생 DB·얼굴 모델을 담았습니다 — 다른 사람과 공유하지 마세요." -ForegroundColor Yellow
}

if (Test-Path $Zip) { Remove-Item $Zip }
# 압축 풀면 TheEclipseAttendance 폴더 하나가 나오도록
Compress-Archive -Path $AppOut -DestinationPath $Zip -CompressionLevel Optimal
Write-Host ("완료: {0} ({1:N0} MB)" -f $Zip, ((Get-Item $Zip).Length / 1MB))
