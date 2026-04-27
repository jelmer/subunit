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

Three CLI modes mirror the testrepository / inquest contract so a
single ``test_command`` line covers the whole workflow:

* ``jvmtest-subunit`` runs the whole suite.
* ``jvmtest-subunit --list`` walks ``src/test/java`` /
  ``src/test/kotlin`` and emits subunit ``exists`` events for every
  conventionally-named test class. Methods discovered at runtime
  (``@ParameterizedTest``, ``@TestFactory`` dynamic tests) aren't in
  the listing — they'd require running the suite to discover.
* ``jvmtest-subunit --id-file FILE`` reads ``Class`` /
  ``Class::method`` IDs and translates them to the build tool's
  selection vocabulary (``-Dtest=`` for Maven, ``--tests`` for
  Gradle), then runs only those.

Typical inquest config::

    test_command = "jvmtest-subunit $LISTOPT $IDOPTION"
    test_id_option = "--id-file $IDFILE"
    test_list_option = "--list"
"""

import argparse
import os
import subprocess
import sys

from subunit import watch_junit_xml
from subunit.v2 import StreamResultToBytes


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
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--list",
        dest="list_tests",
        action="store_true",
        help=(
            "Enumerate test classes by walking src/test/java and "
            "src/test/kotlin and emit subunit `exists` events instead "
            "of running the suite. Methods created at runtime "
            "(parameterised, dynamic) aren't listed — they're only "
            "visible after a real run."
        ),
    )
    mode.add_argument(
        "--id-file",
        dest="id_file",
        metavar="FILE",
        help=(
            "Run only the tests whose IDs (one per line) are listed in "
            "FILE. Each ID is either a fully-qualified class name "
            "(`com.example.FooTest`) or a class+method "
            "(`com.example.FooTest::testBar`). The wrapper translates "
            "to the build tool's selection vocabulary: `-Dtest=` for "
            "Maven, `--tests` for Gradle."
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


# Surefire's default test-class patterns. Gradle's defaults are similar
# (``*Tests``/`*Test`/`*TestCase`); ``*IT`` covers integration tests.
TEST_CLASS_SUFFIXES = ("Test", "Tests", "TestCase", "IT")
TEST_SOURCE_DIRS = ("src/test/java", "src/test/kotlin")


def _looks_like_test_class(stem):
    return any(stem.endswith(suffix) for suffix in TEST_CLASS_SUFFIXES)


def discover_test_classes(cwd):
    """Walk the conventional test source roots and yield FQCNs.

    Looks under ``src/test/java`` and ``src/test/kotlin`` (the Maven
    and Gradle conventions). Files whose stem matches one of
    ``*Test``/``*Tests``/``*TestCase``/``*IT`` are treated as test
    classes; the FQCN is derived from the path relative to the source
    root with ``/`` replaced by ``.``.

    Returns a sorted list of FQCNs so the resulting subunit stream is
    reproducible across runs and filesystems.
    """
    found = set()
    for source_dir in TEST_SOURCE_DIRS:
        root = os.path.join(cwd, source_dir)
        if not os.path.isdir(root):
            continue
        for dirpath, _dirs, filenames in os.walk(root):
            rel_pkg = os.path.relpath(dirpath, root)
            # The source root itself maps to the empty package.
            pkg = "" if rel_pkg == "." else rel_pkg.replace(os.sep, ".")
            for name in filenames:
                stem, ext = os.path.splitext(name)
                if ext not in (".java", ".kt"):
                    continue
                if not _looks_like_test_class(stem):
                    continue
                fqcn = "{}.{}".format(pkg, stem) if pkg else stem
                found.add(fqcn)
    return sorted(found)


def list_tests(cwd, output_stream):
    """Emit subunit `exists` events for every discovered test class.

    Per-method discovery would require parsing source files to find
    ``@Test`` annotations — and even then would miss runtime-generated
    tests. Class-level discovery matches what the build tools'
    selection flags want anyway (``-Dtest=ClassName``,
    ``--tests ClassName``), so it's the natural granularity.

    :return: Number of classes emitted (zero is suspicious — surfaces
        the no-tests case to the caller).
    """
    output = StreamResultToBytes(output_stream)
    fqcns = discover_test_classes(cwd)
    for fqcn in fqcns:
        output.status(test_id=fqcn, test_status="exists", eof=True)
    return len(fqcns)


def parse_test_id(line):
    """Split a load-list line into ``(class, method)``.

    Returns ``(class, None)`` for a bare class ID and ``(class,
    method)`` for ``class::method``. Returns ``None`` for empty or
    malformed lines.
    """
    line = line.strip()
    if not line:
        return None
    if "::" in line:
        cls, _, method = line.rpartition("::")
        if not cls or not method:
            return None
        return cls, method
    return line, None


def group_ids(ids):
    """Group an iterable of test IDs into ``{class: [method, ...]}``.

    A class with at least one bare-class entry maps to ``[]`` (meaning
    "all methods"); otherwise the value lists the specific methods to
    run. Malformed IDs are warned and skipped — losing them silently
    would mask configuration mistakes.
    """
    groups = {}
    for raw in ids:
        parsed = parse_test_id(raw)
        if parsed is None:
            stripped = raw.strip()
            if stripped:
                sys.stderr.write(
                    "jvmtest-subunit: skipping malformed test ID '{}' "
                    "(expected '<class>' or '<class>::<method>')\n".format(stripped)
                )
            continue
        cls, method = parsed
        existing = groups.get(cls)
        if existing is None:
            groups[cls] = [] if method is None else [method]
        elif method is None:
            # A bare-class entry overrides any prior method list —
            # "run everything in this class" subsumes the methods.
            groups[cls] = []
        elif existing:  # existing is a non-empty method list
            existing.append(method)
        # else: existing is [] (all-methods); leave it alone.
    return groups


def build_maven_test_arg(groups):
    """Build the value for Maven's ``-Dtest=...`` flag.

    Maven Surefire's ``-Dtest`` accepts comma-separated entries. A
    class-only entry runs every method; ``Class#methodA+methodB``
    runs only those methods. The resulting flag is a single
    ``-Dtest=...`` string ready to be appended to the build argv.
    """
    parts = []
    for cls in sorted(groups):
        methods = groups[cls]
        if not methods:
            parts.append(cls)
        else:
            parts.append("{}#{}".format(cls, "+".join(sorted(set(methods)))))
    return "-Dtest=" + ",".join(parts)


def build_gradle_tests_args(groups):
    """Build a list of ``--tests PATTERN`` args for Gradle.

    Gradle's ``--tests`` is per-pattern (repeatable). ``Class``
    matches every method; ``Class.method`` matches just the one.
    Returns a flat list ready to extend the build argv.
    """
    args = []
    for cls in sorted(groups):
        methods = groups[cls]
        if not methods:
            args.extend(["--tests", cls])
        else:
            for method in sorted(set(methods)):
                args.extend(["--tests", "{}.{}".format(cls, method)])
    return args


def build_selection_args(tool, groups):
    """Translate grouped IDs into build-tool-specific selection args."""
    if not groups:
        return []
    if tool == "maven":
        return [build_maven_test_arg(groups)]
    if tool == "gradle":
        return build_gradle_tests_args(groups)
    raise ValueError("unknown tool: {}".format(tool))


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

    # --list doesn't need a build tool — it's a pure source-tree scan.
    if args.list_tests:
        if build_args:
            sys.stderr.write(
                "jvmtest-subunit: --list ignores extra build args\n"
            )
        list_tests(os.getcwd(), sys.stdout.buffer)
        return 0

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

    selection_args = []
    if args.id_file:
        try:
            with open(args.id_file, "r", encoding="utf-8") as fh:
                ids = list(fh)
        except OSError as exc:
            sys.stderr.write(
                "jvmtest-subunit: failed to read --id-file {}: {}\n".format(
                    args.id_file, exc
                )
            )
            return 2
        groups = group_ids(ids)
        if not groups:
            # No usable IDs — bail rather than running the whole suite,
            # which would be the opposite of the user's intent.
            sys.stderr.write(
                "jvmtest-subunit: --id-file {} contains no usable test "
                "IDs; refusing to run the whole suite\n".format(args.id_file)
            )
            return 2
        selection_args = build_selection_args(tool, groups)

    reports_dir = args.reports_dir or TOOLS[tool]["reports_dir"]
    return run(
        tool=tool,
        reports_dir=reports_dir,
        build_args=selection_args + list(build_args),
        output_stream=sys.stdout.buffer,
        stderr_stream=sys.stderr,
        poll_secs=args.poll_secs,
    )


if __name__ == "__main__":
    sys.exit(main())
