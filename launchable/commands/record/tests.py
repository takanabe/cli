import glob
import json
import os
import traceback
import click
from junitparser import JUnitXml, TestSuite, TestCase  # type: ignore
import xml.etree.ElementTree as ET
from typing import Callable, Generator, Iterator, List, Optional
from itertools import repeat, starmap, takewhile, islice
from more_itertools import ichunked
from .case_event import CaseEvent
from ...testpath import TestPathComponent
from ...utils.env_keys import REPORT_ERROR_KEY
from ...utils.gzipgen import compress
from ...utils.http_client import LaunchableClient
from ...utils.token import parse_token
from ...utils.env_keys import REPORT_ERROR_KEY
from ...utils.session import read_session, parse_session
from ...testpath import TestPathComponent
from .session import session as session_command
from ..helper import find_or_create_session
from http import HTTPStatus
from ...utils.click import KeyValueType
from ...utils.logger import Logger, AUDIT_LOG_FORMAT
import datetime


@click.group()
@click.option(
    '--base',
    'base_path',
    help='(Advanced) base directory to make test names portable',
    type=click.Path(exists=True, file_okay=False),
    metavar="DIR",
)
@click.option(
    '--session',
    'session',
    help='Test session ID',
    type=str,
)
@click.option(
    '--build',
    'build_name',
    help='build name',
    type=str,
    metavar='BUILD_NAME'
)
@click.option(
    '--debug',
    help='print request payload',
    default=False,
    is_flag=True,
)
@click.option(
    '--post-chunk',
    help='Post chunk',
    default=1000,
    type=int
)
@click.option(
    "--flavor",
    "flavor",
    help='flavors',
    cls=KeyValueType,
    multiple=True,
)
@click.pass_context
def tests(context, base_path: str, session: Optional[str], build_name: Optional[str], debug: bool, post_chunk: int, flavor):
    session_id = find_or_create_session(context, session, build_name, flavor)

    token, org, workspace = parse_token()
    record_start_at = get_record_start_at(
        token, org, workspace, build_name, session)

    logger = Logger()

    # TODO: placed here to minimize invasion in this PR to reduce the likelihood of
    # PR merge hell. This should be moved to a top-level class

    class RecordTests:
        # function that returns junitparser.TestCase
        # some libraries output invalid  incorrectly format then have to fix them.
        JUnitXmlParseFunc = Callable[[str],  ET.Element]

        @property
        def path_builder(self) -> CaseEvent.TestPathBuilder:
            """
            This function, if supplied, is used to build a test path
            that uniquely identifies a test case
            """
            return self._path_builder

        @path_builder.setter
        def path_builder(self, v: CaseEvent.TestPathBuilder):
            self._path_builder = v

        @property
        def junitxml_parse_func(self):
            return self._junitxml_parse_func

        @junitxml_parse_func.setter
        def junitxml_parse_func(self, f: JUnitXmlParseFunc):
            self._junitxml_parse_func = f

        @property
        def check_timestamp(self):
            return self._check_timestamp

        @check_timestamp.setter
        def check_timestamp(self, enable: bool):
            self._check_timestamp = enable

        def __init__(self):
            self.reports = []
            self.path_builder = CaseEvent.default_path_builder(base_path)
            self.junitxml_parse_func = None
            self.check_timestamp = True

        def make_file_path_component(self, filepath) -> TestPathComponent:
            """Create a single TestPathComponent from the given file path"""
            if base_path:
                filepath = os.path.relpath(filepath, start=base_path)
            return {"type": "file", "name": filepath}

        def report(self, junit_report_file: str):
            ctime = datetime.datetime.fromtimestamp(
                os.path.getctime(junit_report_file))

            if self.check_timestamp and ctime.timestamp() < record_start_at.timestamp():
                format = "%Y-%m-%d %H:%M:%S"
                logger.debug("skip: {} is old to report. start_record_at: {} file_created_at:{}".format(
                    junit_report_file, record_start_at.strftime(format), ctime.strftime(format)))
                return

            self.reports.append(junit_report_file)

        def scan(self, base, pattern):
            """
            Starting at the 'base' path, recursively add everything that matches the given GLOB pattern

            scan('build/test-reports', '**/*.xml')
            """
            for t in glob.iglob(os.path.join(base, pattern), recursive=True):
                self.report(t)

        def run(self):
            count = 0   # count number of test cases sent

            client = LaunchableClient(
                token, test_runner=context.invoked_subcommand)

            def testcases(reports: List[str]):
                exceptions = []
                for report in reports:
                    try:
                        # To understand JUnit XML format, https://llg.cubic.org/docs/junit/ is helpful
                        # TODO: robustness: what's the best way to deal with broken XML file, if any?
                        xml = JUnitXml.fromfile(
                            report, self.junitxml_parse_func)
                        if isinstance(xml, JUnitXml):
                            testsuites = [suite for suite in xml]
                        elif isinstance(xml, TestSuite):
                            testsuites = [xml]
                        else:
                            # TODO: what is a Pythonesque way to do this?
                            assert False

                        for suite in testsuites:
                            for case in suite:
                                yield json.dumps(CaseEvent.from_case_and_suite(self.path_builder, case, suite, report))

                    except Exception as e:
                        exceptions.append(Exception(
                            "Failed to process a report file: {}".format(report), e))

                if len(exceptions) > 0:
                    # defer XML persing exceptions
                    raise Exception(exceptions)

            def splitter(iterable: Generator, size: int) -> Iterator[Iterator]:
                return ichunked(iterable, size)

            # generator that creates the payload incrementally
            def payload(cases: Generator[TestCase, None, None]) -> Generator[str, None, None]:
                nonlocal count
                yield '{"events": ['
                first = True        # used to control ',' in printing
                for case in cases:
                    if not first:
                        yield ','
                    first = False
                    count += 1
                    yield case
                yield ']}'

            def printer(f: Generator) -> Generator:
                for d in f:
                    print(d)
                    yield d

            def send(payload: Generator[TestCase, None, None]) -> None:
                # str -> bytes then gzip compress
                headers = {
                    "Content-Type": "application/json",
                    "Content-Encoding": "gzip",
                }

                def audit_log(f: Generator) -> Generator:
                    args = []
                    for d in f:
                        args.append(d)
                        yield d

                    logger.audit(AUDIT_LOG_FORMAT.format(
                        "post", "{}/events".format(session_id), headers, list(args)))

                payload = (s.encode() for s in audit_log(payload))
                payload = compress(payload)

                res = client.request(
                    "post", "{}/events".format(session_id), data=payload, headers=headers)

                if res.status_code == HTTPStatus.NOT_FOUND:
                    if session:
                        _, _, build, _ = parse_session(session)
                        click.echo(click.style(
                            "Session {} was not found. Make sure to run `launchable record session --build {}` before `launchable record tests`".format(session, build), 'yellow'), err=True)
                    elif build_name:
                        click.echo(click.style(
                            "Build {} was not found. Make sure to run `launchable record build --name {}` before `launchable record tests`".format(build_name, build_name), 'yellow'), err=True)

                res.raise_for_status()

            try:
                tc = testcases(self.reports)
                if debug:
                    tc = printer(tc)
                splitted_cases = splitter(tc, post_chunk)
                for chunk in splitted_cases:
                    send(payload(chunk))

                headers = {
                    "Content-Type": "application/json",
                    "Content-Encoding": "gzip",
                }
                res = client.request(
                    "patch", "{}/close".format(session_id), headers=headers)
                res.raise_for_status()

            except Exception as e:
                if os.getenv(REPORT_ERROR_KEY):
                    raise e
                else:
                    traceback.print_exc()
                    return

            click.echo("Recorded {} tests".format(count))
            if count == 0:
                click.echo(click.style(
                    "Looks like tests didn't run? If not, make sure the right files/directories are passed", 'yellow'))

    context.obj = RecordTests()


def get_record_start_at(token: str, org: str, workspace: str, build_name: Optional[str], session: Optional[str]):
    if session is None and build_name is None:
        raise click.UsageError(
            'Either --build or --session has to be specified')

    if session:
        _, _, build_name, _ = parse_session(session)

    client = LaunchableClient(token)
    headers = {
        "Content-Type": "application/json",
    }
    path = "/intake/organizations/{}/workspaces/{}/builds/{}".format(
        org, workspace, build_name)

    Logger().audit(AUDIT_LOG_FORMAT.format(
        "get", path, headers, None))

    res = client.request("get", path, headers=headers)
    if res.status_code == 404:
        click.echo(click.style(
            "Build {} was not found. Make sure to run `launchable record build --name {}` before `launchable record tests`".format(build_name, build_name), 'yellow'), err=True)
    elif res.status_code != 200:
        # to avoid stop report command
        return datetime.datetime.now()

    return parse_launchable_timeformat(res.json()["createdAt"])


def parse_launchable_timeformat(t: str) -> datetime.datetime:
    # e.g) "2021-04-01T09:35:47.934+00:00"
    try:
        return datetime.datetime.strptime(t, "%Y-%m-%dT%H:%M:%S.%f%z")
    except Exception as e:
        Logger().error(
            "parse time error {}. time: {}".format(str(e), t))
        return datetime.datetime.now()
