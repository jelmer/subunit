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

"""A filter that reads JUnit XML test reports and emits a subunit v2 stream.

JUnit XML is the de-facto interchange format for JVM test runners (Maven
Surefire, Gradle, Ant) and many other ecosystems. Maven and Gradle write
one XML file per test class into a reports directory, so this script
accepts directories as well as individual files.

Two operating modes:

* **Batch** (default): convert each input file once and exit.
* **Watch** (``--watch DIR``): poll the reports directory and stream
  packets as each per-class XML file finishes being written. Combined
  with ``--until-pid PID``, the script exits when the build process
  does — perfect for live progress under inquest::

      mvn test -q & junitxml2subunit --watch target/surefire-reports \\
          --until-pid $!

  Both Maven Surefire and Gradle write the per-class report to disk
  the moment that class finishes, so polling is enough to give live
  feedback without any cooperation from the build tool.
"""

import argparse
import os
import sys

from subunit import JUnitXML2SubUnit, watch_junit_xml


def parse_args(argv):
    parser = argparse.ArgumentParser(
        description=(
            "Convert JUnit XML test reports to a subunit v2 stream on stdout. "
            "Pass individual files as positional arguments, use -d/--dir to "
            "walk a reports directory for *.xml files, or use --watch DIR "
            "to stream results live as the test runner writes them."
        ),
    )
    parser.add_argument(
        "-d",
        "--dir",
        dest="dirs",
        action="append",
        default=[],
        metavar="DIR",
        help=(
            "Directory to walk for *.xml report files. May be repeated. "
            "Files inside the directory are converted in lexical order so "
            "the output is deterministic across runs."
        ),
    )
    parser.add_argument(
        "--watch",
        dest="watch",
        metavar="DIR",
        help=(
            "Watch DIR for newly-written *.xml report files and stream "
            "packets live. Combine with --until-pid to stop when the "
            "test runner exits. Mutually exclusive with -d/--dir and "
            "positional FILE args."
        ),
    )
    parser.add_argument(
        "--until-pid",
        dest="until_pid",
        type=int,
        metavar="PID",
        help=(
            "With --watch, exit when this process is no longer alive. "
            "Typically the PID of the backgrounded build tool: "
            "`mvn test & junitxml2subunit --watch DIR --until-pid $!`."
        ),
    )
    parser.add_argument(
        "--poll-secs",
        dest="poll_secs",
        type=float,
        default=1.0,
        metavar="SECS",
        help=(
            "With --watch, seconds between directory rescans. Defaults "
            "to 1.0; smaller values just waste CPU since Surefire and "
            "Gradle write reports at much coarser granularity."
        ),
    )
    parser.add_argument(
        "files",
        nargs="*",
        help="Individual JUnit XML report files to convert.",
    )
    return parser.parse_args(argv)


def collect_files(dirs, files):
    """Combine `--dir DIR` walks with explicit FILE arguments.

    Within each directory we sort by filename so the resulting subunit
    stream is reproducible. Across directories we preserve the user's
    argv order (some workflows feed multiple module-specific report
    directories and care about the suite ordering).
    """
    out = []
    for d in dirs:
        if not os.path.isdir(d):
            sys.stderr.write("junitxml2subunit: not a directory: {}\n".format(d))
            continue
        for root, _dirs, names in sorted(os.walk(d)):
            for name in sorted(names):
                if name.endswith(".xml"):
                    out.append(os.path.join(root, name))
    out.extend(files)
    return out


def main(argv=None):
    args = parse_args(argv if argv is not None else sys.argv[1:])

    if args.watch:
        if args.dirs or args.files:
            sys.stderr.write(
                "junitxml2subunit: --watch is mutually exclusive with "
                "-d/--dir and positional FILE arguments\n"
            )
            return 2
        return watch_junit_xml(
            args.watch,
            sys.stdout.buffer,
            until_pid=args.until_pid,
            poll_secs=args.poll_secs,
        )

    inputs = collect_files(args.dirs, args.files)
    if not inputs:
        sys.stderr.write("junitxml2subunit: no input files found (pass FILE arguments, use -d DIR, or use --watch DIR)\n")
        return 2
    return JUnitXML2SubUnit(inputs, sys.stdout.buffer)


if __name__ == "__main__":
    sys.exit(main())
