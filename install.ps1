# beom-workbench 설치 (Windows) — 앱과 필수 엔진까지 설치하고, 시작 메뉴·바탕화면 바로가기를 만들지 묻는다.
#   irm https://raw.githubusercontent.com/ParkBeomsoo/beom-workbench/main/install.ps1 | iex
# 묻지 않고 정하려면 먼저 $env:BEOM_SHORTCUT = "start,desktop" (또는 "start", "none") 를 두고 실행한다.
# 다른 브랜치·로컬 폴더에서 설치하려면 $env:BEOM_SOURCE = "git+https://github.com/ParkBeomsoo/beom-workbench.git@<브랜치>" (또는 폴더 경로)
$ErrorActionPreference = "Stop"
$source = if ($env:BEOM_SOURCE) { $env:BEOM_SOURCE } else { "git+https://github.com/ParkBeomsoo/beom-workbench.git" }
$spec = if (Test-Path $source) { "$((Resolve-Path $source).Path)[app]" } else { "beom-workbench[app] @ $source" }

# pipx — 없으면 Python 으로 설치한다 (PATH 반영 전이라도 python -m pipx 로 바로 쓴다)
$py = Get-Command python -ErrorAction SilentlyContinue
if (-not $py) { $py = Get-Command py -ErrorAction SilentlyContinue }
function Invoke-Pipx { if (Get-Command pipx -ErrorAction SilentlyContinue) { pipx @args } else { & $py.Source -m pipx @args } }
if (-not (Get-Command pipx -ErrorAction SilentlyContinue)) {
    if (-not $py) { Write-Host "Python 3.11 이상이 필요합니다 — https://www.python.org/downloads/ 에서 설치한 뒤 다시 실행해 주세요." -ForegroundColor Yellow; exit 1 }
    Write-Host "pipx 를 설치합니다…"
    & $py.Source -m pip install --user --quiet pipx
    if ($LASTEXITCODE -ne 0) { Write-Host "pipx 설치에 실패했습니다: python -m pip install --user pipx" -ForegroundColor Red; exit 1 }
    & $py.Source -m pipx ensurepath | Out-Null
}

Write-Host "beom-workbench 를 설치합니다 (앱 포함)…"
Invoke-Pipx install --force $spec
if ($LASTEXITCODE -ne 0) { Write-Host "설치에 실패했습니다." -ForegroundColor Red; exit 1 }
$bin = (Invoke-Pipx environment --value PIPX_BIN_DIR).Trim()
$wb = Join-Path $bin "beom-workbench.exe"

Write-Host "필수 엔진(구조 지도·진입점 규칙)을 설치합니다 — 내려받은 파일은 체크섬으로 확인합니다…"
& $wb setup --apply codebase-memory-mcp ast-grep | Out-Null
& $wb doctor | Out-Null
if ($LASTEXITCODE -ne 0) { Write-Host "엔진 확인에 실패했습니다 — 'beom-workbench doctor' 로 무엇이 빠졌는지 보세요." -ForegroundColor Yellow }

$choice = $env:BEOM_SHORTCUT
if (-not $choice) {
    $ans = Read-Host "시작 메뉴와 바탕화면에 바로가기를 만들까요? [Y: 둘 다 / s: 시작 메뉴만 / n: 안 함]"
    $choice = switch -Regex ($ans) { "^[sS]" { "start" } "^[nN]" { "none" } default { "start,desktop" } }
}
if ($choice -ne "none") {
    & $wb app --shortcut $choice | Out-Null
    if ($LASTEXITCODE -eq 0) { Write-Host "바로가기를 만들었습니다 — 시작 메뉴에서 'beom' 으로 검색하세요." -ForegroundColor Green }
    else { Write-Host "바로가기를 만들지 못했습니다. 나중에 'beom-workbench app --shortcut' 으로 만들 수 있습니다." -ForegroundColor Yellow }
}
Write-Host "앱 열기: beom-workbench    /    상태 확인: beom-workbench doctor   (새 터미널에서 PATH 가 반영됩니다)"
