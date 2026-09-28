"""Process-wide Python defense in depth, NOT a native-code security sandbox.

Call install_guard() before core work. Use Docker --network none for actual OS
network isolation. A local model must share that isolated network namespace.
The opt-in PDF parser is trusted native code, not sandboxed by Python hooks.
Windows SMB filesystem traffic need not pass through Python sockets. Validate
untrusted paths BEFORE resolve/stat/is_file; filesystem audit events below are
only a backstop (notably os.stat and Path.resolve have no reliable audit event).
Lexical checks cannot detect mapped drives or local junctions to remote shares.
"""
from __future__ import annotations

import contextvars
import inspect
import ipaddress
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
from urllib.parse import unquote, urlsplit

_lock = threading.Lock()
_installed = False
_pdf_enabled = False
_parser: str | None = None
_parser_env: dict[str, str] = {}
_permit = contextvars.ContextVar("ebe_pdf_launch", default=None)


def validate_local_path(value: str | bytes | os.PathLike) -> str | bytes:
    """Reject network/device path syntax without any filesystem or DNS access.

    Return os.fspath(value) unchanged for use by the caller. Accept local relative,
    drive and POSIX paths; reject all UNC, extended/device namespaces (including
    extended local paths), mixed slash spellings and native NT namespaces on ALL
    platforms. Raises ValueError for forbidden syntax, TypeError for non-paths.

    Call before Path.resolve/exists/is_file, os.stat, or third-party native I/O.
    Recheck after expanduser/variable expansion and paths loaded from metadata/DB.
    This is not a containment check: local links, mapped drives, inherited remote
    cwd and races still require OS isolation and trusted local storage. PathLike
    implementations themselves must be trusted (their __fspath__ is Python code).
    """
    path = os.fspath(value)
    text = os.fsdecode(path)
    normalized = text.replace("/", "\\").casefold()
    if ("\x00" in text or normalized.startswith("\\\\")
            or normalized.startswith(("\\??\\", "\\device\\", "\\global??\\", "\\globalroot\\"))
            or text.casefold().startswith("file:")):
        # Do not include potentially private host/share names in exceptions.
        raise ValueError("EBE requires a local path; network and device paths are forbidden")
    return path


# Only actual documented CPython filesystem audit events. Do not imply that
# adding a fictitious os.stat event would guard stat/resolve on Windows.
_FS_PATH_ARGS = {
    "open": (0,), "os.listdir": (0,), "os.scandir": (0,), "os.chdir": (0,),
    "os.mkdir": (0,), "os.remove": (0,), "os.rmdir": (0,), "os.chmod": (0,),
    "os.chown": (0,), "os.truncate": (0,), "os.utime": (0,),
    "os.rename": (0, 1), "os.link": (0, 1), "os.symlink": (0, 1),
    "shutil.copyfile": (0, 1), "shutil.copymode": (0, 1),
    "shutil.copystat": (0, 1), "shutil.copytree": (0, 1),
    "shutil.move": (0, 1), "shutil.rmtree": (0,),
    "ctypes.dlopen": (0,),
}


def _validate_sqlite_database(value):
    """Allow only local file: URIs with the exact mode=ro query for SQLite.

    The audit event supplies the database name, not the uri keyword. This checks
    URI syntax/location; callers still need uri=True to obtain SQLite read-only
    semantics. Decode once, as SQLite does, before the ordinary lexical check.
    """
    text = os.fsdecode(os.fspath(value))
    if not text.casefold().startswith("file:"):
        validate_local_path(value)
        return
    if (not text.startswith("file:") or any(ord(c) < 32 or ord(c) == 127 for c in text)
            or "#" in text or re.search(r"%(?![0-9a-fA-F]{2})", text)):
        raise ValueError("invalid local read-only SQLite URI")
    uri = urlsplit(text)
    if uri.scheme != "file" or uri.netloc or not uri.path or uri.query != "mode=ro":
        raise ValueError("SQLite file URI must be local with only mode=ro")
    validate_local_path(unquote(uri.path, encoding="utf-8", errors="strict"))


def _loopback(host) -> bool:
    if isinstance(host, bytes):
        try:
            host = host.decode("ascii")
        except UnicodeError:
            return False
    if not isinstance(host, str) or "%" in host:
        return False
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _audit(event, args):
    if event == "sqlite3.connect":
        _validate_sqlite_database(args[0])
    elif event in _FS_PATH_ARGS:
        for index in _FS_PATH_ARGS[event]:
            path = args[index]
            if path is not None and not isinstance(path, int):
                validate_local_path(path)
    elif event in {"socket.connect", "socket.bind", "socket.sendto", "socket.sendmsg"}:
        sock, address = args[0], args[-1]
        if (sock.family not in (socket.AF_INET, socket.AF_INET6)
                or not isinstance(address, tuple) or not _loopback(address[0])):
            raise PermissionError("EBE core permits only literal loopback sockets")
    elif event in {"socket.getaddrinfo", "socket.gethostbyname"}:
        if not _loopback(args[0]):
            raise PermissionError("EBE core forbids external DNS")
    elif event in {"socket.getnameinfo", "socket.gethostbyaddr"}:
        raise PermissionError("EBE core forbids reverse DNS")
    elif event == "subprocess.Popen":
        permitted = _permit.get()
        if permitted is None or args != permitted:
            raise PermissionError("EBE core forbids subprocesses; use the fixed local model adapter")
    elif event in {"os.system", "os.posix_spawn", "os.exec", "os.spawn", "os.fork", "os.forkpty"}:
        raise PermissionError("EBE core forbids process execution")


def _wrap_sockets():
    # CPython may resolve sockaddr hostnames BEFORE emitting its audit event.
    # Keep the hook as a backstop and reject at public Python entry points too.
    def wrap_address(original, event, last=False):
        def guarded(self, *args, **kwargs):
            address = args[-1] if last and args else args[0] if args else kwargs.get("address")
            _audit(event, (self, address))
            return original(self, *args, **kwargs)
        return guarded
    for name in ("connect", "connect_ex", "bind", "sendto"):
        setattr(socket.socket, name, wrap_address(getattr(socket.socket, name),
                "socket.bind" if name == "bind" else "socket.connect", name == "sendto"))
    if hasattr(socket.socket, "sendmsg"):
        original_sendmsg = socket.socket.sendmsg
        def sendmsg(self, buffers, ancdata=(), flags=0, address=None):
            if address is not None:
                _audit("socket.connect", (self, address))
            # Connected sendmsg is denied by the audit hook as a conservative policy.
            return original_sendmsg(self, buffers, ancdata, flags, address)
        socket.socket.sendmsg = sendmsg
    def wrap_dns(original):
        def guarded(*args, **kwargs):
            host = args[0] if args else kwargs.get("host")
            if not _loopback(host):
                raise PermissionError("EBE core forbids external DNS")
            return original(*args, **kwargs)
        return guarded
    for name in ("getaddrinfo", "gethostbyname", "gethostbyname_ex"):
        setattr(socket, name, wrap_dns(getattr(socket, name)))
    def deny_reverse(*args, **kwargs):
        raise PermissionError("EBE core forbids reverse DNS")
    socket.gethostbyaddr = deny_reverse
    socket.getnameinfo = deny_reverse


def _wrap_parser():
    original = subprocess.Popen.__init__
    signature = inspect.signature(original)

    def guarded(self, *args, **kwargs):
        bound = signature.bind(self, *args, **kwargs)
        bound.apply_defaults()
        options = bound.arguments
        argv = options["args"]
        # Validate every path BEFORE the first resolve, including optional cwd
        # and executable overrides that will subsequently be rejected anyway.
        if isinstance(argv, (list, tuple)):
            for index in (0, 2, 3):
                if len(argv) > index and isinstance(argv[index], (str, bytes, os.PathLike)):
                    validate_local_path(argv[index])
        for key in ("cwd", "executable"):
            if options[key] is not None:
                validate_local_path(options[key])
        if (not _pdf_enabled or _parser is None
                or not isinstance(argv, (list, tuple)) or len(argv) != 4
                or any(not isinstance(x, str) for x in argv)
                or str(Path(argv[0]).resolve()) != _parser or argv[1] != "-layout"
                or not argv[2] or argv[2].startswith("-")
                or options["shell"] or options["executable"] not in (None, _parser)
                or options["preexec_fn"] is not None
                or options.get("pass_fds") or options.get("startupinfo") is not None
                or options.get("creationflags", 0) != 0):
            raise PermissionError("EBE core permits only the fixed pdftotext parser")
        environment = dict(_parser_env)
        if argv[3] == "-":
            if options["cwd"] is not None:
                raise PermissionError("stdout PDF parser must not change cwd")
        else:
            # Current engine writes body.txt inside its private TemporaryDirectory.
            directory = Path(options["cwd"]).resolve() if options["cwd"] else None
            output = Path(argv[3])
            if (directory is None or not directory.name.startswith("ebe-pdf-")
                    or directory.parent != Path(validate_local_path(tempfile.gettempdir())).resolve()
                    or not directory.is_dir() or output.is_symlink()
                    or not output.is_absolute() or output.resolve() != directory / "body.txt"
                    or any(options[key] != subprocess.DEVNULL for key in ("stdin", "stdout", "stderr"))):
                raise PermissionError("PDF output must be private ebe-pdf-*/body.txt")
            environment.update(TMP=str(directory), TEMP=str(directory), TMPDIR=str(directory))
            options["cwd"] = str(directory)
        # Resolve the input to avoid option injection, and never inherit credentials,
        # PATH, loader variables or proxy settings into the trusted native parser.
        argv = [_parser, "-layout", str(Path(argv[2]).resolve()), argv[3]]
        options["args"] = argv
        options["env"] = environment
        options["close_fds"] = True
        if os.name != "nt":
            options["start_new_session"] = True  # Avoid posix_spawn fast path.
        audit_argv = subprocess.list2cmdline(argv) if os.name == "nt" else argv
        token = _permit.set((_parser, audit_argv, options["cwd"], options["env"]))
        try:
            original(*bound.args, **bound.kwargs)
        finally:
            _permit.reset(token)

    subprocess.Popen.__init__ = guarded


def install_guard(*, allow_pdf_parser: bool = False) -> None:
    """Install once, irreversibly for this process; first call fixes PDF policy.

    With allow_pdf_parser=True, pin the installed pdftotext executable and adapt
    the existing engine.extract_text call to a fixed argv and minimal env.
    Repeated identical calls are no-ops; changing policy requires a new process.
    """
    global _installed, _pdf_enabled, _parser, _parser_env
    if type(allow_pdf_parser) is not bool:
        raise ValueError("allow_pdf_parser must be a boolean")
    with _lock:
        if _installed:
            if allow_pdf_parser != _pdf_enabled:
                raise ValueError("guard policy is already installed")
            return
        if allow_pdf_parser:
            # which() performs stat/access while searching PATH, before a found
            # executable can be checked. Reject remote search roots first.
            for entry in os.get_exec_path():
                validate_local_path(entry)
            for key in ("TMP", "TEMP", "TMPDIR", "SystemRoot", "WINDIR"):
                if os.environ.get(key):
                    validate_local_path(os.environ[key])
            found = shutil.which("pdftotext")
            if found:
                resolved = Path(validate_local_path(found)).resolve()
                if resolved.name.lower() not in {"pdftotext", "pdftotext.exe"}:
                    raise ValueError("PDF parser must be exactly pdftotext or pdftotext.exe")
                _parser = str(resolved)
            _parser_env = {"SystemRoot": os.environ["SystemRoot"]} if os.name == "nt" and "SystemRoot" in os.environ else {}
        _pdf_enabled = allow_pdf_parser
        _wrap_sockets()
        _wrap_parser()
        sys.addaudithook(_audit)
        _installed = True
