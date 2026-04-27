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
