import gzip
import json
import os
import platform

from requests import Session
from requests.adapters import HTTPAdapter
from requests.packages.urllib3.util.retry import Retry

from launchable.version import __version__
from .authentication import get_org_workspace, authentication_headers
from .env_keys import BASE_URL_KEY
from .logger import Logger, AUDIT_LOG_FORMAT

DEFAULT_BASE_URL = "https://api.mercury.launchableinc.com"


def get_base_url():
    return os.getenv(BASE_URL_KEY) or DEFAULT_BASE_URL


class LaunchableClient:
    def __init__(self, base_url: str = "", session: Session = None, test_runner: str = ""):
        self.base_url = base_url or get_base_url()

        if session is None:
            strategy = Retry(
                total=3,
                method_whitelist=["GET", "PUT", "PATCH", "DELETE"],
                status_forcelist=[429, 500, 502, 503, 504],
                backoff_factor=2
            )

            adapter = HTTPAdapter(max_retries=strategy)
            s = Session()
            s.mount("http://", adapter)
            s.mount("https://", adapter)
            self.session = s
        else:
            self.session = session

        self.test_runner = test_runner

        self.organization, self.workspace = get_org_workspace()
        if self.organization is None or self.workspace is None:
            raise ValueError("Organization/workspace cannot be empty")

    def request(self, method, sub_path, payload=None, timeout=(5, 60), compress=False):
        url = _join_paths(self.base_url, "/intake/organizations/{}/workspaces/{}".format(
            self.organization, self.workspace), sub_path)

        headers = self._headers(compress)

        Logger().audit(AUDIT_LOG_FORMAT.format(method, url, headers, payload))

        data = _build_data(payload, compress)

        try:
            response = self.session.request(
                method, url, headers=headers, timeout=timeout, data=data)
            Logger().debug(
                "received response status:{} message:{} headers:{}".format(
                    response.status_code, response.reason, response.headers)
            )
            return response
        except Exception as e:
            raise Exception("unable to post to %s" % url) from e

    def _headers(self, compress):
        h = {
            "User-Agent": "Launchable/{} (Python {}, {})".format(__version__, platform.python_version(),
                                                                 platform.platform()),
            "Content-Type": "application/json"
        }

        if compress:
            h["Content-Encoding"] = "gzip"

        if self.test_runner != "":
            h["User-Agent"] = h["User-Agent"] + \
                " TestRunner/{}".format(self.test_runner)

        return {**h, **authentication_headers()}


def _build_data(payload, compress):
    if payload is None:
        return None

    encoded = json.dumps(payload).encode()
    if compress:
        return gzip.compress(encoded)

    return encoded


def _join_paths(*components):
    return '/'.join([c.strip('/') for c in components])
