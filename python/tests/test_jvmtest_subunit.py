#
#  subunit: extensions to python unittest to get test results from subprocesses.
#  Copyright (C) 2026  Jelmer Vernooij <jelmer@samba.org>
#
#  Licensed under either the Apache License, Version 2.0 or the BSD 3-clause
#  license at the users choice. A copy of both licenses are available in the
#  project source as Apache-2.0 and BSD. You may not use this file except in
#  compliance with one of these two licences.
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under these licenses is distributed on an "AS IS" BASIS, WITHOUT
#  WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.  See the
#  license you chose for the specific language governing permissions and
#  limitations under that license.
#

"""Tests for the jvmtest-subunit orchestrator script."""

import io
import os
import shutil
import sys
import tempfile
from unittest import mock

from testtools import TestCase

from subunit.filter_scripts import jvmtest_subunit


class TestDetectTool(TestCase):
    """Pure-function tests for the Maven/Gradle detection."""

    def setUp(self):
        super().setUp()
        self.tmp = tempfile.mkdtemp(prefix="jvmtest-subunit-detect-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_pom_xml_detects_maven(self):
        with open(os.path.join(self.tmp, "pom.xml"), "w") as fh:
            fh.write("<project/>")
        self.assertEqual("maven", jvmtest_subunit.detect_tool(self.tmp))

    def test_build_gradle_detects_gradle(self):
        with open(os.path.join(self.tmp, "build.gradle"), "w") as fh:
            fh.write("// gradle\n")
        self.assertEqual("gradle", jvmtest_subunit.detect_tool(self.tmp))

    def test_build_gradle_kts_detects_gradle(self):
        with open(os.path.join(self.tmp, "build.gradle.kts"), "w") as fh:
            fh.write("// gradle kotlin\n")
        self.assertEqual("gradle", jvmtest_subunit.detect_tool(self.tmp))

    def test_settings_gradle_alone_detects_gradle(self):
        # Multi-module Gradle builds often have only `settings.gradle` at
        # the root (per-module build.gradle is one level down).
        with open(os.path.join(self.tmp, "settings.gradle"), "w") as fh:
            fh.write("rootProject.name = 'demo'\n")
        self.assertEqual("gradle", jvmtest_subunit.detect_tool(self.tmp))

    def test_no_marker_returns_none(self):
        self.assertIsNone(jvmtest_subunit.detect_tool(self.tmp))

    def test_both_markers_raises(self):
        # A polyglot project with both Maven and Gradle would be ambiguous;
        # require the user to pick explicitly via --tool.
        with open(os.path.join(self.tmp, "pom.xml"), "w") as fh:
            fh.write("<project/>")
        with open(os.path.join(self.tmp, "build.gradle"), "w") as fh:
            fh.write("// gradle\n")
        self.assertRaises(RuntimeError, jvmtest_subunit.detect_tool, self.tmp)


class TestStripDoubleDash(TestCase):
    def test_strips_leading_double_dash(self):
        self.assertEqual(
            ["-Dx=1", "clean"],
            jvmtest_subunit._strip_double_dash(["--", "-Dx=1", "clean"]),
        )

    def test_no_double_dash_passthrough(self):
        self.assertEqual(["-Dx=1"], jvmtest_subunit._strip_double_dash(["-Dx=1"]))

    def test_empty(self):
        self.assertEqual([], jvmtest_subunit._strip_double_dash([]))


class TestRun(TestCase):
    """Orchestration tests using mocked Popen and watch_junit_xml."""

    def setUp(self):
        super().setUp()
        self.tmp = tempfile.mkdtemp(prefix="jvmtest-subunit-run-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.reports_dir = os.path.join(self.tmp, "target", "surefire-reports")
        self.stdout = io.BytesIO()
        self.stderr = io.StringIO()

    def _stub_popen(self, build_pid=4242, build_rc=0):
        """Build a fake Popen that returns the given pid/exit code."""
        proc = mock.Mock()
        proc.pid = build_pid
        proc.wait.return_value = build_rc
        return proc

    def test_invokes_default_argv_for_maven(self):
        with mock.patch("subprocess.Popen") as popen, mock.patch(
            "subunit.filter_scripts.jvmtest_subunit.watch_junit_xml",
            return_value=0,
        ) as watch:
            popen.return_value = self._stub_popen()
            rc = jvmtest_subunit.run(
                tool="maven",
                reports_dir=self.reports_dir,
                build_args=[],
                output_stream=self.stdout,
                stderr_stream=self.stderr,
            )
        self.assertEqual(0, rc)
        # First positional arg to Popen is the argv list.
        argv = popen.call_args[0][0]
        self.assertEqual(["mvn", "test", "-q"], argv)
        # Watcher gets the build's pid so it knows when to exit.
        self.assertEqual(4242, watch.call_args.kwargs["until_pid"])
        # Reports dir is created up front so the watcher's first sweep
        # doesn't error out before Maven gets around to creating it.
        self.assertTrue(os.path.isdir(self.reports_dir))

    def test_invokes_default_argv_for_gradle(self):
        with mock.patch("subprocess.Popen") as popen, mock.patch(
            "subunit.filter_scripts.jvmtest_subunit.watch_junit_xml",
            return_value=0,
        ):
            popen.return_value = self._stub_popen()
            jvmtest_subunit.run(
                tool="gradle",
                reports_dir=self.reports_dir,
                build_args=[],
                output_stream=self.stdout,
                stderr_stream=self.stderr,
            )
        argv = popen.call_args[0][0]
        self.assertEqual(["gradle", "test", "-q"], argv)

    def test_extra_build_args_are_appended(self):
        with mock.patch("subprocess.Popen") as popen, mock.patch(
            "subunit.filter_scripts.jvmtest_subunit.watch_junit_xml",
            return_value=0,
        ):
            popen.return_value = self._stub_popen()
            jvmtest_subunit.run(
                tool="maven",
                reports_dir=self.reports_dir,
                build_args=["-Dskip.it=true", "-pl", "module-a"],
                output_stream=self.stdout,
                stderr_stream=self.stderr,
            )
        argv = popen.call_args[0][0]
        self.assertEqual(
            ["mvn", "test", "-q", "-Dskip.it=true", "-pl", "module-a"], argv
        )

    def test_build_stdout_and_stderr_forwarded_to_our_stderr(self):
        # Build-tool output must not pollute our stdout (which is the
        # subunit byte stream); both go to our stderr instead.
        with mock.patch("subprocess.Popen") as popen, mock.patch(
            "subunit.filter_scripts.jvmtest_subunit.watch_junit_xml",
            return_value=0,
        ):
            popen.return_value = self._stub_popen()
            jvmtest_subunit.run(
                tool="maven",
                reports_dir=self.reports_dir,
                build_args=[],
                output_stream=self.stdout,
                stderr_stream=self.stderr,
            )
        kwargs = popen.call_args.kwargs
        self.assertIs(self.stderr, kwargs["stdout"])
        self.assertIs(self.stderr, kwargs["stderr"])

    def test_test_failure_propagates_watch_exit_code(self):
        with mock.patch("subprocess.Popen") as popen, mock.patch(
            "subunit.filter_scripts.jvmtest_subunit.watch_junit_xml",
            return_value=1,
        ):
            popen.return_value = self._stub_popen(build_rc=1)
            # Drop a fake report so the crash-detection branch doesn't
            # override watch_rc.
            os.makedirs(self.reports_dir, exist_ok=True)
            with open(os.path.join(self.reports_dir, "TEST-A.xml"), "w") as fh:
                fh.write("<testsuite/>")
            rc = jvmtest_subunit.run(
                tool="maven",
                reports_dir=self.reports_dir,
                build_args=[],
                output_stream=self.stdout,
                stderr_stream=self.stderr,
            )
        self.assertEqual(1, rc)

    def test_build_crash_with_no_reports_returns_build_exit(self):
        # `mvn` died (SIGKILL = 137) before any test class finished, so
        # the watcher saw zero reports. A clean "0 failures" return
        # would be misleading — surface the build's exit code instead.
        with mock.patch("subprocess.Popen") as popen, mock.patch(
            "subunit.filter_scripts.jvmtest_subunit.watch_junit_xml",
            return_value=0,
        ):
            popen.return_value = self._stub_popen(build_rc=137)
            # Reports dir is created (run() does that) but stays empty.
            rc = jvmtest_subunit.run(
                tool="maven",
                reports_dir=self.reports_dir,
                build_args=[],
                output_stream=self.stdout,
                stderr_stream=self.stderr,
            )
        self.assertEqual(137, rc)
        self.assertIn("without producing any test reports", self.stderr.getvalue())

    def test_build_crash_with_some_reports_keeps_watch_exit(self):
        # If at least some tests ran, the recorded results are real and
        # take precedence over the build crash code.
        with mock.patch("subprocess.Popen") as popen, mock.patch(
            "subunit.filter_scripts.jvmtest_subunit.watch_junit_xml",
            return_value=0,
        ):
            popen.return_value = self._stub_popen(build_rc=137)
            os.makedirs(self.reports_dir, exist_ok=True)
            with open(os.path.join(self.reports_dir, "TEST-A.xml"), "w") as fh:
                fh.write("<testsuite/>")
            rc = jvmtest_subunit.run(
                tool="maven",
                reports_dir=self.reports_dir,
                build_args=[],
                output_stream=self.stdout,
                stderr_stream=self.stderr,
            )
        self.assertEqual(0, rc)

    def test_keyboard_interrupt_terminates_build_and_propagates(self):
        with mock.patch("subprocess.Popen") as popen, mock.patch(
            "subunit.filter_scripts.jvmtest_subunit.watch_junit_xml",
            side_effect=KeyboardInterrupt,
        ):
            proc = self._stub_popen()
            popen.return_value = proc
            self.assertRaises(
                KeyboardInterrupt,
                jvmtest_subunit.run,
                tool="maven",
                reports_dir=self.reports_dir,
                build_args=[],
                output_stream=self.stdout,
                stderr_stream=self.stderr,
            )
        proc.terminate.assert_called_once()
        # We always wait() to reap the build process — the build's
        # zombie state would otherwise linger past our exit.
        self.assertTrue(proc.wait.called)


class TestMainCli(TestCase):
    """Tests for the CLI dispatcher."""

    def setUp(self):
        super().setUp()
        self.tmp = tempfile.mkdtemp(prefix="jvmtest-subunit-main-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        # Run main() with cwd set to a controlled directory so detection
        # is deterministic.
        self._old_cwd = os.getcwd()
        os.chdir(self.tmp)
        self.addCleanup(os.chdir, self._old_cwd)

    def test_no_marker_exits_with_diagnostic(self):
        with mock.patch("sys.stderr", new=io.StringIO()) as stderr:
            rc = jvmtest_subunit.main([])
        self.assertEqual(2, rc)
        self.assertIn("no Maven", stderr.getvalue())

    def test_explicit_tool_overrides_detection(self):
        # No project markers, but --tool gradle should still work
        # (run() is mocked so we don't actually invoke gradle).
        with mock.patch(
            "subunit.filter_scripts.jvmtest_subunit.run", return_value=0
        ) as run:
            rc = jvmtest_subunit.main(["--tool", "gradle"])
        self.assertEqual(0, rc)
        self.assertEqual("gradle", run.call_args.kwargs["tool"])
        self.assertEqual(
            "build/test-results/test", run.call_args.kwargs["reports_dir"]
        )

    def test_reports_dir_override_passed_through(self):
        with mock.patch(
            "subunit.filter_scripts.jvmtest_subunit.run", return_value=0
        ) as run:
            jvmtest_subunit.main(["--tool", "maven", "--reports-dir", "/custom/path"])
        self.assertEqual("/custom/path", run.call_args.kwargs["reports_dir"])

    def test_extra_build_args_after_double_dash(self):
        with mock.patch(
            "subunit.filter_scripts.jvmtest_subunit.run", return_value=0
        ) as run:
            jvmtest_subunit.main(
                ["--tool", "maven", "--", "-Dskip.it=true", "clean"]
            )
        self.assertEqual(
            ["-Dskip.it=true", "clean"], run.call_args.kwargs["build_args"]
        )

    def test_ambiguous_project_raises_diagnostic(self):
        with open(os.path.join(self.tmp, "pom.xml"), "w") as fh:
            fh.write("<project/>")
        with open(os.path.join(self.tmp, "build.gradle"), "w") as fh:
            fh.write("// gradle\n")
        with mock.patch("sys.stderr", new=io.StringIO()) as stderr:
            rc = jvmtest_subunit.main([])
        self.assertEqual(2, rc)
        self.assertIn("both Maven", stderr.getvalue())


def _touch_test_class(root, package, class_name, ext=".java"):
    """Materialise a (mostly-empty) test source file at the right path.

    The discovery walker only looks at the path + filename — it doesn't
    parse Java/Kotlin source — so we don't need real `@Test` methods.
    """
    pkg_dir = os.path.join(root, package.replace(".", os.sep))
    os.makedirs(pkg_dir, exist_ok=True)
    path = os.path.join(pkg_dir, class_name + ext)
    with open(path, "w") as fh:
        fh.write("// stub for {}.{}\n".format(package, class_name))
    return path


class TestDiscoverTestClasses(TestCase):
    """Walk-based discovery of test classes under src/test/{java,kotlin}."""

    def setUp(self):
        super().setUp()
        self.tmp = tempfile.mkdtemp(prefix="jvmtest-subunit-discover-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_finds_test_classes_under_java_root(self):
        java_root = os.path.join(self.tmp, "src/test/java")
        _touch_test_class(java_root, "com.example", "FooTest")
        _touch_test_class(java_root, "com.example.sub", "BarTests")
        _touch_test_class(java_root, "com.example", "BazIT")
        result = jvmtest_subunit.discover_test_classes(self.tmp)
        self.assertEqual(
            [
                "com.example.BazIT",
                "com.example.FooTest",
                "com.example.sub.BarTests",
            ],
            result,
        )

    def test_finds_kotlin_test_classes(self):
        kt_root = os.path.join(self.tmp, "src/test/kotlin")
        _touch_test_class(kt_root, "com.example", "MyTest", ext=".kt")
        result = jvmtest_subunit.discover_test_classes(self.tmp)
        self.assertEqual(["com.example.MyTest"], result)

    def test_skips_non_test_classes(self):
        java_root = os.path.join(self.tmp, "src/test/java")
        _touch_test_class(java_root, "com.example", "Helper")  # no Test suffix
        _touch_test_class(java_root, "com.example", "FooTest")
        result = jvmtest_subunit.discover_test_classes(self.tmp)
        self.assertEqual(["com.example.FooTest"], result)

    def test_skips_non_source_files(self):
        java_root = os.path.join(self.tmp, "src/test/java/com/example")
        os.makedirs(java_root, exist_ok=True)
        with open(os.path.join(java_root, "FooTest.txt"), "w") as fh:
            fh.write("not source")
        result = jvmtest_subunit.discover_test_classes(self.tmp)
        self.assertEqual([], result)

    def test_no_source_dirs_returns_empty(self):
        # Project with no src/test/java or src/test/kotlin → empty list
        # (rather than crashing). The caller decides whether that's an
        # error (--list with no tests is suspicious; --id-file is fine).
        self.assertEqual([], jvmtest_subunit.discover_test_classes(self.tmp))

    def test_default_package_classes_are_included(self):
        # Test classes at the source root (no package) should still be
        # discoverable — Java permits them, even if rare in practice.
        java_root = os.path.join(self.tmp, "src/test/java")
        os.makedirs(java_root, exist_ok=True)
        with open(os.path.join(java_root, "BareTest.java"), "w") as fh:
            fh.write("// no package\n")
        result = jvmtest_subunit.discover_test_classes(self.tmp)
        self.assertEqual(["BareTest"], result)


class TestParseTestId(TestCase):
    def test_bare_class(self):
        self.assertEqual(
            ("com.example.FooTest", None),
            jvmtest_subunit.parse_test_id("com.example.FooTest"),
        )

    def test_class_and_method(self):
        self.assertEqual(
            ("com.example.FooTest", "testBar"),
            jvmtest_subunit.parse_test_id("com.example.FooTest::testBar"),
        )

    def test_strips_whitespace(self):
        self.assertEqual(
            ("FooTest", "testBar"),
            jvmtest_subunit.parse_test_id("  FooTest::testBar\n"),
        )

    def test_empty_returns_none(self):
        self.assertIsNone(jvmtest_subunit.parse_test_id(""))
        self.assertIsNone(jvmtest_subunit.parse_test_id("   \n"))

    def test_malformed_returns_none(self):
        self.assertIsNone(jvmtest_subunit.parse_test_id("::testBar"))
        self.assertIsNone(jvmtest_subunit.parse_test_id("FooTest::"))


class TestGroupIds(TestCase):
    def test_groups_methods_per_class(self):
        groups = jvmtest_subunit.group_ids(
            [
                "com.example.A::m1\n",
                "com.example.B::m1\n",
                "com.example.A::m2\n",
            ]
        )
        self.assertEqual(
            {"com.example.A": ["m1", "m2"], "com.example.B": ["m1"]}, groups
        )

    def test_bare_class_subsumes_method_list(self):
        # If the user asks for the whole class AND specific methods,
        # the whole-class request wins — running everything is a
        # superset, and Maven/Gradle don't have a way to express
        # "everything except these methods" inline.
        groups = jvmtest_subunit.group_ids(
            ["com.example.A::m1", "com.example.A"]
        )
        self.assertEqual({"com.example.A": []}, groups)

    def test_bare_class_subsumes_in_either_order(self):
        groups = jvmtest_subunit.group_ids(
            ["com.example.A", "com.example.A::m1"]
        )
        self.assertEqual({"com.example.A": []}, groups)

    def test_blank_lines_dropped(self):
        groups = jvmtest_subunit.group_ids(["", "com.example.A::m1", "  "])
        self.assertEqual({"com.example.A": ["m1"]}, groups)

    def test_malformed_warned_and_skipped(self):
        with mock.patch("sys.stderr", new=io.StringIO()) as stderr:
            groups = jvmtest_subunit.group_ids(
                ["::m1", "com.example.A::m1"]
            )
        self.assertEqual({"com.example.A": ["m1"]}, groups)
        self.assertIn("malformed test ID", stderr.getvalue())


class TestSelectionArgs(TestCase):
    """Translating grouped IDs into per-tool selection flags."""

    def test_maven_class_only(self):
        self.assertEqual(
            ["-Dtest=com.example.A,com.example.B"],
            jvmtest_subunit.build_selection_args(
                "maven", {"com.example.A": [], "com.example.B": []}
            ),
        )

    def test_maven_class_with_methods(self):
        # Methods within a class are joined with `+`; classes are
        # joined with `,`. Sort everything for stable output.
        self.assertEqual(
            ["-Dtest=com.example.A,com.example.B#mA+mB"],
            jvmtest_subunit.build_selection_args(
                "maven",
                {"com.example.A": [], "com.example.B": ["mB", "mA"]},
            ),
        )

    def test_maven_dedupes_methods(self):
        self.assertEqual(
            ["-Dtest=A#m"],
            jvmtest_subunit.build_selection_args("maven", {"A": ["m", "m"]}),
        )

    def test_gradle_class_only(self):
        self.assertEqual(
            ["--tests", "com.example.A", "--tests", "com.example.B"],
            jvmtest_subunit.build_selection_args(
                "gradle", {"com.example.A": [], "com.example.B": []}
            ),
        )

    def test_gradle_class_with_methods(self):
        # One --tests per class+method pair so each pattern is precise.
        self.assertEqual(
            ["--tests", "com.example.A.m1", "--tests", "com.example.A.m2"],
            jvmtest_subunit.build_selection_args(
                "gradle", {"com.example.A": ["m2", "m1"]}
            ),
        )

    def test_empty_groups_returns_empty(self):
        # No selection means "everything" (the build tool's default).
        # Returning [] lets the caller fall through to running the
        # whole suite.
        self.assertEqual(
            [], jvmtest_subunit.build_selection_args("maven", {})
        )


class TestListMode(TestCase):
    """End-to-end test of `--list`: walk source dirs, emit subunit."""

    def setUp(self):
        super().setUp()
        self.tmp = tempfile.mkdtemp(prefix="jvmtest-subunit-list-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self._old_cwd = os.getcwd()
        os.chdir(self.tmp)
        self.addCleanup(os.chdir, self._old_cwd)

    def _decode(self, raw):
        from testtools.testresult.doubles import StreamResult

        import subunit

        sink = StreamResult()
        subunit.ByteStreamToStreamResult(io.BytesIO(raw)).run(sink)
        return [(e[1], e[2]) for e in sink._events if e[0] == "status"]

    def test_main_list_emits_exists_events(self):
        java_root = os.path.join(self.tmp, "src/test/java")
        _touch_test_class(java_root, "com.example", "FooTest")
        _touch_test_class(java_root, "com.example.sub", "BarTests")

        out = io.BytesIO()
        with mock.patch.object(sys, "stdout") as stdout_mock:
            stdout_mock.buffer = out
            rc = jvmtest_subunit.main(["--list"])
        self.assertEqual(0, rc)
        self.assertEqual(
            [
                ("com.example.FooTest", "exists"),
                ("com.example.sub.BarTests", "exists"),
            ],
            self._decode(out.getvalue()),
        )

    def test_main_list_with_no_tests_returns_zero_no_output(self):
        # A project with no test sources is a valid (if unusual) state;
        # exit cleanly so `inq list-tests` reports zero rather than
        # treating it as a failure.
        out = io.BytesIO()
        with mock.patch.object(sys, "stdout") as stdout_mock:
            stdout_mock.buffer = out
            rc = jvmtest_subunit.main(["--list"])
        self.assertEqual(0, rc)
        self.assertEqual(b"", out.getvalue())


class TestIdFileMode(TestCase):
    """End-to-end tests of `--id-file`: read IDs, run with selection."""

    def setUp(self):
        super().setUp()
        self.tmp = tempfile.mkdtemp(prefix="jvmtest-subunit-idfile-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self._old_cwd = os.getcwd()
        os.chdir(self.tmp)
        self.addCleanup(os.chdir, self._old_cwd)
        # Mark the project as Maven so the auto-detect picks the right
        # selection vocabulary.
        with open(os.path.join(self.tmp, "pom.xml"), "w") as fh:
            fh.write("<project/>")

    def _write_id_file(self, *ids):
        path = os.path.join(self.tmp, "ids.txt")
        with open(path, "w") as fh:
            fh.write("\n".join(ids) + "\n")
        return path

    def test_main_id_file_passes_maven_selection(self):
        id_path = self._write_id_file(
            "com.example.A::m1",
            "com.example.B",
            "com.example.A::m2",
        )
        with mock.patch(
            "subunit.filter_scripts.jvmtest_subunit.run", return_value=0
        ) as run:
            rc = jvmtest_subunit.main(["--id-file", id_path])
        self.assertEqual(0, rc)
        # The selection flag is prepended to build_args.
        build_args = run.call_args.kwargs["build_args"]
        self.assertEqual(
            ["-Dtest=com.example.A#m1+m2,com.example.B"], build_args
        )

    def test_main_id_file_passes_gradle_selection(self):
        # Switch the project marker to Gradle.
        os.unlink(os.path.join(self.tmp, "pom.xml"))
        with open(os.path.join(self.tmp, "build.gradle"), "w") as fh:
            fh.write("// gradle\n")
        id_path = self._write_id_file("com.example.A::m1")
        with mock.patch(
            "subunit.filter_scripts.jvmtest_subunit.run", return_value=0
        ) as run:
            rc = jvmtest_subunit.main(["--id-file", id_path])
        self.assertEqual(0, rc)
        self.assertEqual(
            ["--tests", "com.example.A.m1"],
            run.call_args.kwargs["build_args"],
        )

    def test_main_id_file_combines_with_extra_build_args(self):
        # Extra `-- foo` build args go *after* the selection flag so
        # the user's overrides take precedence in case of conflict.
        id_path = self._write_id_file("com.example.A")
        with mock.patch(
            "subunit.filter_scripts.jvmtest_subunit.run", return_value=0
        ) as run:
            jvmtest_subunit.main(
                ["--id-file", id_path, "--", "-Dskip.it=true"]
            )
        self.assertEqual(
            ["-Dtest=com.example.A", "-Dskip.it=true"],
            run.call_args.kwargs["build_args"],
        )

    def test_main_id_file_empty_refuses_to_run(self):
        # An empty id-file would otherwise mean "no selection" and we'd
        # run the whole suite — which is the opposite of the user's
        # intent. Bail with a clear diagnostic.
        id_path = self._write_id_file("")
        with mock.patch(
            "subunit.filter_scripts.jvmtest_subunit.run", return_value=0
        ) as run:
            with mock.patch("sys.stderr", new=io.StringIO()) as stderr:
                rc = jvmtest_subunit.main(["--id-file", id_path])
        self.assertEqual(2, rc)
        run.assert_not_called()
        self.assertIn("no usable test IDs", stderr.getvalue())

    def test_main_id_file_missing_returns_error(self):
        with mock.patch("sys.stderr", new=io.StringIO()) as stderr:
            rc = jvmtest_subunit.main(["--id-file", "/nonexistent/ids.txt"])
        self.assertEqual(2, rc)
        self.assertIn("failed to read", stderr.getvalue())

    def test_list_and_id_file_are_mutually_exclusive(self):
        # argparse prints "not allowed with argument" and exits 2 via
        # SystemExit; we just confirm the CLI rejects the combination.
        id_path = self._write_id_file("com.example.A")
        with mock.patch("sys.stderr", new=io.StringIO()):
            self.assertRaises(
                SystemExit,
                jvmtest_subunit.main,
                ["--list", "--id-file", id_path],
            )
