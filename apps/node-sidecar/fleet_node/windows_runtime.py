"""Prepare interpreters for a read-only AppContainer. This module never launches a script."""

import codecs
import os
import shutil
import subprocess
import sys
import sysconfig
import zipfile
from pathlib import Path


def _node_executable():
    # PATH belongs to the Sidecar, not the job. Never search a relative entry or its CWD.
    for value in os.environ.get("PATH", "").split(os.pathsep):
        directory = Path(value.strip('"'))
        if directory.is_absolute() and directory != Path.cwd():
            candidate = directory / "node.exe"
            if candidate.is_file():
                return candidate.resolve()
    return None


def _powershell_sdk():
    root = Path(os.environ.get("SystemRoot", ""))
    if root.is_absolute():
        framework = root / "Microsoft.NET"
        assembly = (
            framework
            / "assembly/GAC_MSIL/System.Management.Automation"
            / "v4.0_3.0.0.0__31bf3856ad364e35/System.Management.Automation.dll"
        )
        for arch in ("Framework64", "Framework"):
            compiler = framework / arch / "v4.0.30319/csc.exe"
            if compiler.is_file() and assembly.is_file():
                return compiler, assembly
    return None


def _powershell_runtime(runtime, sdk):
    compiler, assembly = sdk
    system = Path(os.environ["SystemRoot"])
    exe = runtime / "fleet-powershell.exe"
    # Only the shipped fixed host is compiled outside the LPAC. The script path,
    # arguments and workspace never enter the compiler invocation or its cwd.
    source = Path(__file__).with_name("windows_powershell.cs")
    env = {
        "SystemRoot": str(system),
        "WINDIR": str(system),
        "TEMP": str(runtime),
        "TMP": str(runtime),
        "PATH": str(system / "System32"),
    }
    result = subprocess.run(
        [
            str(compiler),
            "/nologo",
            "/noconfig",
            "/target:exe",
            "/optimize+",
            "/reference:System.dll",
            "/reference:System.Core.dll",
            "/reference:" + str(assembly),
            "/out:" + str(exe),
            str(source),
        ],
        cwd=runtime,
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=25,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    if result.returncode:
        raise RuntimeError(
            "Failed to compile the fixed Windows PowerShell SDK host: "
            + result.stdout.decode("oem", errors="replace")[-800:]
        )
    return exe


def native_languages():
    if os.name != "nt":
        return []
    languages = []
    if Path(sys._base_executable).is_file() and sys.implementation.name == "cpython":
        languages.append("python")
    if _node_executable():
        languages.append("node")
    if _powershell_sdk():
        languages.append("powershell")
    return languages


def _copy_libraries(destination, directories, patterns):
    for directory in directories:
        for pattern in patterns:
            for library in directory.glob(pattern):
                target = destination / library.name
                if library.is_file() and not target.exists():
                    shutil.copyfile(library, target)


def _pack_stdlib(source, target):
    # Use the interpreter's own module catalogue, including version-specific modules.
    # Personal/site packages, .pth files and environment customizations stay on the host.
    with zipfile.ZipFile(target, "x", compression=zipfile.ZIP_STORED) as bundle:
        for name in sorted(sys.stdlib_module_names):
            module = source / (name + ".py")
            package = source / name
            if module.is_file() and module.resolve().is_relative_to(source):
                bundle.write(module, module.name)
            elif package.is_dir() and package.resolve().is_relative_to(source):
                for directory, children, files in os.walk(package):
                    here = Path(directory)
                    children[:] = [
                        child
                        for child in children
                        if child not in {"__pycache__", "test", "tests"}
                        and (here / child).resolve().is_relative_to(package)
                        and not (here / child).is_symlink()
                    ]
                    for filename in files:
                        path = here / filename
                        if path.suffix == ".py" and path.resolve().is_relative_to(
                            package
                        ):
                            bundle.write(path, path.relative_to(source).as_posix())


def _python_runtime(runtime, stage):
    base = Path(sys.base_prefix).resolve()
    executable = runtime / "python.exe"
    shutil.copyfile(sys._base_executable, executable)
    tag = f"python{sys.version_info.major}{sys.version_info.minor}"
    if sysconfig.get_config_var("Py_GIL_DISABLED"):
        tag += "t"
    stdlib = Path(sysconfig.get_path("stdlib")).resolve()
    archive = runtime / (tag + ".zip")
    if (stdlib / "encodings/__init__.py").is_file():
        _pack_stdlib(stdlib, archive)
    elif (base / archive.name).is_file():
        shutil.copyfile(base / archive.name, archive)
    else:
        raise RuntimeError("Cannot stage the installed CPython standard library")
    extensions = base / "DLLs"
    for path in extensions.glob("*.pyd"):
        if path.name.partition(".")[0] in sys.stdlib_module_names:
            shutil.copyfile(path, runtime / path.name)
    _copy_libraries(
        runtime,
        (base, extensions, base / "Library/bin"),
        (
            tag + ".dll",
            "python3.dll",
            "vcruntime140*.dll",
            "msvcp140*.dll",
            "libcrypto*.dll",
            "libssl*.dll",
            "libffi*.dll",
            "sqlite3.dll",
            "libexpat.dll",
            "zlib*.dll",
            "liblzma.dll",
            "libbz2.dll",
            "bz2.dll",
        ),
    )
    if not (runtime / (tag + ".dll")).is_file():
        raise RuntimeError("Cannot stage the installed CPython runtime DLL")
    relative_stage = os.path.relpath(stage, runtime)
    # A ._pth next to the actual interpreter DLL supersedes registry paths and site loading.
    (runtime / (tag + "._pth")).write_text(
        f"{archive.name}\n.\n{relative_stage}\n", encoding="utf8"
    )
    return executable


def stage_command(plan, root, stage):
    """Return an argv array for a fresh private bundle. ACLs and launch are the caller's job."""
    if os.name != "nt":
        raise RuntimeError("Native Windows execution requires Windows")
    stage = Path(stage).resolve()
    script = (stage / plan.script).resolve(strict=True)
    if not script.is_relative_to(stage):
        raise ValueError("Script is outside the staged inputs")
    if plan.language in {"python", "node", "powershell"}:
        runtime = Path(root) / "runtime"
        runtime.mkdir(exist_ok=True)
        if plan.language == "powershell":
            if not (sdk := _powershell_sdk()):
                raise RuntimeError(
                    "The Windows PowerShell 5.1 SDK and .NET Framework 4 compiler are required"
                )
            # Windows PowerShell 5.1 otherwise interprets UTF-8 text as the
            # local ANSI code page. Only the private copy gets the encoding
            # marker, so the engine executes the Unicode source that was reviewed.
            source = script.read_bytes()
            if not source.startswith(codecs.BOM_UTF8) and not source.isascii():
                source.decode("utf-8", errors="strict")
                script.write_bytes(codecs.BOM_UTF8 + source)
            return [str(_powershell_runtime(runtime, sdk)), str(script), *plan.args]
        if plan.language == "python":
            executable = _python_runtime(runtime, stage)
            return [
                str(executable),
                "-I",
                "-B",
                "-S",
                "-X",
                "utf8",
                str(script),
                *plan.args,
            ]
        source = _node_executable()
        if source:
            executable = runtime / "node.exe"
            shutil.copyfile(source, executable)
            _copy_libraries(
                runtime,
                (source.parent,),
                (
                    "libnode.dll",
                    "node.dll",
                    "libuv.dll",
                    "vcruntime140*.dll",
                    "msvcp140*.dll",
                ),
            )
            # The staged path is already resolved. Avoid traversing private drive
            # ancestors to canonicalize the main module again inside the LPAC.
            return [
                str(executable),
                "--no-addons",
                "--preserve-symlinks",
                "--preserve-symlinks-main",
                "--",
                str(script),
                *plan.args,
            ]
    raise RuntimeError(f"Local Windows runtime unavailable: {plan.language}")
