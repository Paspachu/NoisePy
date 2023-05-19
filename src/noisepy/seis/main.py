import argparse
import logging
import os
import typing
from enum import Enum
from typing import Any, Callable, List

import obspy
from datetimerange import DateTimeRange

from .asdfstore import ASDFCCStore, ASDFRawDataStore
from .channelcatalog import CSVChannelCatalog, XMLStationChannelCatalog
from .constants import DATE_FORMAT_HELP, STATION_FILE
from .datatypes import Channel, ConfigParameters
from .S0A_download_ASDF_MPI import download
from .S1_fft_cc_MPI import cross_correlate
from .S2_stacking import stack
from .scedc_s3store import SCEDCS3DataStore
from .utils import fs_join, get_filesystem

# Utility running the different steps from the command line. Defines the arguments for each step

default_data_path = "Documents/SCAL"


class Step(Enum):
    DOWNLOAD = 1
    CROSS_CORRELATE = 2
    STACK = 3
    ALL = 4


def valid_date(d: str) -> str:
    _ = obspy.UTCDateTime(d)
    return d


def initialize_fft_params(raw_dir: str) -> ConfigParameters:
    params = ConfigParameters()
    dfile = fs_join(raw_dir, "download_info.txt")
    if os.path.isfile(dfile):
        down_info = eval(open(dfile).read())  # TODO: do proper json/yaml serialization
        params.samp_freq = down_info["samp_freq"]
        params.freqmin = down_info["freqmin"]
        params.freqmax = down_info["freqmax"]
        params.start_date = down_info["start_date"]
        params.end_date = down_info["end_date"]
        params.inc_hours = down_info["inc_hours"]
        params.ncomp = down_info["ncomp"]
    return params


# TODO: Remove this and generalize to loading a single config file. Doing the simple thing
# since there's a refactoring of txt files in progress
def initialize_stack_params(ccf_dir: str) -> ConfigParameters:
    file = fs_join(ccf_dir, "fft_cc_data.txt")
    if os.path.isfile(file):
        params = eval(open(file).read())  # TODO: do proper json/yaml serialization
        return params

    return ConfigParameters()


def get_channel_filter(sta_list: List[str]) -> Callable[[Channel], bool]:
    if len(sta_list) == 1 and sta_list[0] == "*":
        return lambda ch: True
    else:
        stations = set(sta_list)
        return lambda ch: ch.station.name in stations


def get_date_range(args) -> DateTimeRange:
    if args.start is None or args.end is None:
        return None
    return DateTimeRange(obspy.UTCDateTime(args.start).datetime, obspy.UTCDateTime(args.end).datetime)


def create_raw_store(args):
    stations = args.stations if "stations" in args else []
    xml_path = args.xml_path if "xml_path" in args else None
    raw_dir = args.raw_data_path

    fs = get_filesystem(raw_dir)

    def count(pat):
        return len(fs.glob(fs_join(raw_dir, pat)))

    # Use heuristics around which store to use by the files present
    if count("*.h5") > 0:
        return ASDFRawDataStore(raw_dir)
    else:
        # assert count("*.ms") > 0 or count("*.sac") > 0, f"Can not find any .h5, .ms or .sac files in {raw_dir}"
        if xml_path is not None:
            catalog = XMLStationChannelCatalog(xml_path)
        elif os.path.isfile(os.path.join(raw_dir, STATION_FILE)):
            catalog = CSVChannelCatalog(raw_dir)
        else:
            raise ValueError(f"Either an --xml_path argument or a {STATION_FILE} must be provided")

        date_range = get_date_range(args)
        return SCEDCS3DataStore(raw_dir, catalog, get_channel_filter(stations), date_range)


def main(args: typing.Any):
    raw_dir = args.raw_data_path
    logger = logging.getLogger(__package__)
    logger.setLevel(args.loglevel.upper())

    def run_cross_correlation():
        ccf_dir = args.ccf_path
        fft_params = initialize_fft_params(raw_dir)
        fft_params.freq_norm = args.freq_norm
        cc_store = ASDFCCStore(ccf_dir)
        raw_store = create_raw_store(args)
        cross_correlate(raw_store, fft_params, cc_store)

    def run_download():
        params = ConfigParameters()
        params.start_date = args.start
        params.end_date = args.end
        params.inc_hours = args.inc_hours
        download(args.raw_data_path, args.channels, args.stations, params)

    def run_stack():
        params = initialize_stack_params(args.ccf_path)
        params.stack_method = args.method
        cc_store = ASDFCCStore(args.ccf_path, "r")
        stack(cc_store, args.stack_path, params)

    if args.step == Step.DOWNLOAD:
        run_download()
    if args.step == Step.CROSS_CORRELATE:
        run_cross_correlation()
    if args.step == Step.STACK:
        run_stack()
    if args.step == Step.ALL:
        run_download()
        run_cross_correlation()
        run_stack()


def add_date_args(parser, required):
    parser.add_argument(
        "--start",
        type=valid_date,
        required=required,
        help="Start date in the format: " + DATE_FORMAT_HELP,
    )
    parser.add_argument(
        "--end",
        type=valid_date,
        required=required,
        help="End date in the format: " + DATE_FORMAT_HELP,
    )


def add_download_args(down_parser: argparse.ArgumentParser):
    down_parser.add_argument(
        "--channels",
        type=lambda s: s.split(","),
        help="Comma separated list of channels",
        default="BHE,BHN,BHZ",
    )
    down_parser.add_argument("--inc_hours", type=int, default=24, help="Time increment size (hrs)")


def add_stations_arg(parser: argparse.ArgumentParser):
    parser.add_argument(
        "--stations",
        type=lambda s: s.split(","),
        help="Comma separated list of stations or '*' for all",
        default="*",
    )


def add_path(parser, prefix: str):
    parser.add_argument(
        f"--{prefix}_path",
        type=str,
        default=os.path.join(os.path.join(os.path.expanduser("~"), default_data_path), prefix.upper()),
        help=f"Directory for {prefix} data",
    )


def add_paths(parser, types: List[str]):
    for t in types:
        add_path(parser, t)


def add_cc_args(parser):
    parser.add_argument("--freq_norm", choices=["rma", "no", "phase_only"], default="rma")
    parser.add_argument("--xml_path", required=False, default=None)


def add_stack_args(parser):
    parser.add_argument(
        "--method",
        type=str,
        required=True,
        choices=[
            "linear",
            "pws",
            "robust",
            "nroot",
            "selective",
            "auto_covariance",
            "all",
        ],
        help="Stacking method",
    )


def make_step_parser(subparsers: Any, step: Step, parser_config_funcs: List[Callable[[argparse.ArgumentParser], None]]):
    parser = subparsers.add_parser(
        step.name.lower(),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    for config_fn in parser_config_funcs:
        config_fn(parser)
    parser.add_argument(
        "-log",
        "--loglevel",
        type=str.lower,
        default="info",
        choices=["notset", "debug", "info", "warning", "error", "critical"],
    )


def main_cli():
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="step", required=True)
    make_step_parser(
        subparsers,
        Step.DOWNLOAD,
        [lambda p: add_paths(p, ["raw_data"]), add_download_args, add_stations_arg, lambda p: add_date_args(p, True)],
    )
    make_step_parser(
        subparsers,
        Step.CROSS_CORRELATE,
        [lambda p: add_paths(p, ["raw_data", "ccf"]), add_cc_args, add_stations_arg, lambda p: add_date_args(p, False)],
    )
    make_step_parser(subparsers, Step.STACK, [lambda p: add_paths(p, ["raw_data", "stack", "ccf"]), add_stack_args])
    make_step_parser(
        subparsers,
        Step.ALL,
        [
            lambda p: add_paths(p, ["raw_data", "ccf", "stack"]),
            add_download_args,
            lambda p: add_date_args(p, True),
            add_cc_args,
            add_stack_args,
            add_stations_arg,
        ],
    )

    args = parser.parse_args()
    args.step = Step[args.step.upper()]
    main(args)


if __name__ == "__main__":
    main_cli()
