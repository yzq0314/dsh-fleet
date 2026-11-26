import asyncio
import os
import signal
import subprocess

import psutil


def spawn_settings():
    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NO_WINDOW}
    return {"start_new_session": True}


def terminate_tree(pid):
    try:
        root = psutil.Process(pid)
        children = root.children(recursive=True)
    except psutil.NoSuchProcess:
        return
    if os.name != "nt":
        try:
            if os.getpgid(pid) == pid:
                os.killpg(pid, signal.SIGSTOP)
        except ProcessLookupError:
            pass
    for proc in [root, *reversed(children)]:
        try:
            proc.kill()
        except psutil.NoSuchProcess:
            pass
    if os.name != "nt":
        try:
            os.killpg(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    psutil.wait_procs(children, timeout=2)


async def stop_process(process):
    if process and process.returncode is None:
        await asyncio.to_thread(terminate_tree, process.pid)
        await process.wait()
