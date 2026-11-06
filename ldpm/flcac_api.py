"""
Download and cache FLCAC data packages via the public API

FLCAC API docs: https://www.lcacommons.gov/lca-commons-api-guide
"""
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from urllib.parse import urlencode, urljoin
from zoneinfo import ZoneInfo

import requests
import yaml
from dotenv import dotenv_values

from ldpm.utils import PATHS, find_file


yaml.SafeDumper.add_representer(
    defaultdict, yaml.representer.SafeRepresenter.represent_dict)

log = logging.getLogger(__name__)

_PATH_INDEX_DPKG = PATHS.config / 'data_packages.yaml'


def _nested_dict() -> defaultdict:
    return defaultdict(_nested_dict)
    

@dataclass
class APIClientSession:
    """Container for managing requests sessions and configs"""
    session: requests.Session = field(default_factory=requests.Session)
    headers: dict = field(default_factory=dict)
    timeout: float = 10.0
    url_base: str = field(init=False)
    api_key: str | None = field(init=False)
    
    def __post_init__(self) -> None:
        self.session.headers.update(self.headers)
        
        # attempt to read .env secrets, with .env.example as fallback
        try:
            env_file_path = find_file('.env')
        except FileNotFoundError:
            env_file_path = find_file('.env.example')

        secrets = dotenv_values(env_file_path)

        if secrets['API_KEY']:
            self.api_key = secrets['API_KEY']
            self.url_base = 'https://api.nal.usda.gov/FederalLCACommonsapi/'
        else:
            self.api_key = None
            self.url_base = 'https://www.lcacommons.gov/lca-collaboration/ws/public/'
         
    def get(self, endpoint: str, params: dict| None = None, **kwargs) -> bytes:
        """Submit GET request to FLCAC API"""
        url = self._build_get_url(url_path=endpoint, params=params)
        response = self.session.get(url, timeout=self.timeout, **kwargs)
        response.raise_for_status()
        return response
        
    def _build_get_url(self, url_path: str, params: dict | None = None) -> str:
        """Build a valid GET request URL for the FLCAC API"""
        url_base_path = urljoin(self.url_base, url_path.strip('/'))
        if not (params or self.api_key):
            return url_base_path
        else:
            if params is None:
                params = {}
            if self.api_key:
                params |= {'api_key': self.api_key}
            query = urlencode(params)
            return f'{url_base_path}?{query}'

    def compile_dpkg_release_index(self) -> None:
        """
        Generate an index of all available dpkg releases. Fetches metadata via
        FLCAC API, compiles {group: {name: {version: commit}}} dict, then writes
        to ./config/data_packages.yaml if non-existent or new release is available
        """
        response = self.get(endpoint='repository')  # list public dpkg repos
        metadata_dpkgs = response.json()
        
        index = _nested_dict()
        release_dates = []
        for dpkg in sorted(metadata_dpkgs, key=lambda d: (d['group'].lower(), d['name'].lower())):
            group, name = dpkg['group'], dpkg['name']
            if 'commons_merged' in name:
                continue
            if not dpkg['hasReleases']:
                log.warning(f'Data package has no releases: {name}')
                index[group][name] = None
            else:
                release_dates.append( 
                    datetime.fromtimestamp(
                        dpkg['settings']['releaseDate'] / 1000.0,  # milliseconds
                        tz=ZoneInfo('America/New_York')))
                response = self.get(f'history/{group}/{name}')  # list all releases
                dpkg_releases = response.json()
                for release in dpkg_releases:  # already ordered newest-to-oldest
                    info = release['releaseInfo']
                    index[group][name][info['version']] = info['commitId']
        
        # skip dpkg-index update if generated after the latest release
        if (_PATH_INDEX_DPKG.exists() and 
            (datetime.fromtimestamp(_PATH_INDEX_DPKG.stat().st_mtime).astimezone() > 
             max(release_dates))):
            return
        else:
            with _PATH_INDEX_DPKG.open('w') as file:
                yaml.safe_dump(index, file, sort_keys=False, indent=4)


SESSION = APIClientSession()

# compile and load index of available dpkgs
SESSION.compile_dpkg_release_index()
with _PATH_INDEX_DPKG.open() as file:
    INDEX_DPKG = yaml.safe_load(file) 
