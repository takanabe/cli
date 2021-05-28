import os
from os.path import join
from pathlib import Path
import click
from junitparser import TestCase, TestSuite  # type: ignore
import json
from typing import List, Generator

from . import launchable
from ..testpath import TestPath
from ..utils.logger import Logger


def make_test_path(pkg, target) -> TestPath:
    return [{'type': 'package', 'name': pkg}, {'type': 'target', 'name': target}]


@launchable.subset
def subset(client):
    # Read targets from stdin, which generally looks like //foo/bar:zot
    for label in client.stdin():
        # //foo/bar:zot -> //foo/bar & zot
        if label.startswith('//'):
            pkg, target = label.rstrip('\n').split(':')
            # TODO: error checks and more robustness
            client.test_path(make_test_path(pkg.lstrip('//'), target))

    client.formatter = lambda x: x[0]['name'] + ":" + x[1]['name']
    client.run()


@click.argument('workspace', required=True)
@click.option('--build-event-json', 'build_event_json_files', help="set file path generated by --build_event_json_file", type=click.Path(exists=True), required=False, multiple=True)
@launchable.record.tests
def record_tests(client, workspace, build_event_json_files):
    """
    Takes Bazel workspace, then report all its test results
    """
    base = Path(workspace).joinpath('bazel-testlogs').resolve()
    if not base.exists():
        exit("No such directory: %s" % str(base))

    default_path_builder = client.path_builder

    def f(case: TestCase, suite: TestSuite, report_file: str) -> TestPath:
        # In Bazel, report path name contains package & target.
        # for example, for //foo/bar:zot, the report file is at bazel-testlogs/foo/bar/zot/test.xml
        # TODO: robustness
        pkgNtarget = report_file[len(str(base))+1:-len("/test.xml")]

        # last path component is the target, the rest is package
        # TODO: does this work correctly when on Windows?
        path = make_test_path(os.path.dirname(pkgNtarget),
                              os.path.basename(pkgNtarget))

        # let the normal path building kicks in
        path.extend(default_path_builder(case, suite, report_file))
        return path

    client.path_builder = f
    client.check_timestamp = False

    if build_event_json_files:
        for l in parse_build_event_json(build_event_json_files):
            if l is None:
                continue

            client.report(str(Path(base).joinpath(l, 'test.xml')))
    else:
        client.scan(str(base), '**/test.xml')

    client.run()


def parse_build_event_json(files: List[str]) -> Generator:
    for file in files:
        with open(file) as f:
            for line in f:
                try:
                    d = json.loads(line)
                except Exception as e:
                    Logger().error("Can not parse build event json {}".format(line))
                    yield
                if "id" in d:
                    if "testResult" in d["id"]:
                        if "label" in d["id"]["testResult"]:
                            label = d["id"]["testResult"]["label"]
                            # replace //foo/bar:zot to /foo/bar/zot
                            label = label.lstrip("/").replace(":", "/")
                            yield label
