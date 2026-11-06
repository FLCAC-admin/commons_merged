"""
Fetch FLCAC data packages (dpkg); deduplicate by UUID; link across dpkgs by
overwriting .Ref attributes; then merge into a single olca-schema .ZIP

Rules for deduplicating same-UUID objects:
    1. Import deduplicate.yaml dict of {original/parent dpkg:uuid_child} pairs,
       and ignore all instances of uuid_child that appear in other non-parent dpkgs
       > Implicit rules omitted from deduplicate.yaml: 
           - FEDEFL is parent for all elem. flows
           - USEEIO is parent for all tech. flows which it contains
    2. If duplicate UUIDs arise w/o any specified parent, defer to the order 
       of the dpkg entries in manifest's [dependencies] table (i.e., first takes priority)
"""

import json
import logging
import logging.config  # <-- can this go away?
import sys
import tomllib
import zipfile as zf
from collections import defaultdict
from contextlib import nullcontext
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import yaml

from ldpm.utils import LOGGING_CONFIG, PATHS, find_file, format_log_msg

logging.config.dictConfig(LOGGING_CONFIG)
log = logging.getLogger(__name__)

from ldpm.flcac_api import SESSION, INDEX_DPKG
from ldpm.linking import prepare_provider_links


# %%
@dataclass(frozen=True)  # unsafe_hash=True allows hashing w/o immutability
class DataPackage:
    name: str
    version: str | None = None
    commitId: str | None = None
    excluded_types: tuple[str] = field(default_factory=tuple)
    group: str = field(init=False, repr=False)
    
    @property
    def id(self):
        if self.version:  
            return f'{self.name}--v{self.version}'   # public release
        else:
            return f'{self.name}--_{self.commitId}'  # pre-release
    
    @property
    def path_zip(self) -> Path:
        return PATHS.cache / f'{self.id}.zip'
    
    @property
    def zipfile(self, mode: str = 'r') -> zf.ZipFile:
        return zf.ZipFile(self.path_zip, mode)
                    
    def __eq__(self, other):
        if not isinstance(other, DataPackage):
            return NotImplemented
        return self.id == other.id

    def __repr__(self) -> str:
        attrs = [f'name={self.name!r}']
        if self.version is not None:
            attrs.append(f'version={self.version!r}')
        else:
            attrs.append(f'commitId={self.commitId!r}')
        if self.excluded_types:
            attrs.append(f'excluded_types={self.excluded_types!r}')
        return f'{self.__class__.__name__}({', '.join(attrs)})'

    def __str__(self) -> str:
        if self.version:
            return self.id
        else:
            return f'{self.name}--_{self.commitId[:7]}'  # short hash for readability

    def __post_init__(self) -> None:
        # validate olca .ZIP-package type folder exclusions:
        valid_types = {
            'actors', 'currencies', 'dq_systems', 'epds', 'flows', 'flow_properties', 
            'lcia_categories', 'lcia_methods', 'locations', 'parameters', 'processes', 
            'product_systems','projects', 'results', 'social_indicators', 'sources', 
            'unit_groups',
            }
        invalid_types = []
        for _type in self.excluded_types:
            if _type not in valid_types:
                invalid_types.append(_type)
        if invalid_types:
            msg = format_log_msg(
                [f'Invalid excluded type(s) for {self.name}:',
                 f'\t- {"\n\t\t- ".join(invalid_types)}',
                 'Please use only "folder" type labels of Zip packages in olca-schema docs:'
                 '\thttps://greendelta.github.io/olca-schema/#zip-packages'])
            raise ValueError(msg)
        # infer .group from .name
        try:
            groups = [group for group, dpkgs in INDEX_DPKG.items()
                      if self.name in dpkgs.keys()]
            _group, = groups
            attribute_updates = {'group': _group}
        except ValueError as e:
            if not groups:
                msg = f'No group (top-level key) available in data_packages.yaml for "{self.name}"'
            elif len(groups) > 1:
                msg = (f'Multiple groups contain a dpkg named "{self.name}";'
                       ' contact FLCAC admin to fix.')
            log.exception(msg)
            raise ValueError(msg) from e

        match (self.version, self.commitId):
            case (_version, None):
                if _version == "*":  # replace wildcard w/ latest version
                    _version = next(iter(INDEX_DPKG[_group][self.name]))
                    attribute_updates |= {'version': _version}
                attribute_updates |= {'commitId': INDEX_DPKG[_group][self.name][_version]}
            case (None, _commitId):
                # if .commitId hash in INDEX_DPKG, use corresponding version; else None
                _version = next((k for k, v in INDEX_DPKG[_group][self.name].items() if v == _commitId), 
                                None)
                attribute_updates |= {'version': _version}
            case (_, _):
                # validate mutually exclusive usage of .version and .commitId on init
                error_msg = ('Cannot initialize {self.name} with both .version and .commitId. '
                             'In manifest TOML, please use only `version` for a public release '
                             'or `commitId` for a pre-release (private).')
                raise ValueError(error_msg)
        # set post-init attributes on frozen instance
        for attr, value in attribute_updates.items():
            object.__setattr__(self, attr, value)
    
    def iter_zip_jsons(
        self, 
        dedup: dict = None,
        metadata_only: bool = False,
        subdir: str = None,
    ) -> (zf.ZipInfo, bytes | None):
        """
        Generate an iterable over the JSON-LD files within the dpkg .zip archive, 
        ignoring non-JSON, binary, and root-level files.
        
        Args:
            dedup: deduplication index, as compiled by get_dedup_config()
            metadata_only: if True, yield only the files' zf.ZipInfo metadata
            subdir: filter to only return files w/i a subdirectory of the .zip (e.g., 'processes') 
        """
        if dedup is None:
            dedup = {}
        objs_ignore = dedup.get(self.name, set()) | {'categories.json', 'openlca.json'}
        dirs_ignore = ('bin/', *self.excluded_types)
        if subdir in dirs_ignore:
            log.exception(f'Cannot access subdir {subdir} due to excluded_types')
        with self.zipfile as z:
            for metadata in z.infolist():
                file_rpath = metadata.filename  # str, relative file path w/i .zip
                if subdir:
                    if not file_rpath.startswith(subdir): continue
                if (not file_rpath.endswith('.json')
                    or file_rpath.startswith(dirs_ignore)
                    or file_rpath in objs_ignore
                    ): continue
                if metadata_only:
                    yield (metadata, None)
                else:
                    with z.open(file_rpath, 'r') as file_stream:
                        yield (metadata, file_stream)
    
    def fetch(self) -> None:
        """Check local cache for a data package; download if absent"""
        if self.path_zip.exists():
            log.info(f'Already cached:\n\t{self.path_zip.as_posix()}')
        else:
            try:
                # ask server to generate unique token, indexing dpkg for download
                token = (SESSION.get(f'download/json/prepare/{self.group}/{self.name}',
                                     params={'commitId': self.commitId})
                                .content.decode().strip())
                # download dpkg via the token
                dpkg_bytes = SESSION.get(f'download/json/{token}').content
            except Exception as e:
                log.exception(f'Bad download of {self.name}: {e}')
                raise
            with self.path_zip.open('xb') as f:
                f.write(dpkg_bytes)
                log.info(f'Downloaded and cached:\n\t{self.path_zip.as_posix()}')
            

@dataclass(frozen=True)
class Build:
    name: str
    version: str
    manifest_path: Path = field(repr=False)
    dependencies: tuple[DataPackage] = field(default_factory=tuple)
    dependencies_indirect: tuple[DataPackage] = field(init=False, default_factory=tuple)
    created_at: str = field(default_factory=datetime.now)

    @classmethod
    def from_manifest_toml(cls, file_name: str = 'manifest.toml'):  # -> Build
        manifest_path = find_file(file_name)
        with (manifest_path).open('rb') as f:
            manifest = tomllib.load(f)
        dependencies = ()
        for dpkg_name, spec in manifest['dependencies'].items():
            kwargs = {'name': dpkg_name}
            match spec:
                case str():  # expects SemVer string or "*" wildcard
                    kwargs |= {'version': spec}
                case dict():
                    match (spec.get('version'), spec.get('commit')):
                        case (None, None):
                            error_msg = ('Under [dependencies] in manifest TOML, '
                                         f'{dpkg_name} lacks a "version" or "commit" identfier')
                            log.error(error_msg)
                            raise KeyError(error_msg)
                        case (_version, None):
                            kwargs |= {'version': _version}
                        case (None, _commitId):
                            kwargs |= {'commitId': _commitId}
                        case (_, _):
                            error_msg = ('Under [dependencies] in manifest TOML, '
                                         f'{dpkg_name} has both "version" and "commit" identifiers; '
                                         'please provide only one or the other.')
                            log.error(error_msg)
                            raise ValueError(error_msg)
                    match spec.get('exclude', []):
                        case str() as _type:
                            kwargs |= {'excluded_types': (_type,)}
                        case list() as _types:
                            kwargs |= {'excluded_types': (*_types,)}
            dependencies += (DataPackage(**kwargs),)
        return cls(name=manifest['build']['name'], 
                   version=manifest['build']['version'],
                   manifest_path=manifest_path,
                   dependencies=dependencies)

    def __post_init__(self) -> None:
        dpkgs_available = {name: tuple(version for version in version_subdict.keys())
                           for name_subdict in INDEX_DPKG.values() 
                           for name, version_subdict in name_subdict.items()}
        for dpkg in self.dependencies:
            if dpkg.name not in dpkgs_available:
                error_msg = f'No data package named "{self.name}" is available, per data_packages.yaml.'
                log.error(error_msg)
                raise KeyError(error_msg)
            elif (dpkg.version not in dpkgs_available[dpkg.name] 
                  and dpkg.commitId is None):
                error_msg = f'Version "{self.version}" of data package "{self.name}" unavailable.'
                log.error(error_msg)
                raise KeyError(error_msg)
        """
        An 'indirect dependency' contains objects used across most/all other 
        dpkgs on the FLCAC; including them in Build.dependencies is optional; 
        and omission from Build defaults to using the latest version
        """
        names_indirect_dependencies  = (
            'elementary_flow_list',  # elem. flows
            'USEEIO_v2',  # tech. flows
            'Fed_Commons_core_database',  #  non-process objs.
            )
        dependencies_indirect = tuple(
            DataPackage(name, dpkgs_available[name][0])
            for name in names_indirect_dependencies
            if name not in [dpkg.name for dpkg in self.dependencies])
        object.__setattr__(self, 'dependencies_indirect', dependencies_indirect)
    
    def __str__(self) -> str:
        dpkgs_as_str = lambda dpkgs: ',\n\t'.join(str(dpkg) for dpkg in dpkgs)
        str_deps_direct = dpkgs_as_str(self.dependencies)
        str_deps_indirect = dpkgs_as_str(self.dependencies_indirect)
        return (f'Build: {self.name}, v{self.version}\n'
                f'Dependencies, direct:\n\t{str_deps_direct}\n'
                f'Dependencies, indirect:\n\t{str_deps_indirect}'
                f'Created: {self.created_at.strftime("%y-%m-%d %T")}')
    
    @property
    def dependencies_all(self) -> set[DataPackage]:
        return self.dependencies + self.dependencies_indirect
    
    @property
    def manifest_path_short(self) -> str:
        return '~/' + self.manifest_path.relative_to(PATHS.project.parent).as_posix()
    
    def fetch_dependencies(self) -> None:
        # TODO: add command-line progress bar
        for dpkg in self.dependencies_all: 
            dpkg.fetch()


def get_dedup_config(build: Build) -> dict:
    """
    Import deduplicate.yaml and invert it from
    (1) {parent_dpkg: [UUIDs duplicated in other_dpkg(s)]}, to
    (2) {other_dpkg: file_rpath} to ignore, for each other_dpkg in the build,
        where file_rpath is a .zip-internal relative file path
    """
    with (PATHS.config / 'deduplicate.yaml').open() as file:
        dedup_orig = yaml.safe_load(file)
    dpkgs_build = {dpkg.name for dpkg in build.dependencies}
    if dpkgs_build > dedup_orig.keys():
        log.warning('One or more dependencies lack deduplicate.yaml entries:'
                    f'\n\t{dpkgs_build - dedup_orig.keys()}'
                    'Fall-back deduplication will occur via descending order of '
                    f'[dependencies] entries in {build.manifest_path_short}')    
    dedup = defaultdict(set)
    for dpkg_name, uuids_by_type in dedup_orig.items():
        for _type, uuids in uuids_by_type.items():
            if uuids is not None:
                for dpkg_other in (dpkgs_build - {dpkg_name}):
                    dedup[dpkg_other] |= {f'{_type}/{uuid}.json' for uuid in uuids}
    return dict(dedup)


def update_provider_links(process: dict, process_links: dict) -> dict:
    """
    For a single olca.Process JSON file, alter `exchange.defaultProvider.@id` 
    field/s via instructions complied from provider_links.yaml and linking.py
    
    Args:
        process: dict of olca.Process JSON
        process_links: sub-dict accessed via links_rpath_all[file_rpath]
    Returns:
        olca.Process with updated exchange.defaultProvider links, as single-line JSON
    """
    # reindex on exchange.internalId; exchange.flow.@id keys are for readability
    exchange_updates = {exchange_id: provider_id
                        for sub_dict in process_links.values() 
                        for exchange_id, provider_id in sub_dict.items()}
    # target exchange pointers for in-place alteration
    target_exchanges = {exchange['internalId']: exchange
                        for exchange in process['exchanges'] 
                        if exchange['internalId'] in exchange_updates}
    for exchange_id, exchange in target_exchanges.items():
        exchange['defaultProvider'] = {'@id': exchange_updates[exchange_id]}
    return process


def write_compact_json(_dict: dict) -> None:
    """Write dict to single-line JSON, without trailing whitespace"""
    return json.dumps(_dict, separators=(',', ':'), ensure_ascii=False)


def add_dpkg_tag(process: dict, dpkg: DataPackage) -> str:
    """
    Append a dpkg.__str__ identifier to an olca.Process's .tags list

    Args:
        json_str: parsed olca.Process JSON
    Returns:
        olca.Process with updated .tags
    """
    process.setdefault('tags', []).append(dpkg.name)  # .append(str(dpkg))
    return process


def build_db(
    build: Build,
    add_tags: bool = True,
):
    """
    Merge the set of objects from dpkg dependencies listed in the manifest TOML 
    into the build, while (1) avoiding duplicated-UUID collissions, and (2) 
    overwriting Ref pointers embedded in select objects to facilitate inter-dpkg
    linking and seamless DB import + usage in openLCA Desktop.
    
    Args:
        build: parsed olca.Process JSON
    Returns:
        olca.Process with updated .tags as single-line JSON
    """
    build.fetch_dependencies()
    
    PATH_OUT_ZIP = PATHS.output / f'{build.name}_v{build.version}.zip'
        
    dedup = get_dedup_config(build)
    provider_updates = prepare_provider_links(build)
    # TODO: orchestrate dropping of "pass-through" bridge processes in linking.py
        # compile relative file paths of bridge processes to ignore:
        # bridges_drop = [f'processes/{uuid}.json' for uuid in _new_linking_function()]
    
    # Compile FEDEFL elem. & USEEIO tech. flows; substitute for duplicates in other dpkgs
    def _get_dpkg_flows(dpkg: DataPackage) -> dict:
        return {metadata.filename: metadata for metadata, _ 
                in dpkg.iter_zip_jsons(metadata_only=True, subdir='flows')}
    
    flows_fedefl_elem, flows_useeio_tech = ({}, {})
    for dpkg in build.dependencies_all:
        match dpkg.name:
            case 'elementary_flow_list':
                flows_fedefl_elem = _get_dpkg_flows(dpkg)
                zip_fedefl = dpkg.zipfile
            case 'USEEIO_v2':
                flows_useeio_tech = _get_dpkg_flows(dpkg)
                zip_useeio = dpkg.zipfile
        
    if flows_useeio_tech and flows_fedefl_elem:
        for flow in flows_fedefl_elem.keys():
            flows_useeio_tech.pop(flow, None)
    
    # TODO: give same dedup priority to FEDEFL & USEEIO as direct dependencies
    with (zf.ZipFile(PATH_OUT_ZIP, 'w') as zip_build,
          zip_fedefl if flows_fedefl_elem else nullcontext(),
          zip_useeio if flows_useeio_tech else nullcontext()):
        files_written = set()
        for dpkg in build.dependencies:
            # copy JSONs from dpkg to build .ZIP
            for metadata, file_stream in dpkg.iter_zip_jsons(dedup=dedup):
                file_rpath = metadata.filename
                subdir = file_rpath.split('/')[0]
                # On-the-fly deduplication of already-written UUIDs:
                if file_rpath in files_written:
                    if not subdir in ['processes', 'flows']:
                        log.debug('Duplicate @type not addressed by deduplicate.yaml:'
                                  f'\n\t{dpkg.name}\n\t{file_rpath}')
                    elif (subdir == 'flows' and 
                          (file_rpath in flows_fedefl_elem or
                           file_rpath in flows_useeio_tech)):
                        pass
                    else:
                        log.warning('Duplicate not yet addressed by deduplicate.yaml:'
                                    f'\n\t{dpkg.name}\n\t{file_rpath}')
                # On-the-fly deduplication of objects from special-case dpkgs:
                elif (subdir == 'flows' and file_rpath in flows_fedefl_elem):
                    zip_build.writestr(flows_fedefl_elem[file_rpath], 
                                       zip_fedefl.read(file_rpath))
                    files_written.add(file_rpath)
                elif (subdir == 'flows' and file_rpath in flows_useeio_tech):
                    zip_build.writestr(flows_useeio_tech[file_rpath], 
                                       zip_useeio.read(file_rpath))
                    files_written.add(file_rpath)
                # elif file_rpath in bridges_drop:
                #     log.debug(f'Dropped bridge:\n\t{dpkg.name}\n\t{file_rpath}')
                #     pass
                else:
                    try:
                        if subdir == 'processes':
                            if not (add_tags or file_rpath in provider_updates):
                                _json: bytes = file_stream.read()
                            else:
                                process: dict = json.load(file_stream)
                                if file_rpath in provider_updates:
                                    process = update_provider_links(process,
                                                                    provider_updates[file_rpath])
                                if add_tags:
                                    process = add_dpkg_tag(process, dpkg)
                                _json: str = write_compact_json(process)
                        else:
                            _json: bytes = file_stream.read()
                        zip_build.writestr(metadata, _json)
                        files_written.add(file_rpath)
                    except:
                        log.exception(f'Bad write for {file_rpath} from {str(dpkg)}')
                        raise
    # TODO: write resolved/frozen/locked manifest TOML to PATHS.output
    print(f'\nWrote combined DB: {PATH_OUT_ZIP}')


def main() -> int:    
    build = Build.from_manifest_toml()
    build_db(build)
    return 0


# %%
if __name__ == '__main__':
    sys.exit(main())