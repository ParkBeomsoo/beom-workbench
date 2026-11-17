#!/usr/bin/env sh
# beom-workbench 설치 (macOS · Linux) — 앱과 필수 엔진까지 설치하고, Linux 는 앱 메뉴 바로가기를 만들지 묻는다.
#   curl -fsSL https://raw.githubusercontent.com/ParkBeomsoo/beom-workbench/main/install.sh | sh
# 묻지 않고 정하려면 BEOM_SHORTCUT=start,desktop (또는 start, none) 를 두고 실행한다.
# 다른 브랜치·로컬 폴더: BEOM_SOURCE=git+https://github.com/ParkBeomsoo/beom-workbench.git@<브랜치> (또는 폴더 경로)
# 안내 문구는 운영체제 언어(한국어·영어·일본어·중국어)로, 그 밖의 언어면 한국어로 보인다 — 앱과 같은 규칙.
set -e
SOURCE="${BEOM_SOURCE:-git+https://github.com/ParkBeomsoo/beom-workbench.git}"
if [ -d "$SOURCE" ]; then SPEC="$(cd "$SOURCE" && pwd)[app]"; else SPEC="beom-workbench[app] @ $SOURCE"; fi

LOC="${LC_ALL:-${LC_MESSAGES:-${LANG:-}}}"
case "$LOC" in en*) L=en ;; ja*) L=ja ;; zh*) L=zh ;; *) L=ko ;; esac
if [ -z "$LOC" ] && [ "$(uname -s)" = "Darwin" ]; then
  case "$(defaults read -g AppleLanguages 2>/dev/null | sed -n '2p')" in *en*) L=en ;; *ja*) L=ja ;; *zh*) L=zh ;; esac
fi
t() {  # 안내 문구 하나 — 키 하나에 네 언어
  case "$1.$L" in
    pipx.ko) echo "pipx 를 설치합니다…" ;; pipx.en) echo "Installing pipx…" ;; pipx.ja) echo "pipx をインストールします…" ;; pipx.zh) echo "正在安装 pipx…" ;;
    pipxFail.ko) echo "pipx 설치 실패 — 배포판 패키지(예: apt install pipx)로 설치한 뒤 다시 실행해 주세요" ;;
    pipxFail.en) echo "Could not install pipx — install it with your distribution's package (e.g. apt install pipx) and run this again" ;;
    pipxFail.ja) echo "pipx のインストールに失敗しました — ディストリビューションのパッケージ (例: apt install pipx) でインストールしてから、もう一度実行してください" ;;
    pipxFail.zh) echo "pipx 安装失败 — 请用发行版的软件包 (例如 apt install pipx) 安装后重新运行" ;;
    needPy.ko) echo "Python 3.11 이상이 필요합니다." ;; needPy.en) echo "Python 3.11 or later is required." ;;
    needPy.ja) echo "Python 3.11 以上が必要です。" ;; needPy.zh) echo "需要 Python 3.11 或更高版本。" ;;
    install.ko) echo "beom-workbench 를 설치합니다 (앱 포함)…" ;; install.en) echo "Installing beom-workbench (with the app)…" ;;
    install.ja) echo "beom-workbench をインストールします (アプリを含む)…" ;; install.zh) echo "正在安装 beom-workbench (含应用)…" ;;
    engines.ko) echo "필수 엔진(구조 지도·진입점 규칙)을 설치합니다 — 내려받은 파일은 체크섬으로 확인합니다…" ;;
    engines.en) echo "Installing the required engines (structure map, entry-point rules) — downloads are checked against checksums…" ;;
    engines.ja) echo "必須エンジン (構造マップ・エントリポイントルール) をインストールします — ダウンロードしたファイルはチェックサムで確認します…" ;;
    engines.zh) echo "正在安装必需引擎 (结构图、入口规则) — 下载的文件会用校验和核对…" ;;
    engFail.ko) echo "엔진 확인에 실패했습니다 — 'beom-workbench doctor' 로 무엇이 빠졌는지 보세요." ;;
    engFail.en) echo "Engine check failed — run 'beom-workbench doctor' to see what is missing." ;;
    engFail.ja) echo "エンジンの確認に失敗しました — 'beom-workbench doctor' で不足しているものを確認してください。" ;;
    engFail.zh) echo "引擎检查失败 — 运行 'beom-workbench doctor' 查看缺少什么。" ;;
    ask.ko) echo "앱 메뉴와 바탕화면에 바로가기를 만들까요? [Y: 둘 다 / s: 메뉴만 / n: 안 함] " ;;
    ask.en) echo "Create shortcuts in the app menu and on the desktop? [Y: both / s: menu only / n: none] " ;;
    ask.ja) echo "アプリメニューとデスクトップにショートカットを作成しますか? [Y: 両方 / s: メニューのみ / n: 作成しない] " ;;
    ask.zh) echo "要在应用菜单和桌面创建快捷方式吗? [Y: 两处都要 / s: 仅菜单 / n: 不创建] " ;;
    scOk.ko) echo "바로가기를 만들었습니다." ;; scOk.en) echo "Shortcuts created." ;; scOk.ja) echo "ショートカットを作成しました。" ;; scOk.zh) echo "已创建快捷方式。" ;;
    done.ko) echo "앱 열기: beom-workbench-app    /    상태 확인: beom-workbench doctor   (새 터미널에서 PATH 가 반영됩니다)" ;;
    done.en) echo "Open the app: beom-workbench-app    /    Check status: beom-workbench doctor   (PATH applies in a new terminal)" ;;
    done.ja) echo "アプリを開く: beom-workbench-app    /    状態確認: beom-workbench doctor   (PATH は新しいターミナルで反映されます)" ;;
    done.zh) echo "打开应用: beom-workbench-app    /    检查状态: beom-workbench doctor   (PATH 在新终端中生效)" ;;
  esac
}

if command -v pipx >/dev/null 2>&1; then PIPX="pipx"
elif command -v python3 >/dev/null 2>&1; then
  if ! python3 -m pipx --version >/dev/null 2>&1; then
    t pipx
    python3 -m pip install --user --quiet pipx || { t pipxFail; exit 1; }
    python3 -m pipx ensurepath >/dev/null 2>&1 || true
  fi
  PIPX="python3 -m pipx"
else
  t needPy; exit 1
fi

t install
$PIPX install --force "$SPEC"
BIN="$($PIPX environment --value PIPX_BIN_DIR)"
WB="$BIN/beom-workbench"

t engines
"$WB" setup --apply codebase-memory-mcp ast-grep >/dev/null || true
"$WB" doctor >/dev/null || t engFail

if [ "$(uname -s)" = "Linux" ]; then
  CHOICE="${BEOM_SHORTCUT:-}"
  if [ -z "$CHOICE" ] && [ -t 0 ]; then
    printf "%s " "$(t ask)"
    read -r ANS
    case "$ANS" in s*|S*) CHOICE=start ;; n*|N*) CHOICE=none ;; *) CHOICE=start,desktop ;; esac
  fi
  if [ -n "$CHOICE" ] && [ "$CHOICE" != "none" ]; then
    "$WB" app --shortcut "$CHOICE" >/dev/null && t scOk
  fi
fi
t done
