"""Run staged scripts inside a temporary Windows Less Privileged AppContainer.

The application has no network capability, an explicit handle allowlist and a single-
process kill-on-close Job. No code executes before these limits are attached.
"""

import asyncio
import ctypes as ct
from ctypes import wintypes as wt
from contextlib import ExitStack, contextmanager
import msvcrt
import os
from pathlib import Path
import subprocess
import threading
import time
import uuid

from .windows_runtime import stage_command

PTR = ct.c_void_p
SIZE = ct.c_size_t
DWORD = wt.DWORD
HANDLE = wt.HANDLE
kernel = ct.WinDLL("kernel32", use_last_error=True)
userenv = ct.WinDLL("userenv", use_last_error=True)
advapi = ct.WinDLL("advapi32", use_last_error=True)


class StartupInfo(ct.Structure):
    _fields_ = [
        ("cb", DWORD),
        ("reserved", wt.LPWSTR),
        ("desktop", wt.LPWSTR),
        ("title", wt.LPWSTR),
        ("x", DWORD),
        ("y", DWORD),
        ("width", DWORD),
        ("height", DWORD),
        ("cols", DWORD),
        ("rows", DWORD),
        ("fill", DWORD),
        ("flags", DWORD),
        ("show", wt.WORD),
        ("reserved_size", wt.WORD),
        ("reserved_data", PTR),
        ("stdin", HANDLE),
        ("stdout", HANDLE),
        ("stderr", HANDLE),
    ]


class ExtendedStartup(ct.Structure):
    _fields_ = [("info", StartupInfo), ("attributes", PTR)]


class ProcessInfo(ct.Structure):
    _fields_ = [("process", HANDLE), ("thread", HANDLE), ("pid", DWORD), ("tid", DWORD)]


class Capabilities(ct.Structure):
    _fields_ = [
        ("sid", PTR),
        ("capabilities", PTR),
        ("count", DWORD),
        ("reserved", DWORD),
    ]


class CapabilityEntry(ct.Structure):
    _fields_ = [("sid", PTR), ("attributes", DWORD)]


class BasicLimits(ct.Structure):
    _fields_ = [
        ("process_time", ct.c_int64),
        ("job_time", ct.c_int64),
        ("flags", DWORD),
        ("working_min", SIZE),
        ("working_max", SIZE),
        ("active", DWORD),
        ("affinity", SIZE),
        ("priority", DWORD),
        ("scheduling", DWORD),
    ]


class ExtendedLimits(ct.Structure):
    _fields_ = [
        ("basic", BasicLimits),
        ("io_counters", ct.c_uint64 * 6),
        ("process_memory", SIZE),
        ("job_memory", SIZE),
        ("peak_process", SIZE),
        ("peak_job", SIZE),
    ]


def win_function(lib, name, restype, *args):
    function = getattr(lib, name)
    function.restype = restype
    function.argtypes = args
    return function


make_profile = win_function(
    userenv,
    "CreateAppContainerProfile",
    wt.LONG,
    wt.LPCWSTR,
    wt.LPCWSTR,
    wt.LPCWSTR,
    PTR,
    DWORD,
    ct.POINTER(PTR),
)
drop_profile = win_function(userenv, "DeleteAppContainerProfile", wt.LONG, wt.LPCWSTR)
free_sid = win_function(advapi, "FreeSid", PTR, PTR)
derive_capability = win_function(
    ct.WinDLL("kernelbase", use_last_error=True),
    "DeriveCapabilitySidsFromName",
    wt.BOOL,
    wt.LPCWSTR,
    ct.POINTER(ct.POINTER(PTR)),
    ct.POINTER(DWORD),
    ct.POINTER(ct.POINTER(PTR)),
    ct.POINTER(DWORD),
)
format_sid = win_function(
    advapi, "ConvertSidToStringSidW", wt.BOOL, PTR, ct.POINTER(wt.LPWSTR)
)
local_free = win_function(kernel, "LocalFree", HANDLE, HANDLE)
create_job = win_function(kernel, "CreateJobObjectW", HANDLE, PTR, wt.LPCWSTR)
set_job = win_function(
    kernel, "SetInformationJobObject", wt.BOOL, HANDLE, DWORD, PTR, DWORD
)
close = win_function(kernel, "CloseHandle", wt.BOOL, HANDLE)
init_attrs = win_function(
    kernel,
    "InitializeProcThreadAttributeList",
    wt.BOOL,
    PTR,
    DWORD,
    DWORD,
    ct.POINTER(SIZE),
)
add_attr = win_function(
    kernel, "UpdateProcThreadAttribute", wt.BOOL, PTR, DWORD, SIZE, PTR, SIZE, PTR, PTR
)
free_attrs = win_function(kernel, "DeleteProcThreadAttributeList", None, PTR)
launch = win_function(
    kernel,
    "CreateProcessW",
    wt.BOOL,
    wt.LPCWSTR,
    wt.LPWSTR,
    PTR,
    PTR,
    wt.BOOL,
    DWORD,
    PTR,
    wt.LPCWSTR,
    PTR,
    ct.POINTER(ProcessInfo),
)
peek = win_function(
    kernel, "PeekNamedPipe", wt.BOOL, HANDLE, PTR, DWORD, PTR, ct.POINTER(DWORD), PTR
)
wait = win_function(kernel, "WaitForSingleObject", DWORD, HANDLE, DWORD)
exit_code = win_function(
    kernel, "GetExitCodeProcess", wt.BOOL, HANDLE, ct.POINTER(DWORD)
)
end_job = win_function(kernel, "TerminateJobObject", wt.BOOL, HANDLE, wt.UINT)


def checked(ok):
    if not ok:
        raise ct.WinError(ct.get_last_error())
    return ok


@contextmanager
def profile():
    name = "DshFleet." + uuid.uuid4().hex
    sid = PTR()
    result = make_profile(
        name, name, "Ephemeral isolated script", None, 0, ct.byref(sid)
    )
    if result:
        raise OSError(f"CreateAppContainerProfile failed (0x{result & 0xFFFFFFFF:08x})")
    try:
        text = wt.LPWSTR()
        checked(format_sid(sid, ct.byref(text)))
        try:
            yield sid, text.value
        finally:
            local_free(ct.cast(text, PTR))
    finally:
        free_sid(sid)
        result = drop_profile(name)
        if result:
            raise OSError(
                f"DeleteAppContainerProfile failed (0x{result & 0xFFFFFFFF:08x})"
            )


@contextmanager
def named_capabilities(names):
    # LPAC can read OS configuration only through specific system capabilities.
    # Neither these SIDs nor the temporary package have a network entitlement.
    entries = []
    with ExitStack() as cleanup:
        for name in names:
            groups, capabilities = ct.POINTER(PTR)(), ct.POINTER(PTR)()
            group_count, count = DWORD(), DWORD()
            checked(
                derive_capability(
                    name,
                    ct.byref(groups),
                    ct.byref(group_count),
                    ct.byref(capabilities),
                    ct.byref(count),
                )
            )
            for items, length in (
                (groups, group_count.value),
                (capabilities, count.value),
            ):
                cleanup.callback(local_free, ct.cast(items, PTR))
                for index in range(length):
                    cleanup.callback(local_free, items[index])
            entries.extend(
                CapabilityEntry(capabilities[i], 4) for i in range(count.value)
            )
        yield (CapabilityEntry * len(entries))(*entries)


@contextmanager
def attributes(values):
    size = SIZE()
    init_attrs(None, len(values), 0, ct.byref(size))
    if not size.value or ct.get_last_error() != 122:  # ERROR_INSUFFICIENT_BUFFER
        raise ct.WinError(ct.get_last_error())
    data = ct.create_string_buffer(size.value)
    checked(init_attrs(data, len(values), 0, ct.byref(size)))
    try:
        for key, value in values:
            checked(
                add_attr(data, 0, key, ct.byref(value), ct.sizeof(value), None, None)
            )
        yield ct.cast(data, PTR)
    finally:
        free_attrs(data)


def read_output(proc, pipe, stopped, deadline, output_limit):
    result = bytearray()
    handle = msvcrt.get_osfhandle(pipe)
    while True:
        if stopped.is_set():
            raise InterruptedError("Native run cancelled")
        if time.monotonic() >= deadline:
            raise TimeoutError("Native script exceeded its wall time limit")
        count = DWORD()
        if peek(handle, None, 0, None, ct.byref(count), None):
            if count.value:
                result.extend(os.read(pipe, min(8192, count.value)))
                if len(result) > output_limit:
                    raise ValueError("Native script output exceeded its limit")
                continue
        elif ct.get_last_error() != 109:  # ERROR_BROKEN_PIPE
            raise ct.WinError(ct.get_last_error())
        status = wait(proc, 25)
        if status == 0:
            # Output might have arrived between the last peek and process exit.
            if peek(handle, None, 0, None, ct.byref(count), None) and count.value:
                continue
            code = DWORD()
            checked(exit_code(proc, ct.byref(code)))
            if result.startswith((b"\xff\xfe", b"\xfe\xff")):
                output = result.decode("utf-16", errors="replace")
            elif b"\x00" in result[:128]:
                # Windows PowerShell emits early host diagnostics as UTF-16LE.
                output = result.decode("utf-16le", errors="replace")
            else:
                try:
                    output = result.decode("utf8")
                except UnicodeDecodeError:
                    output = result.decode("oem", errors="replace")
            return f"exit={code.value}\n" + output
        if status != 258:  # WAIT_TIMEOUT
            raise ct.WinError(ct.get_last_error())


def run_sync(plan, root, stage, stopped, output_limit):
    command = stage_command(plan, root, stage)
    if stopped.is_set():
        raise InterruptedError("Native run cancelled")
    system_root = Path(os.environ["SystemRoot"])
    with profile() as (sid, sid_text), ExitStack() as resources:
        # Only the new job-owned tree is modified. Existing runtime installations
        # and workspace/NAS/private directories retain their original DACL.
        subprocess.run(
            [
                str(system_root / "System32" / "icacls.exe"),
                str(root),
                "/grant",
                f"*{sid_text}:(OI)(CI)(RX)",
                "/Q",
            ],
            check=True,
            capture_output=True,
            timeout=25,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        job = checked(create_job(None, None))
        resources.callback(close, job)
        limits = ExtendedLimits()
        # PROCESS_TIME | ACTIVE_PROCESS | PROCESS_MEMORY | KILL_ON_JOB_CLOSE
        limits.basic.flags = 0x2 | 0x8 | 0x100 | 0x2000
        limits.basic.active = 1
        limits.basic.process_time = 30 * 10_000_000
        limits.process_memory = 512 * 1024 * 1024
        checked(set_job(job, 9, ct.byref(limits), ct.sizeof(limits)))
        read_fd, write_fd = os.pipe()
        resources.callback(os.close, read_fd)
        with os.fdopen(write_fd, "wb") as writer, open(os.devnull, "rb") as null:
            handles = (HANDLE * 2)(
                msvcrt.get_osfhandle(null.fileno()),
                msvcrt.get_osfhandle(writer.fileno()),
            )
            for handle in handles:
                os.set_handle_inheritable(handle, True)
            startup = ExtendedStartup()
            startup.info.cb = ct.sizeof(startup)
            startup.info.flags = 0x100  # STARTF_USESTDHANDLES
            startup.info.stdin, startup.info.stdout, startup.info.stderr = (
                handles[0],
                handles[1],
                handles[1],
            )
            process = ProcessInfo()
            # HANDLE_LIST, SECURITY_CAPABILITIES, JOB_LIST, CHILD_PROCESS_POLICY,
            # ALL_APPLICATION_PACKAGES_POLICY (LPAC opt out).
            names = ["registryRead"]
            if plan.language == "powershell":
                # The stock managed PowerShell engine initializes COM and its ETW provider.
                names.extend(["lpacCom", "lpacInstrumentation"])
            sids = resources.enter_context(named_capabilities(names))
            security = Capabilities(sid, ct.cast(sids, PTR), len(sids), 0)
            values = [
                (0x20002, handles),
                (0x20009, security),
                (0x2000D, (HANDLE * 1)(job)),
                (0x2000E, DWORD(1)),
                (0x2000F, DWORD(1)),
            ]
            environment = {
                "SystemRoot": str(system_root),
                "WINDIR": str(system_root),
                "LOCALAPPDATA": str(root),
                "TEMP": str(root),
                "TMP": str(root),
                "PATH": str(system_root / "System32"),
                "POWERSHELL_TELEMETRY_OPTOUT": "1",
                "DOTNET_CLI_TELEMETRY_OPTOUT": "1",
                "DOTNET_EnableDiagnostics": "0",
                "PSModulePath": str(
                    system_root / "System32/WindowsPowerShell/v1.0/Modules"
                ),
            }
            block = ct.create_unicode_buffer(
                "\0".join(f"{k}={v}" for k, v in sorted(environment.items())) + "\0"
            )
            # DETACHED_PROCESS avoids allocating a console host inside the single-process
            # Job. Output uses private pipes. CREATE_NO_WINDOW still creates a console.
            flags = (
                subprocess.DETACHED_PROCESS | 0x80000 | 0x400
            )  # EXTENDED_STARTUPINFO, UNICODE_ENVIRONMENT
            with attributes(values) as startup.attributes:
                if stopped.is_set():
                    raise InterruptedError("Native run cancelled")
                checked(
                    launch(
                        command[0],
                        ct.create_unicode_buffer(subprocess.list2cmdline(command)),
                        None,
                        None,
                        True,
                        flags,
                        block,
                        str(stage),
                        ct.byref(startup),
                        ct.byref(process),
                    )
                )
            close(process.thread)
            resources.callback(close, process.process)
        try:
            result = read_output(
                process.process, read_fd, stopped, time.monotonic() + 45, output_limit
            )
            if plan.language == "python":
                status, newline, output = result.partition("\n")
                # Some CPython distributions report the inaccessible host parent of their
                # private ._pth runtime before printing user output. Keep unrelated warnings.
                warning = "Failed to find real location of " + command[0]
                first, separator, remaining = output.partition("\n")
                if separator and first.rstrip("\r") == warning:
                    return status + newline + remaining
            return result
        finally:
            checked(end_job(job, 1))
            if wait(process.process, 5000) != 0:
                raise RuntimeError("Native job failed to terminate")


async def run_native(plan, root, stage, output_limit):
    stopped = threading.Event()
    worker = asyncio.create_task(
        asyncio.to_thread(run_sync, plan, root, stage, stopped, output_limit)
    )
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError:
        stopped.set()
        # Repeated UI requests or policy changes may cancel this task again. A
        # running OS thread still owns the Job and staging files until it exits.
        while not worker.done():
            try:
                await asyncio.shield(worker)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        if not worker.cancelled():
            worker.exception()
        raise
