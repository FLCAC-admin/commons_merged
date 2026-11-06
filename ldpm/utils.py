import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from platformdirs import user_data_dir

from ldpm import __path__ as root_path


# %% paths container
ROOT_PATH = Path(*root_path)

@dataclass
class Paths:
    root: Path = ROOT_PATH
    config: Path = ROOT_PATH / 'config'
    project: Path = ROOT_PATH.parent
    output: Path = ROOT_PATH.parent / 'output'
    cache: Path = field(init=False)
    
    def __post_init__(self) -> None:
        self.output.mkdir(parents=True, exist_ok=True)

        # Cache FLCAC data packages in ~/LDPM/FLCAC
            # on Windows, C:\Users\<user>\AppData\Local\LDPM\FLCAC
        self.cache = Path(user_data_dir(appauthor='LDPM', appname='FLCAC'))  
        self.cache.mkdir(parents=True, exist_ok=True)
        # TODO: split cache into FLCAC [dependencies] and <name?> for [external-dependencies]

PATHS = Paths()


# %% logging
LOGGING_CONFIG = {
    'version': 1,
    'disable_existing_loggers': False,
    'formatters': {
        'simple': {
            'format': '%(levelname)s - %(message)s',
            'datefmt': '%T',
        },
        'standard': {
            'format': '%(asctime)s [%(levelname)s] %(name)s - %(message)s',
            'datefmt': '%y-%m-%d %T',
        },
        'detailed': {
            'format': '%(asctime)s [%(levelname)s] %(name)s:%(lineno)s, in %(funcName)s - %(message)s',
            'datefmt': '%y-%m-%d %T',
        },
    },
    'handlers': {
        'console': {
            'class': 'logging.StreamHandler',
            'level': 'INFO',
            'formatter': 'simple',
        },
        'file_build': {
            'class': 'logging.FileHandler',
            'level': 'INFO',
            'formatter': 'standard',
            'filename': PATHS.output / 'build.log',
            'mode': 'w',  # overwrites file
        },
        'file_debug': {
            'class': 'logging.FileHandler',
            'level': 'DEBUG',
            'formatter': 'detailed',
            'filename': PATHS.output / '_debug.log',
        },
    },
    'root': {
        'handlers': ['console', 'file_build', 'file_debug'],
        'level': 'DEBUG',
    },
}


# %% helper funcs
def format_log_msg(text: list[str]):
    return '\n\t' + '\n\t'.join(text)


def find_file(file_name: str):
    """
    Attempt to find a single file within the project directory
    """
    log = logging.getLogger(__name__)
    file_paths = list(PATHS.project.rglob(file_name))
    if len(file_paths) == 0:
        msg = f'No "{file_name}" file detected in the repo.'
        log.error(msg)
        raise FileNotFoundError(msg)
    elif len(file_paths) > 1:
        printable_file_paths = [str(fp.relative_to(PATHS.project.parent)) 
                                for fp in file_paths]
        msg = format_log_msg(
            [f'Multiple files named "{file_name}" detected in the repo:',
             f'\t- {"\n\t\t- ".join(printable_file_paths)}',
             'Please use unique file names.'])
        log.error(msg)
        raise RuntimeError(msg)
    elif len(file_paths) == 1:
        file_path, = file_paths
        return file_path


# def get_datetime_from_unix(
#     time_stamp: int | float, 
#     time_stamp_in_milliseconds: bool = False,
#     time_zone: ZoneInfo | None = None,
# ) -> datetime:
#     """
#     Convert Unix timestamp (seconds or milliseconds) to datetime object, 
#     with time_zone defaulting to local machine's time zone
#     """
#     denominator = 1000.0 if time_stamp_in_milliseconds else 1.0
#     return datetime.fromtimestamp(time_stamp / denominator,  tz=time_zone).astimezone()


def merge_nested_dicts(target: Mapping, source: Mapping, same_shape=True, path=[]):
    """
    Nested merge of subdicts from source onto target; if same_shape=True, 
    raises error when trees have different shape/depth, as detected by trying 
    to merge Mapping & non-Mapping
    
    Example usage:
        # works:
        print(merge_nested_dicts({1:{"a":"A"},2:{"b":"B"}}, {2:{"b":"C"},3:{"d":"D"}}))
        print(merge_nested_dicts({1:{"a":"A"},2:{"b":"B"}}, {2:{"b":{"B":"B2"}},3:{"d":"D"}}, False))
        # encounters conflict:
        merge_nested_dicts({1:{"a":"A"},2:{"b":"B"}}, {1:{"a":"A"},2:{"b":{"B":"B2"}}})
    """
    result = target.copy()
    for key in source:
        if not key in result:
            result[key] = source[key]
        else:
            match [isinstance(x[key], Mapping) for x in (result, source)]:
                case [True, True]:
                    result[key] = merge_nested_dicts(result[key], source[key], same_shape, path + [str(key)])
                case [False, False]:
                    result[key] = source[key]
                case [True, False] | [False, True]:
                    if same_shape:
                        raise Exception('Conflict at ' + '.'.join(path + [str(key)]))
                    elif source[key] is not None:
                        result[key] = source[key]
    return result
