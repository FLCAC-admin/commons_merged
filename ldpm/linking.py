"""
Connect default providers across all processes within the build
"""
import logging

import msgspec
import pandas as pd
import yaml

from ldpm.utils import PATHS, format_log_msg, merge_nested_dicts


log = logging.getLogger(__name__)

def str_representer(dumper, data):
    """Format multi-line strings as YAML literal block"""
    style = '|' if len(data.splitlines()) > 1 else None
    return dumper.represent_scalar('tag:yaml.org,2002:str', data, style=style)

yaml.SafeDumper.add_representer(str, str_representer)


def df_to_nested_dict(df: pd.DataFrame, key_cols: tuple, value_col: str) -> dict:
    """
    Reshape a dataframe into a nested dict, with key_cols acting as the source
    of keys (in descending order) and value_col as the values
    """
    if not key_cols:
        return df[value_col].item() if len(df) == 1 else df[value_col].tolist()
    current_key = key_cols[0]
    remaining_key_cols = key_cols[1:]
    return {key: df_to_nested_dict(_df, remaining_key_cols, value_col)
            for key, _df in df.groupby(current_key)}


def load_partial_json_process(file_stream: bytes)-> dict:
    """
    Parse only needed fragments of olca process JSON
    
    Here and elsewhere, using olca-schema classes could enable DRYer code and
    cleaner .attribute indexing, but their .from_json and .to_json methods don't 
    preserve the original JSON key order, which adds an extra layer of changes
    to FLCAC JSONs modified during LDPM linking and obscures the actual diffs
    """
    class RefEntity(msgspec.Struct):
        id: str = msgspec.field(name="@id")
        name: str

    class Flow(RefEntity):
        flowType: str
        # category: str

    class Exchange(msgspec.Struct):
        flow: Flow
        internalId: int
        isInput: bool
        # location: RefEntity | None = None
        isQuantitativeReference: bool = False
        defaultProvider: RefEntity = msgspec.field(default_factory=dict)
        description: str | dict = ''

    class Process(RefEntity):
        category: str
        exchanges: list[Exchange]
        # location: RefEntity | None = None
        # description: str | dict = ''
        
    process = msgspec.json.decode(file_stream, type=Process)
    return msgspec.to_builtins(process)


def compile_exchange_table(build: 'Build') -> pd.DataFrame:
    """
    Extract and tidy all exchanges from all processes in Build.dependencies 
    """
    processes = []
    for dpkg in build.dependencies:
        for metadata, file_stream in dpkg.iter_zip_jsons(subdir='processes'):
            process = load_partial_json_process(file_stream.read())
            processes.append({**process, 'dpkg': dpkg})
    df_exchanges = (
        pd.json_normalize(data=processes,
                          record_path='exchanges',
                          record_prefix = 'e.',  # exchange
                          meta=['@id', 'name', 'dpkg', 'category'],
                          meta_prefix='p.',  # process
                          )
          .astype({col: 'category' for col in ['p.dpkg', 'e.flow.flowType']})
          .query('`e.flow.flowType` != "ELEMENTARY_FLOW"')
          .assign(**{
              'p.is_bridge': lambda _df: 
                  _df['p.category'].str.contains('Bridge Process', case=False),
              })
          .drop(columns='p.category')
        )
    # check for non-unique exchange.internalId values within each process
    fields_unique_exchange = ['p.@id', 'p.dpkg', 'e.internalId']
    bad_exchange_mask = df_exchanges.duplicated(subset=fields_unique_exchange, keep=False)
    if sum(bad_exchange_mask) > 0:
        df_exchanges_bad = df_exchanges[bad_exchange_mask][fields_unique_exchange + ['e.flow.@id', 'e.flow.name']]
        log.error('Non-unique exchange.internalId values detected:\n' +
                  str(df_exchanges_bad.to_dict(orient='records')))
    return df_exchanges


def classify_exchanges(df_exchanges: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Differentiate consumer from provider exchanges"""
    df_consumers = df_exchanges.query(
        'not `e.isQuantitativeReference` and '
        '((`e.flow.flowType` == "PRODUCT_FLOW" and `e.isInput`) or'
        ' (`e.flow.flowType` == "WASTE_FLOW" and not `e.isInput`))'
        )
    df_consumers_unlinked = df_consumers.query('`e.defaultProvider.@id`.isna()')
    # bridges should not act as providers for yet-unlinked flows
    df_providers = df_exchanges.query(
        'not `p.is_bridge` and '
        '((`e.flow.flowType` == "PRODUCT_FLOW" and not `e.isInput`) or'
        ' (`e.flow.flowType` == "WASTE_FLOW" and `e.isInput`))'
        )
    # validate expectation: remaining exchanges are only bridge-process outputs
    df_validate = df_exchanges.query('(`p.is_bridge` and `e.isQuantitativeReference`)')
    if not (len(df_exchanges) - len(df_consumers) - len(df_providers)) == len(df_validate):
        df_bad_exchanges = df_exchanges[~df_exchanges.index.isin(df_consumers.index) &
                                        ~df_exchanges.index.isin(df_providers.index) &
                                        ~df_exchanges.index.isin(df_validate.index)]
        bad_exchanges = df_bad_exchanges[['p.@id', 'e.internalId']].to_dict('records')
        log.warning(format_log_msg(
            ['Possibly invalid exchange patterns detected:',
             f'\t- {"\n\t\t- ".join([str(e) for e in bad_exchanges])}']))
        # capture exchange definition/orientation patterns that may disrupt linking
        df_odd_exchanges = df_exchanges.query(
            '(`e.flow.flowType` == "PRODUCT_FLOW" and `e.isInput` and `e.isQuantitativeReference`) or '
            '(`e.flow.flowType` == "WASTE_FLOW" and `e.isInput` and not `e.isQuantitativeReference`) or '
            '(`e.flow.flowType` == "WASTE_FLOW" and not `e.isInput` and `e.isQuantitativeReference`)'
            )
        odd_exchanges = df_odd_exchanges[['p.@id', 'e.internalId']].to_dict('records')
        log.debug(format_log_msg(
            ['Odd exchange patterns detected:',
             f'\t- {"\n\t\t- ".join([str(e) for e in odd_exchanges])}']))
    return (df_consumers_unlinked, df_providers)



def get_linking_config(build: 'Build') -> (dict, dict):
    """    
    Import provider_links.yaml; return as-is and as reindexed by
    .zip-relative file paths ("rpath")
    """
    dpkgs_build = {dpkg.name for dpkg in build.dependencies}
    with (PATHS.config / 'provider_links.yaml').open() as file:
        links_config = yaml.safe_load(file)
        links_rpath_config = {f'processes/{process_id}.json': exchange_dict
                              for dpkg_name, process_dict in links_config.items()
                              if dpkg_name in dpkgs_build and process_dict is not None
                              for process_id, exchange_dict in process_dict.items()}
    return links_config, links_rpath_config


def assign_providers(
    df_consumers_unlinked: pd.DataFrame,
    df_providers: pd.DataFrame,
    build: 'Build',
) -> pd.DataFrame:
    """
    Assign e.defaultProvider.@id to consumer exchanges automatically:
        1. extract and use JSON pointers from e.description on bridges
        2. assign 'solo' providers (only one provider per flow)
    
    Then compile candidate multi-providers (2+ providers yield same flow) to be
    linked manually, by reviewing _provider_links_template.yaml, selecting a 
    provider UUID, and copying those YAML fragments into provider_links.yaml
    """
    mask_multi_provider_flows = df_providers.duplicated(subset=['e.flow.@id'], keep=False)
    df_providers_solo = df_providers.loc[~mask_multi_provider_flows]
    map_solo_providers = {flow_id: provider_id for flow_id, provider_id in 
                          zip(df_providers_solo['e.flow.@id'], df_providers_solo['p.@id'])}
    df_consumers_linked_auto = (
        df_consumers_unlinked
        .assign(**{
            'e.defaultProvider.@id': lambda _df:  # link bridges w/ e.description JSON pointers
                _df['e.description'].str.extract(r'^{.*"defaultProvider.@id":\s*"(.+)"'), 
            })  # TODO: if additional field/s needed, json.loads(_df['e.description']) 
        .assign(**{
            'e.defaultProvider.@id': lambda _df:  # then link solo providers (incl. USEEIO bridges)
                _df['e.defaultProvider.@id'].fillna(_df['e.flow.@id'].map(map_solo_providers)),
            'file_rpath': lambda _df: 'processes/' + _df['p.@id'] + '.json'
            })
        .dropna(subset='e.defaultProvider.@id')
        .filter(['file_rpath', 'e.flow.@id', 'e.internalId', 'e.defaultProvider.@id'])
        )
    # compile auto-linked providers & merge provider_links.yaml contents (higher precedence) onto it
    links_rpath_auto = df_to_nested_dict(
        df_consumers_linked_auto,
        key_cols=['file_rpath', 'e.flow.@id', 'e.internalId'], 
        value_col='e.defaultProvider.@id',
        )
    links_config, links_rpath_config = get_linking_config(build)        
    links_rpath_all = merge_nested_dicts(links_rpath_auto, links_rpath_config)
    
    # compile dict of provider recommendations (recs) for remaining linkable consumers
    df_providers_multi = df_providers.loc[mask_multi_provider_flows]
    df_consumers_unlinked_multi = (
        df_consumers_unlinked[
            ~df_consumers_unlinked.index.isin(df_consumers_linked_auto.index) &
            df_consumers_unlinked['e.flow.@id'].isin(df_providers_multi['e.flow.@id'])
            ]
        .drop(columns=['e.isQuantitativeReference', 'e.defaultProvider.@id'])
        )
    providers_multi_candidates = (
        df_providers_multi[df_providers_multi['e.flow.@id']
                           .isin(df_consumers_unlinked_multi['e.flow.@id'])]
        .query('not `e.flow.@id` == "3bca3bc6-2443-3184-8976-72dc98d258f6"')
            # too many electricity providers; drop here & add placeholder below
        .assign(provider_candidate=lambda _df: _df['p.dpkg'].astype(str) + ': ' + _df['p.@id'])
        # TODO: [later] sort candidates, putting same-dpkg providers first
        .groupby('e.flow.@id')['provider_candidate'].agg(lambda x: 'Please select a provider:\n' + '\n'.join(x))
        .to_dict()
        )
    providers_multi_candidates |= {'3bca3bc6-2443-3184-8976-72dc98d258f6':
                                       'Please Select a Provider:\nUS_electricity_baseline: many'}
    links_recommended = (
        df_consumers_unlinked_multi
        .assign(provider_candidates=lambda _df: _df['e.flow.@id'].map(providers_multi_candidates),
                dpkg_name=lambda _df: _df['p.dpkg'].apply(lambda _dpkg: _dpkg.name),
                )
        .pipe(df_to_nested_dict,
              key_cols=['dpkg_name', 'p.@id', 'e.flow.@id', 'e.internalId'], 
              value_col='provider_candidates')
        )
    # merge provider_links.yaml onto links_recommended to denote predetermined links
    links_recommended_template = merge_nested_dicts(links_recommended, links_config, False)
    with (PATHS.config / '_provider_links_template.yaml').open('w') as file:
        yaml.safe_dump(links_recommended_template, file, default_flow_style=False)
    return links_rpath_all


def prepare_provider_links(build: 'Build') -> dict:
    df_exchanges = compile_exchange_table(build)
    df_consumers_unlinked, df_providers = classify_exchanges(df_exchanges)
    links_rpath_all = assign_providers(df_consumers_unlinked, df_providers, build)
    return links_rpath_all


# %% [later] attempt linking on p.location and/or e.location (once populated)

# use (p.@id, p.location.@id) as key for provider mapping
    # or, instead of p.location.@id, assign a match index 
    # using *.location.name similarity (equality, fuzzy strings, etc.)

# temp = (df_providers_multi
#         .assign(**{'p.location.name': lambda _df: _df['p.location'].str.get('name')})
#         )
# temp2 = temp[['e.flow.@id', 'p.location.name', 'p.dpkg']].value_counts()
# temp2 = temp2[temp2 > 0]

# %% [later] add (drop_bridges: bool) arg to prepare_provider_links()
    # if drop_bridges=True, return:
        # <bridge process files to ignore>: list ~= [f'processes/{id}.json' for id in bridges_to_drop]
        # <exchange alterations>: dict ~= {'file_rpath': {'exchange.internalID': {'flow': {'@id': 'foo'}, 'defaultProvider': {'@id': 'foo'}}}}
            # bridge's input defaultProvider.@id & flow.@id overwrite attrs on non-bridge-process exchanges w/ that bridge as defaultProvider
    
        # identify droppable/ignorable bridges: must be "pass-through" processes
            # 1 input, 1 output; same exchange.amount and exchange.unit on each
            # same (or similar?) flow identity: {strict: same .@id, lax: same-ish .name}
            # easiest approach: flag bridge as droppable in JSON embedded on process.description
            # never to/from USEEIO
    
        # [assign]: e.defaultProvider_is_bridge_to_USEEIO
            # via df['e.defaultProvider.@id'].isin(df[df['p.is_bridge_to_USEEIO']['p.@id'])
        # where e.defaultProvider_is_bridge and not e.defaultProvider_is_bridge_to_USEEIO:
            # [assign]: `e.bridgeProvider_passthru` for bridges with 1 input & 1 output
                # if e.bridgeProvider_passthru: 
                    # replace e.flow.@id + e.defaultProvider.@id w/ that of bridge input
                # else:
                    #  [no action] where N:1, bridge is actually a mixing process

# df_bridges = df_exchanges.query('`p.is_bridge`')
# df_exchanges = (df_exchanges
#         .assign(**{
#             'e.defaultProvider.is_bridge': lambda _df: 
#                 _df['e.defaultProvider.@id'].isin(df_bridges['p.@id']),
#                 })
#         )
# df_bridge_consumers = (
#     df_consumers_unlinked
#     .query('`p.is_bridge`')
#     .assign(**{'e.defaultProvider.dpkg_alias': lambda _df:
#                    _df['p.name'].str.rsplit(r' to ', n=1).str[-1].str.strip(),
#                # 'p.dpkg_name': lambda _df: _df['p.dpkg'].apply(lambda _dpkg: _dpkg.name),
#                'p.is_bridge_to_USEEIO': lambda _df:
#                    _df['e.defaultProvider.dpkg_alias'] == "USEEIO",
#                })
#     )
#     # .assign(**df_bridge['p.name'].str.extract(
#     #     r'; (?P<validate_p_dpkg>.+) to (?P<defaultProvider_dpkg>.+)$'))

# [validate]: need dict mapping to infer dpkg.name from e.defaultProvider.dpkg_alias
    # and/or standardize the use of dpkg.name in bridge p.name 
    # (i.e., in "...; <dpkg.name> to <dpkg.name>")
