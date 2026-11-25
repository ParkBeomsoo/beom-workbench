"""Windows: keep an agent's whole process tree tied to the app — a kill-on-close Job Object.

If the app crashed, the agent and everything it started kept running and kept editing files. Other tools that host agent
CLIs (the Codex CLI's process spawner, VS Code's shim, the process-wrap crate) put the child in a Job Object with
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE: when the last handle to the job closes — including when the app process dies — Windows
ends every process in it. The child is started suspended, assigned, then resumed, so nothing it spawns escapes before the
assignment (assignment is not retroactive). Stopping uses TerminateJobObject, which ends exactly the job's processes —
`taskkill /T` follows parent PIDs, and Windows reuses PIDs. On other systems this module does nothing (the runner starts
agents in their own session and stops the process group).
"""
from __future__ import annotations

import subprocess
import sys

CREATE_SUSPENDED = 0x00000004
_KILL_ON_JOB_CLOSE = 0x00002000
# 스스로 Job 밖에서 띄우겠다고 요청한 프로세스(CREATE_BREAKAWAY_FROM_JOB)는 허용 — 막으면 그 도구가 시작에 실패한다 (Codex 의 기본 Job 도 같다)
_BREAKAWAY_OK = 0x00000800
_JOB_EXTENDED_LIMIT = 9

if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    class _IO(ctypes.Structure):
        _fields_ = [(n, ctypes.c_ulonglong) for n in ("r_ops", "w_ops", "o_ops", "r_bytes", "w_bytes", "o_bytes")]

    class _BASIC(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong), ("PerJobUserTimeLimit", ctypes.c_longlong),
                    ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD), ("SchedulingClass", wintypes.DWORD)]

    class _EXTENDED(ctypes.Structure):
        _fields_ = [("Basic", _BASIC), ("Io", _IO), ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _nt = ctypes.WinDLL("ntdll")
    _k32.CreateJobObjectW.restype = wintypes.HANDLE
    _k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    _k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    _k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    _k32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    _k32.CloseHandle.argtypes = [wintypes.HANDLE]
    _nt.NtResumeProcess.argtypes = [wintypes.HANDLE]


def spawn_flags() -> int:
    """Popen 에 더할 플래그 — Windows 는 일시 정지 상태로 띄워 Job 에 넣은 뒤 다시 시작한다."""
    return CREATE_SUSPENDED if sys.platform == "win32" else 0


def contain(proc: subprocess.Popen) -> int | None:
    """일시 정지 상태로 띄운 프로세스를 닫히면 함께 끝나는 Job 에 넣고 다시 시작한다. Job 핸들(실패하면 None) — 어떤 경우든 다시 시작한다."""
    if sys.platform != "win32":
        return None
    job = None
    try:
        job = _k32.CreateJobObjectW(None, None)
        if job:
            info = _EXTENDED()
            info.Basic.LimitFlags = _KILL_ON_JOB_CLOSE | _BREAKAWAY_OK
            if not _k32.SetInformationJobObject(job, _JOB_EXTENDED_LIMIT, ctypes.byref(info), ctypes.sizeof(info)) or \
                    not _k32.AssignProcessToJobObject(job, int(proc._handle)):  # noqa: SLF001 — Popen 의 프로세스 핸들
                _k32.CloseHandle(job)  # 넣지 못했다(중첩 Job 이 막는 경우) — 예전처럼 프로세스 트리로 끝낸다
                job = None
    except (OSError, AttributeError):
        job = None
    finally:
        try:
            _nt.NtResumeProcess(int(proc._handle))  # noqa: SLF001 — Popen 이 주 스레드 핸들을 닫아서 프로세스 단위로 다시 시작한다
        except (OSError, AttributeError):
            pass
    return job


def terminate(job: int | None) -> bool:
    """Job 의 프로세스를 모두 끝낸다 (PID 를 따라가지 않으니 다시 쓰인 PID 의 남의 프로세스를 끝낼 일이 없다)."""
    if sys.platform != "win32" or not job:
        return False
    try:
        return bool(_k32.TerminateJobObject(job, 1))
    except OSError:
        return False


def close(job: int | None) -> None:
    """Job 핸들을 닫는다 — 아직 남은 프로세스(에이전트가 띄우고 두고 간 것)도 함께 끝난다."""
    if sys.platform == "win32" and job:
        try:
            _k32.CloseHandle(job)
        except OSError:
            pass
