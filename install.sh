#!/usr/bin/env sh
# beom-workbench 설치 (macOS · Linux) — 앱과 필수 엔진까지 설치하고, Linux 는 앱 메뉴 바로가기를 만들지 묻는다.
#   curl -fsSL https://raw.githubusercontent.com/ParkBeomsoo/beom-workbench/main/install.sh | sh
# 묻지 않고 정하려면 BEOM_SHORTCUT=start,desktop (또는 start, none) 를 두고 실행한다.
# 다른 브랜치·로컬 폴더: BEOM_SOURCE=git+https://github.com/ParkBeomsoo/beom-workbench.git@<브랜치> (또는 폴더 경로)
set -e
SOURCE="${BEOM_SOURCE:-git+https://github.com/ParkBeomsoo/beom-workbench.git}"
if [ -d "$SOURCE" ]; then SPEC="$(cd "$SOURCE" && pwd)[app]"; else SPEC="beom-workbench[app] @ $SOURCE"; fi

if command -v pipx >/dev/null 2>&1; then PIPX="pipx"
elif command -v python3 >/dev/null 2>&1; then
  if ! python3 -m pipx --version >/dev/null 2>&1; then
    echo "pipx 를 설치합니다…"
    python3 -m pip install --user --quiet pipx || { echo "pipx 설치 실패 — 배포판 패키지(예: apt install pipx)로 설치한 뒤 다시 실행해 주세요"; exit 1; }
    python3 -m pipx ensurepath >/dev/null 2>&1 || true
  fi
  PIPX="python3 -m pipx"
else
  echo "Python 3.11 이상이 필요합니다."; exit 1
fi

echo "beom-workbench 를 설치합니다 (앱 포함)…"
$PIPX install --force "$SPEC"
BIN="$($PIPX environment --value PIPX_BIN_DIR)"
OSS="$BIN/beom-workbench"

echo "필수 엔진(구조 지도·진입점 규칙)을 설치합니다 — 내려받은 파일은 체크섬으로 확인합니다…"
"$OSS" setup --apply codebase-memory-mcp ast-grep >/dev/null || true
"$OSS" doctor >/dev/null || echo "엔진 확인에 실패했습니다 — 'beom-workbench doctor' 로 무엇이 빠졌는지 보세요."

if [ "$(uname -s)" = "Linux" ]; then
  CHOICE="${BEOM_SHORTCUT:-}"
  if [ -z "$CHOICE" ] && [ -t 0 ]; then
    printf "앱 메뉴와 바탕화면에 바로가기를 만들까요? [Y: 둘 다 / s: 메뉴만 / n: 안 함] "
    read -r ANS
    case "$ANS" in s*|S*) CHOICE=start ;; n*|N*) CHOICE=none ;; *) CHOICE=start,desktop ;; esac
  fi
  if [ -n "$CHOICE" ] && [ "$CHOICE" != "none" ]; then
    "$OSS" app --shortcut "$CHOICE" >/dev/null && echo "바로가기를 만들었습니다."
  fi
fi
echo "앱 열기: beom-workbench    /    상태 확인: beom-workbench doctor   (새 터미널에서 PATH 가 반영됩니다)"
