"""Run irreversible guard tests in fresh Python processes; never contact WAN."""
import os
from contextlib import ExitStack
from pathlib import Path
import subprocess
import sys
import textwrap
import unittest
from unittest import mock

from ebe.isolation import validate_local_path


class LocalPathTests(unittest.TestCase):
    def test_mixed_separators_rejected_by_cli_server_and_images_before_fs(self):
        from ebe import cli, images, server
        forbidden = [r"/\example.invalid\share", r"\/example.invalid/share",
                     r"/\?\UNC\example.invalid\share", r"\/.\pipe\name"]
        with ExitStack() as stack:
            touched = [stack.enter_context(mock.patch.object(Path, name,
                side_effect=AssertionError("filesystem before validation: " + name)))
                for name in ("resolve", "stat", "lstat", "read_text", "open")]
            loaded = stack.enter_context(mock.patch.object(cli, "modules",
                side_effect=AssertionError("MCP dispatched before validation")))
            listener = stack.enter_context(mock.patch.object(server, "ThreadingHTTPServer",
                side_effect=AssertionError("server started before validation")))
            for path in forbidden:
                calls = [
                    lambda: cli.reject_network_paths({"arguments": {"metadata": [{"nested": path}]}}),
                    lambda: cli.call_tool("init_project", {"root": path}),
                    lambda: server.serve(path, 8765, "synthetic-token-" * 3),
                    lambda: server.readonly_status(path),
                    lambda: images._path(path),
                    lambda: images.insert_images("[[IMAGE:example]]\n", path),
                ]
                for index, call in enumerate(calls):
                    with self.subTest(path=path, entry=index), self.assertRaises(ValueError):
                        call()
            for operation in [*touched, loaded, listener]:
                operation.assert_not_called()

    def test_cli_commands_reject_mixed_paths_before_setup_or_file_reads(self):
        from ebe import cli
        bad = r"/\example.invalid\share"
        commands = [["serve", bad], ["insert-images", bad, "assets", "out.md"],
                    ["insert-images", "book.md", bad, "out.md"],
                    ["insert-images", "book.md", "assets", bad]]
        with mock.patch.object(cli, "core_setup", side_effect=AssertionError("setup before validation")) as setup, \
                mock.patch.object(Path, "read_text", side_effect=AssertionError("read before validation")) as read:
            for command in commands:
                with self.subTest(command=command), self.assertRaisesRegex(ValueError, "network_or_device_path_refused"):
                    cli.main(command)
            setup.assert_not_called()
            read.assert_not_called()

    def test_unc_and_device_spellings_rejected_without_filesystem_access(self):
        forbidden = [
            r"\\host\share\file", "//host/share/file", r"\/host\share", r"/\host/share",
            r"\\?\UNC\host\share", "//?/uNc/host/share", r"\\?\C:\local\file",
            r"\\.\pipe\name", "//./PhysicalDrive0", r"\\?\GLOBALROOT\Device\Mup\host\share",
            r"\??\UNC\host\share", r"\Device\Mup\host\share", r"\GLOBAL??\UNC\host\share",
            r"\GLOBALROOT\Device\Mup\host\share", "file://host/share/file", "bad\x00path",
        ]
        with mock.patch.object(Path, "resolve", side_effect=AssertionError("resolve before validation")) as resolve, \
                mock.patch.object(os, "stat", side_effect=AssertionError("stat before validation")) as stat, \
                mock.patch("builtins.open", side_effect=AssertionError("open before validation")) as opened:
            for path in forbidden:
                for value in (path, path.encode("utf-8")):
                    with self.subTest(value=value), self.assertRaisesRegex(ValueError, "local path"):
                        validate_local_path(value)
            resolve.assert_not_called()
            stat.assert_not_called()
            opened.assert_not_called()

    def test_local_paths_return_unchanged_and_pathlike_is_coerced_once(self):
        for value in ("relative/file.txt", "../source.pdf", r"C:\local\file", "C:/local/file",
                      "/tmp/local", r"\rooted\local", b"local.bin", "", "https://example.invalid/source"):
            with self.subTest(value=value):
                self.assertEqual(validate_local_path(value), value)
        pathlike = mock.Mock(spec=os.PathLike)
        pathlike.__fspath__ = mock.Mock(return_value="local.txt")
        self.assertEqual(validate_local_path(pathlike), "local.txt")
        pathlike.__fspath__.assert_called_once_with()
        pathlike.__fspath__.return_value = "//host/share"
        with self.assertRaises(ValueError):
            validate_local_path(pathlike)
        for value in (None, 3, {}, []):
            with self.subTest(value=value), self.assertRaises(TypeError):
                validate_local_path(value)


class IsolationTests(unittest.TestCase):
    def run_guard(self, code):
        result = subprocess.run([sys.executable, "-B", "-c", textwrap.dedent(code)],
                                capture_output=True, text=True, timeout=20,
                                cwd=Path(__file__).resolve().parents[1])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_guard_blocks_network_dns_and_processes(self):
        self.run_guard('''
            import os, socket, subprocess, sys
            from ebe.isolation import install_guard
            install_guard()
            install_guard()
            def denied(call):
                try: call()
                except PermissionError: return
                raise AssertionError("operation was not denied")
            with socket.socket() as s:
                denied(lambda: s.connect(("192.0.2.1", 80)))
                denied(lambda: s.connect(("example.invalid", 80)))
                denied(lambda: s.bind(("0.0.0.0", 0)))
                denied(lambda: s.bind(("", 0)))
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                denied(lambda: s.sendto(b"x", ("192.0.2.1", 53)))
            denied(lambda: socket.getaddrinfo("example.invalid", 80))
            denied(lambda: socket.gethostbyname("example.invalid"))
            denied(lambda: socket.gethostbyaddr("192.0.2.1"))
            denied(lambda: socket.getnameinfo(("127.0.0.1", 80), 0))
            denied(lambda: subprocess.run([sys.executable, "-c", "pass"]))
            denied(lambda: os.system("echo forbidden"))
            if hasattr(os, "posix_spawn"):
                denied(lambda: os.posix_spawn(sys.executable, [sys.executable, "-c", "pass"], {}))
            denied(lambda: sys.audit("subprocess.Popen", sys.executable, [], None, {}))
            denied(lambda: sys.audit("os.exec", sys.executable, [], {}))
            try: install_guard(allow_pdf_parser=True)
            except ValueError: pass
            else: raise AssertionError("policy weakened")
        ''')

    def test_real_loopback_connect_and_bind(self):
        self.run_guard('''
            import socket
            from ebe.isolation import install_guard
            install_guard()
            for family, host in [(socket.AF_INET, "127.0.0.1"), (socket.AF_INET6, "::1")]:
                try: server = socket.socket(family)
                except OSError:
                    if family == socket.AF_INET6: continue
                    raise
                with server:
                    try: server.bind((host, 0))
                    except OSError:
                        if family == socket.AF_INET6: continue
                        raise
                    server.listen(1)
                    with socket.socket(family) as client:
                        client.settimeout(2)
                        client.connect(server.getsockname())
                        peer, _ = server.accept()
                        peer.close()
        ''')

    def test_pdf_adapter_fixed_argv_minimal_env_and_no_shell(self):
        self.run_guard('''
            import os, subprocess, sys
            from pathlib import Path
            from unittest.mock import patch
            from ebe import isolation
            from ebe.core import modules
            engine, _, _ = modules()
            parser = str(Path("pdftotext.exe" if os.name == "nt" else "pdftotext").resolve())
            observed = []
            original = subprocess.Popen.__init__
            import functools
            @functools.wraps(original)
            def fake(self, *args, **kwargs):
                bound = isolation.inspect.signature(original).bind(self, *args, **kwargs)
                bound.apply_defaults()
                kw = bound.arguments
                argv = kw["args"]
                audit_argv = subprocess.list2cmdline(argv) if os.name == "nt" else argv
                sys.audit("subprocess.Popen", parser, audit_argv, kw["cwd"], kw["env"])
                observed.append(kw)
                if argv[3] != "-":
                    Path(argv[3]).write_bytes(b"Parsed PDF")
                self.returncode = 0
                self.args = argv
                self.stdout = self.stderr = self.stdin = None
                self._child_created = False
            with patch.object(isolation.shutil, "which", return_value=parser):
                subprocess.Popen.__init__ = fake
                isolation.install_guard(allow_pdf_parser=True)
                with patch.object(subprocess.Popen, "communicate", return_value=(b"Parsed PDF", b"")), patch.object(subprocess.Popen, "wait", return_value=0), patch.object(subprocess.Popen, "poll", return_value=0):
                    text, method = engine.extract_text(Path("input.pdf"))
                    assert text == "Parsed PDF" and method == "pdftotext"
            kw = observed[0]
            assert kw["args"][:3] == [parser, "-layout", str(Path("input.pdf").resolve())]
            assert Path(kw["args"][3]).name == "body.txt"
            assert set(kw["env"]) <= {"SystemRoot", "TMP", "TEMP", "TMPDIR"}
            assert kw["shell"] is False
            for argv, extra in [([parser, "-v"], {}), ([parser, "-layout", "-evil", "-"], {}),
                                ([parser, "-layout", "input.pdf", "-"], {"shell": True}),
                                ([parser, "-layout", "input.pdf", "-"], {"executable": sys.executable}),
                                ([parser + ".evil", "-layout", "input.pdf", "-"], {})]:
                try: subprocess.Popen(argv, **extra)
                except PermissionError: pass
                else: raise AssertionError("parser exception too broad")
            try: sys.audit("subprocess.Popen", parser, kw["args"], None, kw["env"])
            except PermissionError: pass
            else: raise AssertionError("parser permit leaked")
        ''')

    def test_install_adds_one_hook(self):
        self.run_guard('''
            from unittest.mock import patch
            from ebe import isolation
            with patch.object(isolation.sys, "addaudithook") as hook:
                isolation.install_guard()
                isolation.install_guard()
                assert hook.call_count == 1
        ''')

    def test_filesystem_audit_blocks_paths_without_performing_remote_io(self):
        self.run_guard(r'''
            import os, sys, tempfile
            from pathlib import Path
            from unittest.mock import Mock, patch
            from ebe.isolation import install_guard
            install_guard()
            # Synthetic audit events exercise the installed hook, never a UNC
            # OS call. A mocked backend must remain untouched after rejection.
            backend = Mock(side_effect=AssertionError("filesystem reached"))
            paths = [r"\\host\share\file", "//host/share", r"\\?\UNC\host\share", r"\\.\pipe\name"]
            for path in paths:
                for value in (path, path.encode()):
                    events = [
                        ("open", (value, "r", 0)), ("os.listdir", (value,)),
                        ("os.scandir", (value,)), ("os.chdir", (value,)),
                        ("os.mkdir", (value, 511, -1)), ("os.remove", (value, -1)),
                        ("os.rename", ("local", value, -1, -1)),
                        ("os.rename", (value, "local", -1, -1)),
                        ("os.symlink", (value, "local", -1)),
                        ("shutil.copyfile", ("local", value)),
                        ("sqlite3.connect", (value,)),
                    ]
                    for event, args in events:
                        try:
                            sys.audit(event, *args)
                            backend()
                        except ValueError:
                            pass
                        else:
                            raise AssertionError(event + " not denied")
            backend.assert_not_called()
            # Ordinary local operations and integer file descriptors still work.
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "local.txt"
                path.write_text("local", encoding="utf-8")
                assert path.read_text(encoding="utf-8") == "local"
                assert "local.txt" in os.listdir(directory)
                with os.scandir(directory) as entries:
                    assert next(entries).name == "local.txt"
                with path.open("rb") as handle:
                    with open(handle.fileno(), "rb", closefd=False) as same:
                        assert same.read() == b"local"
        ''')

    def test_pdf_rejects_unc_before_resolve_or_stat(self):
        self.run_guard(r'''
            import os, subprocess
            from pathlib import Path
            from unittest.mock import patch
            from ebe import isolation
            parser = str(Path("pdftotext.exe" if os.name == "nt" else "pdftotext").resolve())
            with patch.object(isolation.shutil, "which", return_value=parser):
                isolation.install_guard(allow_pdf_parser=True)
            bad = "//host/share/file.pdf"
            cases = [([bad, "-layout", "input.pdf", "-"], {}),
                     ([parser, "-layout", bad, "-"], {}),
                     ([parser, "-layout", "input.pdf", bad], {}),
                     ([parser, "-layout", "input.pdf", "-"], {"cwd": bad}),
                     ([parser, "-layout", "input.pdf", "-"], {"executable": bad})]
            with patch.object(Path, "resolve", side_effect=AssertionError("resolve before validation")) as resolve, patch.object(os, "stat", side_effect=AssertionError("stat before validation")) as stat:
                for argv, kwargs in cases:
                    try: subprocess.Popen(argv, **kwargs)
                    except ValueError: pass
                    else: raise AssertionError("UNC accepted")
                resolve.assert_not_called()
                stat.assert_not_called()
        ''')

    def test_readonly_server_under_guard_with_local_sqlite_uri(self):
        self.run_guard(r'''
            import json, sqlite3, tempfile
            from contextlib import closing
            from pathlib import Path
            from ebe.isolation import install_guard, validate_local_path
            from ebe.server import readonly_status
            with tempfile.TemporaryDirectory() as directory:
                project = Path(directory) / "project with spaces"
                project.mkdir()
                (project / "project.json").write_text(json.dumps({"project_id": "synthetic"}), encoding="utf-8")
                db = project / "state.sqlite3"
                with closing(sqlite3.connect(db)) as conn:
                    conn.executescript("CREATE TABLE sources(status TEXT); INSERT INTO sources VALUES ('ingested'); CREATE TABLE artifacts(artifact_type TEXT); INSERT INTO artifacts VALUES ('chapter');")
                before = db.read_bytes()
                install_guard()
                assert readonly_status(project) == {"project_id": "synthetic", "sources": {"ingested": 1}, "artifacts": {"chapter": 1}}
                uri = db.as_uri() + "?mode=ro"
                with closing(sqlite3.connect(uri, uri=True)) as conn:
                    assert conn.execute("SELECT count(*) FROM sources").fetchone() == (1,)
                    try: conn.execute("INSERT INTO sources VALUES ('modified')")
                    except sqlite3.OperationalError as exc: assert "readonly" in str(exc).lower()
                    else: raise AssertionError("read-only URI allowed write")
                assert db.read_bytes() == before
                assert set(p.name for p in project.iterdir()) == {"project.json", "state.sqlite3"}
                try: validate_local_path(uri)
                except ValueError: pass
                else: raise AssertionError("general path policy was relaxed")
        ''')

    def test_sqlite_uri_audit_rejects_remote_encoded_paths_and_options(self):
        self.run_guard(r'''
            import sys
            from unittest.mock import Mock, patch
            from ebe.isolation import install_guard
            install_guard()
            backend = Mock(side_effect=AssertionError("SQLite filesystem reached"))
            forbidden = [
                "file://remote/share/db?mode=ro", "file://localhost/C:/db?mode=ro",
                "file:////remote/share/db?mode=ro", "file:%2f%2fremote/share/db?mode=ro",
                "file:%5c%5cremote%5cshare%5cdb?mode=ro",
                "file:/%5cremote/share/db?mode=ro",
                "file:%5c%5c%3f%5cUNC%5cremote%5cshare?mode=ro",
                "file:%5c%5c.%5cpipe%5cname?mode=ro",
                "file:%5c%3f%3f%5cUNC%5cremote%5cshare?mode=ro",
                "file:/tmp/db%00ignored?mode=ro", "file:/tmp/%zz?mode=ro",
                "file:/tmp/db?mode=rw", "file:/tmp/db?mode=rwc", "file:/tmp/db",
                "file:/tmp/db?mode=ro&vfs=other", "file:/tmp/db?mode=ro&mode=rw",
                "file:/tmp/db?mode=ro#fragment", "file:/tmp/db?mode=ro#",
                "file:/tmp/db?mode=ro\n", "file:?mode=ro",
            ]
            # Emit only audit events; never ask SQLite/the OS to open bad paths.
            for uri in forbidden:
                for value in (uri, uri.encode()):
                    try:
                        sys.audit("sqlite3.connect", value)
                        backend()
                    except ValueError: pass
                    else: raise AssertionError("URI accepted")
            backend.assert_not_called()
            for event, args in [("open", ("file:///tmp/db?mode=ro", "r", 0)),
                                ("os.listdir", ("file:///tmp/db?mode=ro",))]:
                try: sys.audit(event, *args)
                except ValueError: pass
                else: raise AssertionError("URI allowed outside SQLite")
        ''')

    def test_latest_core_setup_real_project_readonly_status(self):
        self.run_guard(r'''
            import sqlite3, tempfile
            from contextlib import closing
            from pathlib import Path
            from ebe.cli import core_setup
            from ebe.server import readonly_status
            from ebe.isolation import install_guard
            # Use the actual core boot sequence, including PDF policy and
            # readiness installation, BEFORE creating the real engine project.
            engine, _, _ = core_setup()
            install_guard(allow_pdf_parser=True)
            with tempfile.TemporaryDirectory(prefix="ebe core readonly ") as directory:
                project = Path(engine.init_project(directory, "readonly-freeze",
                    "Synthetic freeze verification", allow_single_copy=True)["project"])
                source = project / "input" / "synthetic.txt"
                source.write_text("Synthetic local evidence for status verification.", encoding="utf-8")
                engine.ingest_file(project, source)
                engine.save_artifact(project, "topic-analysis", "Synthetic analysis")
                db = project / "state.sqlite3"
                before = db.read_bytes()
                metadata_before = (project / "project.json").read_bytes()
                expected_id = engine.read_project_metadata(project)["project_id"]
                assert readonly_status(project) == {
                    "project_id": expected_id, "sources": {"ingested": 1},
                    "artifacts": {"topic-analysis": 1}}
                with closing(sqlite3.connect(db.as_uri() + "?mode=ro", uri=True)) as conn:
                    assert conn.execute("SELECT COUNT(*) FROM sources").fetchone() == (1,)
                    try: conn.execute("CREATE TABLE forbidden_write(id INTEGER)")
                    except sqlite3.OperationalError as exc: assert "readonly" in str(exc).lower()
                    else: raise AssertionError("read-only connection accepted DDL")
                assert db.read_bytes() == before
                assert (project / "project.json").read_bytes() == metadata_before
        ''')

    def test_pdf_discovery_rejects_remote_search_path_before_which(self):
        self.run_guard(r'''
            from unittest.mock import patch
            from ebe import isolation
            with patch.object(isolation.os, "get_exec_path", return_value=["//host/bin"]), patch.object(isolation.shutil, "which", side_effect=AssertionError("which touched remote PATH")) as which:
                try: isolation.install_guard(allow_pdf_parser=True)
                except ValueError: pass
                else: raise AssertionError("remote PATH accepted")
                which.assert_not_called()
        ''')

    def test_local_model_with_guard_installed(self):
        self.run_guard('''
            from tests.test_local_model import server
            from ebe.isolation import install_guard
            from ebe.local_model import generate
            install_guard()
            with server() as (url, requests):
                assert generate({"kind": "chapter"}, url, "mock")["content"]
                assert len(requests) == 1
        ''')

    def test_pipeline_worker_rejected_before_project_access(self):
        from ebe.core import modules
        _, _, pipeline = modules()
        for command in ([], ["arbitrary-command"]):
            with self.assertRaisesRegex(ValueError, "fixed local model adapter"):
                pipeline.run_topic_pipeline("nonexistent", worker=command)


if __name__ == "__main__":
    unittest.main()
