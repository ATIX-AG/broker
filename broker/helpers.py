"""Miscellaneous helpers live here"""
import json
import logging
import os
import sys
from collections import UserDict, namedtuple
from collections.abc import MutableMapping
from copy import deepcopy
from pathlib import Path

import yaml
from logzero import logger

from broker import exceptions, settings
from broker import logger as b_log

FilterTest = namedtuple("FilterTest", "haystack needle test")


def clean_dict(in_dict):
    """Remove entries from a dict where value is None"""
    return {k: v for k, v in in_dict.items() if v is not None}


def merge_dicts(dict1, dict2):
    """Merge two nested dictionaries together

    :return: merged dictionary
    """
    if not isinstance(dict1, MutableMapping) or not isinstance(dict2, MutableMapping):
        return dict1
    dict1 = clean_dict(dict1)
    dict2 = clean_dict(dict2)
    merged = {}
    dupe_keys = dict1.keys() & dict2.keys()
    for key in dupe_keys:
        merged[key] = merge_dicts(dict1[key], dict2[key])
    for key in dict1.keys() - dupe_keys:
        merged[key] = deepcopy(dict1[key])
    for key in dict2.keys() - dupe_keys:
        merged[key] = deepcopy(dict2[key])
    return merged


def flatten_dict(nested_dict, parent_key="", separator="_"):
    """Flatten a nested dictionary, keeping nested notation in key
    {
        'key': 'value1',
        'another': {
            'nested': 'value2',
            'nested2': [1, 2, {'deep': 'value3'}]
        }
    }
    becomes
    {
        "key": "value",
        "another_nested": "value2",
        "another_nested2": [1, 2],
        "another_nested2_deep": "value3"
    }
    note that dictionaries nested in lists will be removed from the list

    :return: dictionary
    """

    flattened = []
    for key, value in nested_dict.items():
        new_key = f"{parent_key}{separator}{key}" if parent_key else key
        if isinstance(value, dict):
            flattened.extend(flatten_dict(value, new_key, separator).items())
        elif isinstance(value, list):
            to_remove = []
            value = value.copy()  # avoid mutating nested structures
            for index, val in enumerate(value):
                if isinstance(val, dict):
                    flattened.extend(flatten_dict(val, new_key, separator).items())
                    to_remove.append(index)
            for index in to_remove:
                del value[index]
            flattened.append((new_key, value))
        else:
            flattened.append((new_key, value))
    return dict(flattened)


def classify_filter(filter_string):
    """Given a filter string, determine the filter action and components"""
    tests = {
        "!<": "'{needle}' not in '{haystack}'",
        "<": "'{needle}' in '{haystack}'",
        "!=": "'{haystack}' != '{needle}'",
        "=": "'{haystack}' == '{needle}'",
        "!{": "not '{haystack}'.startswith('{needle}')",
        "{": "'{haystack}'.startswith('{needle}')",
        "!}": "not '{haystack}'.endswith('{needle}')",
        "}": "'{haystack}'.endswith('{needle}')",
    }
    if "," in filter_string:
        return [classify_filter(f) for f in filter_string.split(",")]
    for cond, test in tests.items():
        if cond in filter_string:
            k, v = filter_string.split(cond)
            return FilterTest(haystack=k, needle=v, test=test)


def inventory_filter(inventory, raw_filter):
    """Filter out inventory items depending on the filter provided"""
    resolved_filter = classify_filter(raw_filter)
    if not isinstance(resolved_filter, list):
        resolved_filter = [resolved_filter]
    matching = []
    for host in inventory:
        flattened_host = flatten_dict(host, separator=".")
        eval_list = [
            eval(rf.test.format(haystack=flattened_host[rf.haystack], needle=rf.needle))
            for rf in resolved_filter
            if rf.haystack in flattened_host
        ]
        if eval_list and all(eval_list):
            matching.append(host)
    return matching


def results_filter(results, raw_filter):
    """Filter out a list of results depending on the filter provided"""
    resolved_filter = classify_filter(raw_filter)
    if not isinstance(resolved_filter, list):
        resolved_filter = [resolved_filter]
    matching = []
    for res in results:
        eval_list = [
            eval(rf.test.format(haystack=res, needle=rf.needle))
            for rf in resolved_filter
        ]
        if eval_list and all(eval_list):
            matching.append(res)
    return matching


def resolve_nick(nick):
    """Checks if the nickname exists. Used to define broker arguments

    :param nick: String representing the name of a nick

    :return: a dictionary mapping argument names and values
    """
    nick_names = settings.settings.get("NICKS") or {}
    if nick in nick_names:
        return settings.settings.NICKS[nick].to_dict()


def load_file(file):
    """Verifies existence and loads data from json and yaml files"""
    file = Path(file)
    if not file.exists() or file.suffix not in (".json", ".yaml", ".yml"):
        logger.warning(f"File {file.absolute()} is invalid or does not exist.")
        return []
    loader_args = {}
    if file.suffix == ".json":
        loader = json
    elif file.suffix in (".yaml", ".yml"):
        loader = yaml
        loader_args = {"Loader": yaml.FullLoader}
    with file.open() as f:
        data = loader.load(f, **loader_args) or []
    return data


def resolve_file_args(broker_args):
    """Check for files being passed in as values to arguments,
    then attempt to resolve them. If not resolved, keep arg/value pair intact.
    """
    final_args = {}
    for key, val in broker_args.items():
        if isinstance(val, Path) or (
            isinstance(val, str) and val[-4:] in ("json", "yaml", ".yml")
        ):
            if (data := load_file(val)) :
                if key == "args_file":
                    if isinstance(data, dict):
                        final_args.update(data)
                    elif isinstance(data, list):
                        for d in data:
                            final_args.update(d)
                else:
                    final_args[key] = data
            elif key == "args_file":
                raise exceptions.BrokerError(f"No data loaded from {val}")
            else:
                final_args[key] = val
        else:
            final_args[key] = val
    return final_args


def load_inventory(filter=None):
    """Loads all local hosts in inventory

    :return: list of dictionaries
    """
    inventory_file = settings.BROKER_DIRECTORY.joinpath(
        settings.settings.INVENTORY_FILE
    )
    inv_data = load_file(inventory_file)
    return inv_data if not filter else inventory_filter(inv_data, filter)


def update_inventory(add=None, remove=None):
    """Updates list of local hosts in the checkout interface

    :param add: list of dictionaries representing new hosts

    :param remove: list of strings representing hostnames or names to be removed

    :return: no return value
    """
    inventory_file = settings.BROKER_DIRECTORY.joinpath(
        settings.settings.INVENTORY_FILE
    )
    if add and not isinstance(add, list):
        add = [add]
    if remove and not isinstance(remove, list):
        remove = [remove]
    inv_data = load_inventory()
    if inv_data:
        inventory_file.unlink()

    if remove:
        for host in inv_data[::-1]:
            if host["hostname"] in remove or host["name"] in remove:
                inv_data.remove(host)
    if add:
        inv_data.extend(add)

    inventory_file.touch()
    with inventory_file.open("w") as inv_file:
        yaml.dump(inv_data, inv_file)


def yaml_format(in_struct):
    """Convert a yaml-compatible structure to a yaml dumped string

    :param in_struct: yaml-compatible structure or string containing structure

    :return: yaml-formatted string
    """
    if isinstance(in_struct, str):
        in_struct = yaml.load(in_struct, Loader=yaml.FullLoader)
    return yaml.dump(in_struct, default_flow_style=False, sort_keys=False)


class MockStub(UserDict):
    """Test helper class. Allows for both arbitrary mocking and stubbing"""

    def __init__(self, in_dict=None):
        """Initialize the class and all nested dictionaries"""
        if in_dict is None:
            in_dict = {}
        for key, value in in_dict.items():
            if isinstance(value, dict):
                setattr(self, key, MockStub(value))
            elif type(value) in (list, tuple):
                setattr(
                    self,
                    key,
                    [MockStub(x) if isinstance(x, dict) else x for x in value],
                )
            else:
                setattr(self, key, value)
        super().__init__(in_dict)

    def __getattr__(self, name):
        return self

    def __getitem__(self, key):
        item = getattr(self, key, self)
        try:
            item = super().__getitem__(key)
        except KeyError:
            pass
        return item

    def __call__(self, *args, **kwargs):
        return self


def update_log_level(ctx, param, value):
    silent = False
    if value == "silent":
        silent = True
        value = "info"
    if getattr(logging, value.upper()) is not logger.getEffectiveLevel() or silent:
        b_log.setup_logzero(level=value, silent=silent)
        if not silent:
            print(f"Log level changed to [{value}]")


def fork_broker():
    pid = os.fork()
    if pid:
        logger.info(f"Running broker in the background with pid: {pid}")
        sys.exit(0)
    update_log_level(None, None, "silent")


def handle_keyboardinterrupt(*args):
    choice = input(
        "\nEnding Broker while running won't end processes being monitored.\n"
        "Would you like to switch Broker to run in the background?\n"
        "[y/n]: "
    )
    if choice.lower()[0] == "y":
        fork_broker()
    else:
        raise exceptions.BrokerError("Broker killed by user.")