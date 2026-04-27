#!/usr/bin/env python3
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

"""Run a JVM test suite (Maven or Gradle) and emit a subunit v2 stream.

Drives ``mvn test`` or ``gradle test`` and watches its JUnit XML reports
directory live, so each test class's results stream out as the class
finishes — no waiting for the whole suite to complete. The build tool
is auto-detected from the working directory (``pom.xml`` → Maven;
``build.gradle{,.kts}`` / ``settings.gradle{,.kts}`` → Gradle).

Build-tool stdout and stderr are forwarded to *our* stderr so users
still see compile errors and progress; subunit packets go to stdout.

Typical inquest config::

    test_command = "jvmtest-subunit"
"""

import argparse
import os
import subprocess
import sys

from subunit import watch_junit_xml


# Per-tool defaults: command line plus the directory to watch for
# JUnit reports. Surefire's default reports path is
# `target/surefire-reports`, Gradle's `Test` task writes to
# `build/test-results/test` by default.
TOOLS = {
    "maven": {
        "default_argv": ["mvn", "test", "-q"],
        "reports_dir": "target/surefire-reports",
    },
    "gradle": {
        "default_argv": ["gradle", "test", "-q"],
        "reports_dir": "build/test-results/test",
    },
}


def parse_args(argv):
    parser = argparse.ArgumentParser(
        description=(
            "Run a JVM test suite (Maven or Gradle) and emit a subunit v2 "
            "stream on stdout, watching the JUnit XML reports directory "
            "live so per-class results stream as soon as each class "
            "finishes."
        ),
    )
    parser.add_argument(
        "--tool",
        choices=sorted(TOOLS.keys()),
        help=(
            "Which build tool to invoke. Defaults to whichever is detected "
            "from the current directory (pom.xml → maven; build.gradle* / "
            "settings.gradle* → gradle)."
        ),
    )
    parser.add_argument(
        "--reports-dir",
        metavar="DIR",
        help=(
            "Directory containing the per-class JUnit XML reports. "
            "Defaults to target/surefire-reports for Maven or "
            "build/test-results/test for Gradle. Override for non-default "
            "Surefire/Gradle layouts."
        ),
    )
    parser.add_argument(
        "--poll-secs",
        type=float,
        default=1.0,
        metavar="SECS",
        help=(
            "Seconds between rescans of the reports directory. Defaults "
            "to 1.0; smaller values waste CPU since the build tool writes "
            "reports at much coarser granularity."
        ),
    )
    parser.add_argument(
        "build_args",
        nargs=argparse.REMAINDER,
        help=(
            "Extra arguments forwarded to the build tool. Place after "
            "`--`, e.g. `jvmtest-subunit -- -Dmaven.test.skip=false`."
        ),
    )
    return parser.parse_args(argv)


def detect_tool(cwd):
    """Auto-detect the build tool from files in ``cwd``.

    Returns the tool name, or ``None`` if no signal is found, or raises
    ``RuntimeError`` if both signals are present (the user must pick
    explicitly via ``--tool`` to disambiguate).
    """
    has_maven = os.path.exists(os.path.join(cwd, "pom.xml"))
    has_gradle = any(
        os.path.exists(os.path.join(cwd, name))
        for name in ("build.gradle", "build.gradle.kts", "settings.gradle", "settings.gradle.kts")
    )
    if has_maven and has_gradle:
        raise RuntimeError(
            "both Maven (pom.xml) and Gradle markers present; "
            "pass --tool maven or --tool gradle to choose"
        )
    if has_maven:
        return "maven"
    if has_gradle:
        return "gradle"
    return None


def _strip_double_dash(args):
    # ``argparse.REMAINDER`` keeps the leading ``--`` when present.
    if args and args[0] == "--":
        return args[1:]
    return args


def run(tool, reports_dir, build_args, output_stream, stderr_stream, poll_secs=1.0):
    """Spawn the build tool and stream subunit packets from its reports.

    Designed to be unit-testable: the build subprocess invocation is
    overrideable in tests by patching ``subprocess.Popen``, and the
    watch loop is the well-tested ``watch_junit_xml``.

    :param tool: One of the keys in ``TOOLS``.
    :param reports_dir: Directory to watch for ``*.xml`` reports.
    :param build_args: Extra args appended to the tool's default argv.
    :param output_stream: Binary stream to write subunit packets to
        (typically ``sys.stdout.buffer``).
    :param stderr_stream: Stream to forward the build tool's combined
        stdout/stderr to (typically ``sys.stderr``).
    :param poll_secs: Watch poll interval.
    :return: Exit code. 0 on success, non-zero if any test failed or
        the build crashed before producing any reports.
    """
    spec = TOOLS[tool]
    argv = list(spec["default_argv"]) + list(build_args)
    # Make sure the reports directory exists *before* we start the
    # watcher, otherwise the first sweep aborts with "not a directory".
    # Maven and Gradle both create it themselves but only once they
    # actually start running tests, which can be tens of seconds into
    # the build.
    os.makedirs(reports_dir, exist_ok=True)

    # Forward both streams to our stderr so the user sees compile
    # errors and progress; subunit packets are reserved for stdout.
    proc = subprocess.Popen(
        argv,
        stdout=stderr_stream,
        stderr=stderr_stream,
    )

    try:
        watch_rc = watch_junit_xml(
            reports_dir, output_stream, until_pid=proc.pid, poll_secs=poll_secs
        )
    except KeyboardInterrupt:
        # Propagate SIGINT to the build tool so it tears down cleanly,
        # then re-raise so the script exits with the conventional
        # "interrupted" disposition.
        proc.terminate()
        proc.wait()
        raise

    build_rc = proc.wait()

    # If the build crashed and the watcher saw no reports, the test
    # results we'd otherwise return (zero failures) are misleading —
    # the suite never ran. Prefer the build's exit code in that case.
    has_any_reports = any(
        name.endswith(".xml") for name in os.listdir(reports_dir)
    ) if os.path.isdir(reports_dir) else False

    if build_rc != 0 and not has_any_reports:
        stderr_stream.write(
            "jvmtest-subunit: build tool exited {} without producing any "
            "test reports — assuming a build failure\n".format(build_rc)
        )
        return build_rc

    return watch_rc


def main(argv=None):
    args = parse_args(argv if argv is not None else sys.argv[1:])
    build_args = _strip_double_dash(args.build_args)

    tool = args.tool
    if tool is None:
        try:
            tool = detect_tool(os.getcwd())
        except RuntimeError as exc:
            sys.stderr.write("jvmtest-subunit: {}\n".format(exc))
            return 2
        if tool is None:
            sys.stderr.write(
                "jvmtest-subunit: no Maven (pom.xml) or Gradle "
                "(build.gradle*) project detected in current directory; "
                "pass --tool maven or --tool gradle to choose explicitly\n"
            )
            return 2

    reports_dir = args.reports_dir or TOOLS[tool]["reports_dir"]
    return run(
        tool=tool,
        reports_dir=reports_dir,
        build_args=build_args,
        output_stream=sys.stdout.buffer,
        stderr_stream=sys.stderr,
        poll_secs=args.poll_secs,
    )


if __name__ == "__main__":
    sys.exit(main())
