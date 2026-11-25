"""Windows 격리 실행 (내장) — 쓰기 제한 토큰으로 명령을 띄워, 허용한 폴더 밖에는 쓰지 못하게 한다. 관리자 권한·추가 설치 없음.

원리 (Windows Home 에서도 되는 방식):
- 지금 사용자 토큰에서 '쓰기 제한(WRITE_RESTRICTED)' 토큰을 만든다. 쓰기 권한은 원래 사용자 권한과 함께 제한 SID 목록
  (Everyone · 로그온 세션 · 이 툴킷의 격리 SID)으로도 허락돼야 한다 → 사용자 폴더 대부분(사용자·SYSTEM·Administrators 에게만
  쓰기를 준 곳)은 쓸 수 없다. 읽기는 그대로라 빌드·테스트 도구는 평소처럼 돈다.
- 쓸 수 있게 할 폴더(대상 폴더·임시 폴더·도구 캐시)에는 격리 SID 의 수정 권한을 한 번 더해 둔다 (icacls, 상속).
- 특권은 모두 뺀다 (DISABLE_MAX_PRIVILEGE). 자기 토큰을 줄인 것이라 CreateProcessAsUser 에 특권이 필요 없다.
- 네트워크는 막지 못한다 (Windows 방화벽은 제한 토큰을 구분하지 못함) — 웹 허용 여부는 권한 규칙이 맡는다.

명령줄: python -m beom_workbench.core.winsandbox --root DIR [--root DIR …] -- 프로그램 인자…
표준 입출력은 그대로 이어지고, 끝 코드도 그대로 돌려준다.
"""
from __future__ import annotations

import ctypes
import hashlib
import os
import subprocess
import sys
from ctypes import wintypes as wt
from pathlib import Path

SID_TEXT = "S-1-5-21-1141592653-589793238-462643383-4242"  # 이 툴킷의 격리 SID (계정은 없다 — ACL·제한 목록에만 쓰는 고정 값)
# 권한을 준 폴더 기록 — BEOM_WORKBENCH_SANDBOX_DIR 로 옮길 수 있다 (시험이 사용자 기록에 남지 않게)
_GRANTED = Path(os.environ.get("BEOM_WORKBENCH_SANDBOX_DIR")
                or Path(os.environ.get("LOCALAPPDATA", Path.home())) / "beom-workbench-sandbox") / "granted.txt"

if sys.platform == "win32":
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    adv = ctypes.WinDLL("advapi32", use_last_error=True)

    class SID_AND_ATTRIBUTES(ctypes.Structure):
        _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", wt.DWORD)]

    class STARTUPINFOW(ctypes.Structure):
        _fields_ = [("cb", wt.DWORD), ("lpReserved", wt.LPWSTR), ("lpDesktop", wt.LPWSTR), ("lpTitle", wt.LPWSTR),
                    ("dwX", wt.DWORD), ("dwY", wt.DWORD), ("dwXSize", wt.DWORD), ("dwYSize", wt.DWORD),
                    ("dwXCountChars", wt.DWORD), ("dwYCountChars", wt.DWORD), ("dwFillAttribute", wt.DWORD),
                    ("dwFlags", wt.DWORD), ("wShowWindow", wt.WORD), ("cbReserved2", wt.WORD), ("lpReserved2", ctypes.c_void_p),
                    ("hStdInput", wt.HANDLE), ("hStdOutput", wt.HANDLE), ("hStdError", wt.HANDLE)]

    class PROCESS_INFORMATION(ctypes.Structure):
        _fields_ = [("hProcess", wt.HANDLE), ("hThread", wt.HANDLE), ("dwProcessId", wt.DWORD), ("dwThreadId", wt.DWORD)]

    class TOKEN_GROUPS_HDR(ctypes.Structure):
        _fields_ = [("GroupCount", wt.DWORD)]

    class TRUSTEE_W(ctypes.Structure):
        _fields_ = [("pMultipleTrustee", ctypes.c_void_p), ("MultipleTrusteeOperation", ctypes.c_int),
                    ("TrusteeForm", ctypes.c_int), ("TrusteeType", ctypes.c_int), ("ptstrName", ctypes.c_void_p)]

    class EXPLICIT_ACCESS_W(ctypes.Structure):
        _fields_ = [("grfAccessPermissions", wt.DWORD), ("grfAccessMode", ctypes.c_int), ("grfInheritance", wt.DWORD),
                    ("Trustee", TRUSTEE_W)]

    # 64비트에서 핸들·포인터가 잘리지 않게 인자 형식을 정해 둔다
    PH = ctypes.POINTER(wt.HANDLE)
    k32.GetCurrentProcess.restype = wt.HANDLE
    k32.GetStdHandle.restype = wt.HANDLE
    k32.GetStdHandle.argtypes = [wt.DWORD]
    k32.CloseHandle.argtypes = [wt.HANDLE]
    k32.WaitForSingleObject.argtypes = [wt.HANDLE, wt.DWORD]
    k32.GetExitCodeProcess.argtypes = [wt.HANDLE, ctypes.POINTER(wt.DWORD)]
    k32.GetConsoleProcessList.argtypes = [ctypes.POINTER(wt.DWORD), wt.DWORD]
    adv.OpenProcessToken.argtypes = [wt.HANDLE, wt.DWORD, PH]
    adv.ConvertStringSidToSidW.argtypes = [wt.LPCWSTR, ctypes.POINTER(ctypes.c_void_p)]
    adv.GetTokenInformation.argtypes = [wt.HANDLE, ctypes.c_int, ctypes.c_void_p, wt.DWORD, ctypes.POINTER(wt.DWORD)]
    adv.GetLengthSid.argtypes = [ctypes.c_void_p]
    adv.GetLengthSid.restype = wt.DWORD
    adv.CopySid.argtypes = [wt.DWORD, ctypes.c_void_p, ctypes.c_void_p]
    adv.CreateRestrictedToken.argtypes = [wt.HANDLE, wt.DWORD, wt.DWORD, ctypes.c_void_p, wt.DWORD, ctypes.c_void_p, wt.DWORD,
                                          ctypes.c_void_p, PH]
    adv.GetNamedSecurityInfoW.argtypes = [wt.LPCWSTR, ctypes.c_int, wt.DWORD, ctypes.c_void_p, ctypes.c_void_p,
                                          ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
    adv.GetNamedSecurityInfoW.restype = wt.DWORD
    adv.SetEntriesInAclW.argtypes = [wt.ULONG, ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
    adv.SetEntriesInAclW.restype = wt.DWORD
    adv.SetNamedSecurityInfoW.argtypes = [wt.LPWSTR, ctypes.c_int, wt.DWORD, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                                          ctypes.c_void_p]
    adv.SetNamedSecurityInfoW.restype = wt.DWORD
    k32.LocalFree.argtypes = [ctypes.c_void_p]
    adv.SetTokenInformation.argtypes = [wt.HANDLE, ctypes.c_int, ctypes.c_void_p, wt.DWORD]
    adv.CreateProcessAsUserW.argtypes = [wt.HANDLE, wt.LPCWSTR, wt.LPWSTR, ctypes.c_void_p, ctypes.c_void_p, wt.BOOL, wt.DWORD,
                                         ctypes.c_void_p, wt.LPCWSTR, ctypes.c_void_p, ctypes.c_void_p]


TOKEN_DUPLICATE, TOKEN_QUERY, TOKEN_ASSIGN_PRIMARY, TOKEN_ADJUST_DEFAULT = 0x0002, 0x0008, 0x0001, 0x0080
DISABLE_MAX_PRIVILEGE, WRITE_RESTRICTED = 0x1, 0x8
SE_GROUP_LOGON_ID = 0xC0000000
TokenGroups = 2
STARTF_USESTDHANDLES = 0x100
CREATE_UNICODE_ENVIRONMENT, CREATE_NO_WINDOW = 0x400, 0x08000000
INFINITE = 0xFFFFFFFF


def _sid(text: str):
    p = ctypes.c_void_p()
    if not adv.ConvertStringSidToSidW(text, ctypes.byref(p)):
        raise OSError(ctypes.get_last_error(), f"SID 변환 실패: {text}")
    return p


def _logon_sid(token) -> ctypes.c_void_p | None:
    need = wt.DWORD()
    adv.GetTokenInformation(token, TokenGroups, None, 0, ctypes.byref(need))
    buf = ctypes.create_string_buffer(need.value)
    if not adv.GetTokenInformation(token, TokenGroups, buf, need, ctypes.byref(need)):
        return None
    count = TOKEN_GROUPS_HDR.from_buffer(buf).GroupCount
    off = ctypes.sizeof(ctypes.c_void_p)  # GroupCount 뒤 정렬
    arr = (SID_AND_ATTRIBUTES * count).from_buffer(buf, off)
    for g in arr:
        if g.Attributes & SE_GROUP_LOGON_ID == SE_GROUP_LOGON_ID:
            n = adv.GetLengthSid(ctypes.c_void_p(g.Sid))
            copy = ctypes.create_string_buffer(n)
            adv.CopySid(n, copy, ctypes.c_void_p(g.Sid))
            _logon_sid.keep = copy  # 버퍼 수명 유지
            return ctypes.cast(copy, ctypes.c_void_p)
    return None


def restricted_token():
    k32.GetCurrentProcess.restype = wt.HANDLE
    tok = wt.HANDLE()
    if not adv.OpenProcessToken(k32.GetCurrentProcess(), TOKEN_DUPLICATE | TOKEN_QUERY | TOKEN_ASSIGN_PRIMARY | TOKEN_ADJUST_DEFAULT,
                                ctypes.byref(tok)):
        raise OSError(ctypes.get_last_error(), "OpenProcessToken")
    sids = [_sid("S-1-1-0"), _sid(SID_TEXT)]
    logon = _logon_sid(tok)
    if logon:
        sids.append(logon)
    arr = (SID_AND_ATTRIBUTES * len(sids))(*[SID_AND_ATTRIBUTES(s.value if hasattr(s, "value") else s, 0) for s in sids])
    new = wt.HANDLE()
    if not adv.CreateRestrictedToken(tok, DISABLE_MAX_PRIVILEGE | WRITE_RESTRICTED, 0, None, 0, None, len(sids), arr,
                                     ctypes.byref(new)):
        raise OSError(ctypes.get_last_error(), "CreateRestrictedToken")
    _set_default_dacl(new, tok, logon)
    k32.CloseHandle(tok)
    return new


def _user_sid(token) -> ctypes.c_void_p:
    need = wt.DWORD()
    adv.GetTokenInformation(token, 1, None, 0, ctypes.byref(need))  # TokenUser
    buf = ctypes.create_string_buffer(need.value)
    if not adv.GetTokenInformation(token, 1, buf, need, ctypes.byref(need)):
        raise OSError(ctypes.get_last_error(), "GetTokenInformation(TokenUser)")
    _user_sid.keep = buf
    return ctypes.c_void_p(SID_AND_ATTRIBUTES.from_buffer(buf).Sid)


def _set_default_dacl(new, tok, logon) -> None:
    """격리된 프로세스가 만드는 객체(파이프·이벤트 등)의 기본 권한에 격리 SID·로그온 SID 를 넣는다.
    없으면 자기가 만든 익명 파이프의 쓰기 끝도 못 열어(쓰기 제한 검사) PowerShell 이 명령 출력을 받지 못한다."""
    who = [_user_sid(tok), _sid("S-1-5-18"), _sid(SID_TEXT)] + ([logon] if logon else [])
    entries = (EXPLICIT_ACCESS_W * len(who))(*[
        EXPLICIT_ACCESS_W(0x10000000, GRANT_ACCESS, 0, TRUSTEE_W(None, 0, TRUSTEE_IS_SID, TRUSTEE_IS_GROUP, s.value)) for s in who])
    acl = ctypes.c_void_p()
    if adv.SetEntriesInAclW(len(who), entries, None, ctypes.byref(acl)) != 0:
        raise OSError(0, "SetEntriesInAcl(default DACL)")
    try:
        tdd = ctypes.c_void_p(acl.value)  # TOKEN_DEFAULT_DACL { PACL DefaultDacl }
        if not adv.SetTokenInformation(new, 6, ctypes.byref(tdd), ctypes.sizeof(tdd)):  # TokenDefaultDacl
            raise OSError(ctypes.get_last_error(), "SetTokenInformation(TokenDefaultDacl)")
    finally:
        k32.LocalFree(acl.value)


def _granted() -> set[str]:
    """권한을 준 폴더 — '경로\t만든 시각'. 폴더를 지웠다 다시 만들면 시각이 달라 다시 준다 (예: 같은 이름의 새 프로젝트, 캐시 비우기)."""
    try:
        return {ln for ln in _GRANTED.read_text(encoding="utf-8").splitlines() if "\t" in ln}
    except OSError:
        return set()


def _key(p: Path) -> str:
    try:
        born = p.stat().st_ctime_ns  # Windows: 만든 시각
    except OSError:
        born = 0
    return f"{os.path.normcase(str(p.resolve()))}\t{born}"


SE_FILE_OBJECT, DACL_SECURITY_INFORMATION = 1, 0x4
GRANT_ACCESS, SUB_CONTAINERS_AND_OBJECTS_INHERIT, TRUSTEE_IS_SID, TRUSTEE_IS_GROUP = 1, 0x3, 0, 2
FILE_MODIFY = 0x1301BF  # 수정 (읽기·쓰기·실행·삭제, 권한 변경은 없음)


def _add_ace(path: str, mode: int = 1) -> bool:
    """폴더 DACL 에 격리 SID 의 '수정' 허용(하위 폴더·파일 상속)을 더한다. Windows 가 기존 하위 항목에도 퍼뜨린다.
    icacls 는 계정으로 풀리지 않는 SID 를 거절해서 Win32 API 로 직접 한다."""
    old = ctypes.c_void_p()
    sd = ctypes.c_void_p()
    if adv.GetNamedSecurityInfoW(path, SE_FILE_OBJECT, DACL_SECURITY_INFORMATION, None, None, ctypes.byref(old), None,
                                 ctypes.byref(sd)) != 0:
        return False
    sid = _sid(SID_TEXT)
    ea = EXPLICIT_ACCESS_W(FILE_MODIFY, mode, SUB_CONTAINERS_AND_OBJECTS_INHERIT,
                           TRUSTEE_W(None, 0, TRUSTEE_IS_SID, TRUSTEE_IS_GROUP, sid.value))
    new = ctypes.c_void_p()
    try:
        if adv.SetEntriesInAclW(1, ctypes.byref(ea), old, ctypes.byref(new)) != 0:
            return False
        buf = ctypes.create_unicode_buffer(path)
        return adv.SetNamedSecurityInfoW(buf, SE_FILE_OBJECT, DACL_SECURITY_INFORMATION, None, None, new, None) == 0
    finally:
        for h in (new.value, sd.value, sid.value):
            if h:
                k32.LocalFree(h)


def grant(roots: list[str]) -> list[str]:
    """쓰기를 허락할 폴더에 격리 SID 의 수정 권한(상속)을 더한다 — 한 번 한 폴더는 기억해 두고 다시 하지 않는다."""
    done, out = _granted(), []
    for r in roots:
        p = Path(r)
        try:
            p.mkdir(parents=True, exist_ok=True)
        except OSError:
            continue
        key = _key(p)
        if key in done:
            continue
        if _add_ace(str(p)):
            path = key.split("\t")[0]
            done = {k for k in done if k.split("\t")[0] != path} | {key}
            out.append(str(p))
        else:
            # 권한을 바꿀 수 없는 폴더(관리자가 만든 폴더·네트워크 경로) — 조용히 넘어가면 그 안의 쓰기가 모두 '접근 거부'로만 보인다
            sys.stderr.write(f"[격리] 이 폴더에 쓰기 권한을 줄 수 없음 — 그 안에 쓰는 명령은 '접근 거부'로 실패한다: {p}\n")
    if out:
        done = {k for k in done if Path(k.split("\t")[0]).exists()}  # 지워진 폴더(끝난 실행의 사본 등)는 기록에서 뺀다
        _GRANTED.parent.mkdir(parents=True, exist_ok=True)
        _GRANTED.write_text("\n".join(sorted(done)), encoding="utf-8")
    return out


REVOKE_ACCESS = 4


def revoke() -> list[str]:
    """격리 실행에 준 권한을 모두 거둔다 (기록한 폴더에서 격리 SID 항목을 지운다). 다음에 격리 실행을 쓰면 다시 준다."""
    out = []
    for key in sorted(_granted()):
        path = key.split("\t")[0]
        if Path(path).exists() and _add_ace(path, REVOKE_ACCESS):
            out.append(path)
    _GRANTED.unlink(missing_ok=True)
    return out


def command_line(argv: list[str]) -> str:
    """CreateProcess 에 줄 한 줄. cmd /c 는 보통 프로그램과 달리 \" 이스케이프를 모른다 — 명령 문자열을 그대로 /s /c "…" 로 넘긴다
    (따옴표로 감싼 실행 파일 경로·인자가 그대로 살아야 툴킷이 돌리는 검증 명령이 된다)."""
    if len(argv) == 4 and Path(argv[0]).stem.lower() == "cmd" and [a.lower() for a in argv[1:3]] == ["/d", "/c"]:
        return f'{argv[0]} /d /s /c "{argv[3]}"'
    return subprocess.list2cmdline(argv)


def run(argv: list[str], roots: list[str], cwd: str | None = None) -> int:
    """argv 를 쓰기 제한 토큰으로 띄우고 끝날 때까지 기다린다. 표준 입출력은 이 프로세스의 것을 그대로 넘긴다."""
    grant(roots)
    # 격리 안에서 '분리된' 프로세스(새 콘솔)를 띄우는 도구는 시작에 실패한다 — 오류 대화상자가 화면에 뜨지 않게 (자식에게 이어짐)
    k32.SetErrorMode(0x0001 | 0x0002 | 0x8000)  # SEM_FAILCRITICALERRORS | SEM_NOGPFAULTERRORBOX | SEM_NOOPENFILEERRORBOX
    # 뒤에서 몰래 도는 업데이트 확인(분리된 프로세스)을 끈다 — npm 등
    os.environ.setdefault("NO_UPDATE_NOTIFIER", "1")
    os.environ.setdefault("npm_config_update_notifier", "false")
    token = restricted_token()
    si = STARTUPINFOW()
    si.cb = ctypes.sizeof(si)
    si.dwFlags = STARTF_USESTDHANDLES
    k32.GetStdHandle.restype = wt.HANDLE
    si.hStdInput, si.hStdOutput, si.hStdError = (k32.GetStdHandle(-10), k32.GetStdHandle(-11), k32.GetStdHandle(-12))
    pi = PROCESS_INFORMATION()
    cmd = ctypes.create_unicode_buffer(command_line(argv))
    # 이 프로세스의 콘솔을 함께 쓴다 (main 이 콘솔 없이 불렸으면 숨은 콘솔을 먼저 만들어 둔다 — 제한 토큰은 새 콘솔을 못 만든다)
    if not adv.CreateProcessAsUserW(token, None, cmd, None, None, True, CREATE_UNICODE_ENVIRONMENT, None, cwd,
                                    ctypes.byref(si), ctypes.byref(pi)):
        err = ctypes.get_last_error()
        k32.CloseHandle(token)
        raise OSError(err, f"격리 실행을 시작하지 못함 (오류 {err})")
    k32.WaitForSingleObject(pi.hProcess, INFINITE)
    code = wt.DWORD()
    k32.GetExitCodeProcess(pi.hProcess, ctypes.byref(code))
    for h in (pi.hThread, pi.hProcess, token):
        k32.CloseHandle(h)
    return code.value


def fingerprint() -> str:
    return hashlib.sha1(SID_TEXT.encode()).hexdigest()[:8]


def has_console() -> bool:
    lst = (wt.DWORD * 2)()
    return k32.GetConsoleProcessList(lst, 2) > 0


def main(args: list[str]) -> int:
    if not has_console() and os.environ.get("BEOM_SBX_INNER") != "1":
        # 콘솔 없이 불림(앱·에이전트) — 제한 토큰 프로세스는 스스로 콘솔을 만들지 못해 시작부터 실패한다(0xC0000142).
        # 창 없는 콘솔을 가진 같은 명령으로 한 번 더 띄운다 (입출력·끝 코드는 그대로 이어짐, 창은 뜨지 않음)
        env = {**os.environ, "BEOM_SBX_INNER": "1"}
        return subprocess.run([sys.executable, "-m", "beom_workbench.core.winsandbox", *args], env=env,
                              creationflags=CREATE_NO_WINDOW).returncode
    roots: list[str] = []
    cwd = None
    while args and args[0] != "--":
        flag = args.pop(0)
        if flag == "--root":
            roots.append(args.pop(0))
        elif flag == "--cwd":
            cwd = args.pop(0)
    if not args or args[0] != "--" or len(args) < 2:
        sys.stderr.write("usage: python -m beom_workbench.core.winsandbox --root DIR ... -- program args...\n")
        return 2
    return run(args[1:], roots, cwd)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
