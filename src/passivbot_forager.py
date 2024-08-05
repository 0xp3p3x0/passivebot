import os

if "NOJIT" not in os.environ:
    os.environ["NOJIT"] = "true"

import traceback
import argparse
import asyncio
import json
import hjson
import pprint
import numpy as np
import passivbot_rust as pbr
from prettytable import PrettyTable
from uuid import uuid4
from copy import deepcopy
from collections import defaultdict
from sortedcontainers import SortedDict

from procedures import (
    load_broker_code,
    load_user_info,
    utc_ms,
    make_get_filepath,
    load_live_config,
    get_file_mod_utc,
    get_first_ohlcv_timestamps,
    load_config,
)
from njit_funcs_recursive_grid import calc_recursive_entries_long, calc_recursive_entries_short
from njit_funcs import (
    calc_samples,
    calc_emas_last,
    calc_ema,
    calc_close_grid_long,
    calc_close_grid_short,
    calc_diff,
    qty_to_cost,
    cost_to_qty,
    calc_min_entry_qty,
    round_,
    round_up,
    round_dn,
    round_dynamic,
    calc_pnl,
    calc_pnl_long,
    calc_pnl_short,
    calc_pprice_diff,
)
from njit_multisymbol import calc_AU_allowance
from pure_funcs import (
    numpyize,
    denumpyize,
    filter_orders,
    multi_replace,
    shorten_custom_id,
    determine_side_from_order_tuple,
    str2bool,
    symbol_to_coin,
    add_missing_params_to_hjson_live_multi_config,
    expand_PB_mode,
    ts_to_date_utc,
)

import logging

logging.basicConfig(
    format="%(asctime)s %(levelname)-8s %(message)s",
    level=logging.INFO,
    datefmt="%Y-%m-%dT%H:%M:%S",
)


class Passivbot:
    def __init__(self, config: dict):
        self.config = config
        self.user = config["live"]["user"]
        self.user_info = load_user_info(self.user)
        self.exchange = self.user_info["exchange"]
        self.broker_code = load_broker_code(self.user_info["exchange"])
        self.custom_id_max_length = 36
        self.sym_padding = 17
        self.stop_websocket = False
        self.balance = 1e-12
        self.upd_timestamps = {
            "pnls": 0.0,
            "open_orders": 0.0,
            "positions": 0.0,
            "tickers": 0.0,
        }
        self.hedge_mode = True
        self.inverse = False
        self.active_symbols = []
        self.fetched_positions = []
        self.fetched_open_orders = []
        self.open_orders = {}
        self.positions = {}
        self.pnls = []
        self.tickers = {}
        self.symbol_ids = {}
        self.min_costs = {}
        self.min_qtys = {}
        self.qty_steps = {}
        self.price_steps = {}
        self.c_mults = {}
        self.max_leverage = {}
        self.live_configs = {}
        self.stop_bot = False
        self.pnls_cache_filepath = make_get_filepath(f"caches/{self.exchange}/{self.user}_pnls.json")
        self.ohlcvs_cache_dirpath = make_get_filepath(f"caches/{self.exchange}/ohlcvs/")
        self.previous_execution_ts = 0
        self.recent_fill = False
        self.execution_delay_millis = max(
            3000.0, self.config["live"]["execution_delay_seconds"] * 1000
        )
        self.force_update_age_millis = 60 * 1000  # force update once a minute
        self.quote = "USDT"
        self.forager_mode = self.is_forager_mode()

        self.minimum_market_age_millis = (
            self.config["common"]["minimum_market_age_days"] * 24 * 60 * 60 * 1000
        )
        self.ohlcvs = {}
        self.ohlcv_upd_timestamps = {}
        self.emas = {"long": {}, "short": {}}
        self.ema_alphas = {"long": {}, "short": {}}
        self.upd_minute_emas = {}
        self.ineligible_symbols_with_pos = set()

    async def update_market_info(self):
        logging.info(f"initiating markets...")
        await self.init_markets_dict()

    async def update_hlcs_15m(self):
        # update 15m hlcs for all eligible symbols
        if not hasattr(self, "update_hlcs_15m_verbose"):
            self.update_hlcs_15m_verbose = True
        all_symbols = sorted(set(self.eligible_symbols) | set(self.active_symbols))
        await self.update_ohlcvs_multi(
            all_symbols,
            verbose=self.update_hlcs_15m_verbose,
        )
        try:
            # update one hlc15m each round
            sleep_interval_sec = max(5.0, (60.0 * 60.0) / len(all_symbols))
            symbol = self.get_oldest_updated_ohlcv_symbol()
            res = await self.update_ohlcvs_single(symbol, age_limit_ms=(sleep_interval_sec * 1000))
        except:
            pass
        self.update_hlcs_15m_verbose = False

    async def update_EMAs(self):
        # update EMAs for all eligible symbols
        if not hasattr(self, "update_EMAs_verbose"):
            self.update_EMAs_verbose = True
        await self.update_EMAs_multi(
            sorted(set(self.eligible_symbols) | set(self.active_symbols)),
            verbose=self.update_EMAs_verbose,
        )
        self.update_EMAs_verbose = False

    async def start_data_maintainers(self):
        # maintains market info, hlcs_15m, EMAs and pnls
        self.maintainers = {
            "maintain_market_info": None,
            "maintain_hlcs_15m": None,
            "maintain_EMAs": None,
        }
        self.last_update_timestamps = {
            key.replace("maintain", "update"): 0.0 for key in self.maintainers
        }
        self.update_intervals = {key: 1000 * 60 for key in self.last_update_timestamps}
        self.update_intervals["update_market_info"] = 1000 * 60 * 60
        self.update_intervals["update_EMAs"] = 1000 * 60 * 30
        await self.update_market_info()  # needs to update market info first
        self.last_update_timestamps["update_market_info"] = utc_ms()
        for key in [
            "update_pnls",
            "update_tickers",
            "update_positions",
            "update_open_orders",
            "update_hlcs_1m",
        ]:  # these are initiated here, then updated just before executing to exchange once a minute
            result = await getattr(self, key)()
        for key in self.maintainers:
            if self.maintainers[key] is None:
                function_name = key.replace("maintain", "update")
                if self.last_update_timestamps[function_name] == 0.0:
                    result = await getattr(self, function_name)()
                    self.last_update_timestamps[function_name] = utc_ms()
                self.maintainers[key] = asyncio.create_task(self.maintain_thing(key))

    async def maintain_thing(self, maintainer_name):
        logging.info(f"starting {maintainer_name}")
        if hasattr(self, maintainer_name):
            await getattr(self, maintainer_name)()
        else:
            function_name = maintainer_name.replace("maintain", "update")
            while True:
                try:
                    if (
                        utc_ms() - self.last_update_timestamps[function_name]
                        > self.update_intervals[function_name]
                    ):
                        self.last_update_timestamps[function_name] = utc_ms()
                        # logging.info(f"calling {function_name}")
                        result = await getattr(self, function_name)()
                    await asyncio.sleep(0.1)
                except Exception as e:
                    logging.error(f"error with {maintainer_name} {e}")
                    traceback.print_exc()
                    await asyncio.sleep(5.0)

    async def run_execution_loop(self):
        # simulates backtest which executes once every 1m
        prev_minute = utc_ms() // (1000 * 60)
        while True:
            try:
                now_minute = utc_ms() // (1000 * 60)
                if now_minute != prev_minute:
                    prev_minute = now_minute
                    await self.execute_to_exchange()
                await asyncio.sleep(0.1)
            except Exception as e:
                logging.error(f"error with run_execution_loop {e}")
                traceback.print_exc()
                await asyncio.sleep(5.0)

    async def prepare_for_execution(self):
        await asyncio.gather(
            self.update_open_orders(),
            self.update_positions(),
            self.update_pnls(),
            self.update_exchange_configs(),
        )
        await asyncio.gather(
            self.update_hlcs_1m(),
            self.update_tickers(),
        )
        self.update_PB_modes()

    async def execute_to_exchange(self):
        await self.prepare_for_execution()
        to_cancel, to_create = self.calc_orders_to_cancel_and_create()

        # debug duplicates
        seen = set()
        for elm in to_cancel:
            key = str(elm["price"]) + str(elm["qty"])
            if key in seen:
                logging.info(f"debug duplicate order cancel {elm}")
            seen.add(key)

        seen = set()
        for elm in to_create:
            key = str(elm["price"]) + str(elm["qty"])
            if key in seen:
                logging.info(f"debug duplicate order create {elm}")
            seen.add(key)

        # format custom_id
        to_create = self.format_custom_ids(to_create)
        res = await self.execute_cancellations(
            to_cancel[: self.config["live"]["max_n_cancellations_per_batch"]]
        )
        if res:
            for elm in res:
                self.remove_cancelled_order(elm, source="POST")
        res = await self.execute_orders(to_create[: self.config["live"]["max_n_creations_per_batch"]])
        if res:
            for elm in res:
                self.add_new_order(elm, source="POST")

    def is_forager_mode(self):
        n_approved_symbols = len(self.config["common"]["approved_symbols"])
        if n_approved_symbols == 0:
            return True
        if (
            self.config["bot"]["long"]["total_wallet_exposure_limit"] > 0.0
            and self.config["bot"]["long"]["n_positions"] > 0
            and round(self.config["bot"]["long"]["n_positions"]) < n_approved_symbols
        ):
            return True
        if (
            self.config["bot"]["short"]["total_wallet_exposure_limit"] > 0.0
            and self.config["bot"]["short"]["n_positions"] > 0
            and round(self.config["bot"]["short"]["n_positions"]) < n_approved_symbols
        ):
            return True
        return False

    def set_live_configs(self):
        self.config["bot"]["long"]["n_positions"] = round(self.config["bot"]["long"]["n_positions"])
        self.config["bot"]["short"]["n_positions"] = round(self.config["bot"]["short"]["n_positions"])
        for symbol in self.markets_dict:
            self.live_configs[symbol] = deepcopy(self.config["bot"])
            self.live_configs[symbol]["leverage"] = self.config["live"]["leverage"]
            for pside in ["long", "short"]:
                self.live_configs[symbol][pside]["wallet_exposure_limit"] = (
                    (
                        self.live_configs[symbol][pside]["total_wallet_exposure_limit"]
                        / self.live_configs[symbol][pside]["n_positions"]
                    )
                    if self.live_configs[symbol][pside]["n_positions"] > 0
                    else 0.0
                )
        return
        # live configs priority:
        # 1) -lc path from hjson multi config
        # 2) live config from live configs dir, matching name or coin
        # 3) live config from default config path
        # 4) universal live config given in hjson multi config

        if os.path.isdir(self.config["live_configs_dir"]):
            # live config candidates from live configs dir
            live_configs_fnames = sorted(
                [f for f in os.listdir(self.config["live_configs_dir"]) if f.endswith(".json")]
            )
        else:
            live_configs_fnames = []
        configs_loaded = {
            "lc_flag": set(),
            "live_configs_dir_exact_match": set(),
            "default_config_path": set(),
            "universal_config": set(),
        }
        for symbol in self.markets_dict:
            # try to load live config: 1) -lc flag live_config_path, 2) live_configs_dir, 3) default_config_path, 4) universal live config
            try:
                self.live_configs[symbol] = load_live_config(self.flags[symbol].live_config_path)
                configs_loaded["lc_flag"].add(symbol)
                continue
            except:
                pass
            try:
                # look for an exact match first
                coin = symbol_to_coin(symbol)
                for x in live_configs_fnames:
                    if coin == symbol_to_coin(x.replace(".json", "")):
                        self.live_configs[symbol] = load_live_config(
                            os.path.join(self.config["live_configs_dir"], x)
                        )
                        configs_loaded["live_configs_dir_exact_match"].add(symbol)
                        break
                else:
                    raise Exception
                continue
            except:
                pass
            try:
                self.live_configs[symbol] = load_live_config(self.config["default_config_path"])
                configs_loaded["default_config_path"].add(symbol)
                continue
            except:
                pass
            try:
                self.live_configs[symbol] = deepcopy(self.config["universal_live_config"])
                configs_loaded["universal_config"].add(symbol)
                continue
            except Exception as e:
                logging.error(f"failed to apply universal_live_config {e}")
                raise Exception(f"no usable live config found for {symbol}")
        for symbol in self.live_configs:
            if symbol in self.flags and self.flags[symbol].leverage is not None:
                self.live_configs[symbol]["leverage"] = max(1.0, float(self.flags[symbol].leverage))
            else:
                self.live_configs[symbol]["leverage"] = max(1.0, float(self.config["leverage"]))

            for pside in ["long", "short"]:
                # disable timed AU and set backwards TP
                for key, val in [
                    ("auto_unstuck_delay_minutes", 0.0),
                    ("auto_unstuck_qty_pct", 0.0),
                    ("auto_unstuck_wallet_exposure_threshold", 0.0),
                    ("auto_unstuck_ema_dist", 0.0),
                    ("backwards_tp", True),
                    ("wallet_exposure_limit", 0.0),
                ]:
                    self.live_configs[symbol][pside][key] = val
        for key in configs_loaded:
            if isinstance(configs_loaded[key], dict):
                for symbol in configs_loaded[key]:
                    logging.info(
                        f"loaded {key} for {self.pad_sym(symbol)}: {configs_loaded[key][symbol]}"
                    )
            elif isinstance(configs_loaded[key], set):
                coins_ = sorted([symbol_to_coin(s) for s in configs_loaded[key]])
                if len(coins_) > 20:
                    logging.info(f"loaded from {key} for {len(coins_)} symbols")
                elif len(coins_) > 0:
                    logging.info(f"loaded from {key} for {', '.join(coins_)}")

    def pad_sym(self, symbol):
        return f"{symbol: <{self.sym_padding}}"

    async def start_data_maintainers_old(self):
        if not hasattr(self, "maintainers"):
            self.maintainers = {}
        else:
            self.stop_data_maintainers()
        if self.forager_mode:
            self.maintainers["ohlcvs"] = asyncio.create_task(self.maintain_ohlcvs())
        else:
            self.maintainers["ohlcvs"] = None
        self.maintainers["EMAs"] = asyncio.create_task(self.maintain_EMAs_old())
        self.maintainers["hlcs"] = asyncio.create_task(self.maintain_hlcs())

    def stop_data_maintainers(self):
        if not hasattr(self, "maintainers"):
            return
        res = {}
        for key in self.maintainers:
            try:
                res[key] = self.maintainers[key].cancel()
            except Exception as e:
                logging.error(f"error stopping maintainer {key} {e}")
        logging.info(f"stopped data maintainers: {res}")
        return res

    def has_position(self, symbol, pside=None):
        if pside is None:
            return self.has_position(symbol, "long") or self.has_position(symbol, "short")
        return symbol in self.positions and self.positions[symbol][pside]["size"] != 0.0

    def is_trailing(self, symbol, pside=None):
        if pside is None:
            return self.is_trailing(symbol, "long") or self.is_trailing(symbol, "short")
        return symbol in self.live_configs and any(
            [
                self.live_configs[symbol][pside][f"{x}_trailing_grid_ratio"] != 0.0
                for x in ["entry", "close"]
            ]
        )

    def update_hlc_ws(self, symbol, bid, ask, timestamp):
        return
        # update hlc with latest price from WS
        # hlc format [[timestamp_ms, high, low, close], ...]
        if not hasattr(self, "hlcs"):
            self.hlcs = {}
        mid = (bid + ask) / 2.0
        now_minute = int(round(timestamp // 60000 * 60000))
        if symbol in self.hlcs:
            while now_minute > self.hlcs[symbol][-1][0]:
                did_change = True
                # fill gaps if any and start new hlc
                self.hlcs[symbol].append(
                    [
                        self.hlcs[symbol][-1][0] + 60000,
                        self.hlcs[symbol][-1][3],
                        self.hlcs[symbol][-1][3],
                        self.hlcs[symbol][-1][3],
                    ]
                )
            if (new_high := round_(ask, self.price_steps[symbol])) > self.hlcs[symbol][-1][1]:
                self.hlcs[symbol][-1][1] = new_high
            if (new_low := round_(bid, self.price_steps[symbol])) < self.hlcs[symbol][-1][2]:
                self.hlcs[symbol][-1][2] = new_low
            if (new_close := round_(mid, self.price_steps[symbol])) != self.hlcs[symbol][-1][3]:
                self.hlcs[symbol][-1][3] = new_close
        else:
            self.hlcs[symbol] = [
                [
                    now_minute,
                    round_(ask, self.price_steps[symbol]),
                    round_(bid, self.price_steps[symbol]),
                    round_(mid, self.price_steps[symbol]),
                ]
            ]

    async def update_hlcs_1m(self, n_fetches=10, verbose=False):
        if not hasattr(self, "hlcs_1m"):
            self.hlcs_1m = {}
        last_pos_changes = self.get_last_position_changes()
        symsince = [(s, min(last_pos_changes[s].values()) - 1000 * 60 * 60) for s in last_pos_changes]
        all_res = []
        for sym_sublist in [symsince[i : i + n_fetches] for i in range(0, len(symsince), n_fetches)]:
            try:
                res = await asyncio.gather(
                    *[self.update_hlcs_1m_single(sym, sn) for sym, sn in sym_sublist]
                )
                if verbose:
                    if any(res):
                        logging.info(
                            f"updated hlcs1m for {','.join([symbol_to_coin(s) for s, r in zip(sym_sublist, res) if r])}"
                        )
                all_res += res
            except Exception as e:
                logging.error(f"error in update_hlcs {sym_sublist} {e}")
        return all_res

    def get_last_position_changes(self, symbol=None):
        last_position_changes = defaultdict(dict)
        for symbol in self.positions:
            for pside in ["long", "short"]:
                if self.has_position(symbol, pside) and self.is_trailing(symbol, pside):
                    last_position_changes[symbol][pside] = utc_ms() - 1000 * 60 * 60 * 24 * 7
                    for fill in self.pnls[::-1]:
                        if fill["symbol"] == symbol and fill["position_side"] == pside:
                            last_position_changes[symbol][pside] = fill["timestamp"]
                            break
        return last_position_changes

    async def init_hlcs_1m(self):
        # fetch latest hlcs_1m for all eligible symbols
        # to be called after starting websocket
        min_n_candles = self.config["common"]["noisiness_rolling_mean_window_size"]
        sleep_time_seconds = 20 / len(self.eligible_symbols)
        tasks = {}
        symbols = sorted(self.eligible_symbols)
        logging.info(f"initiating hlcs_1m for {','.join([symbol_to_coin(s) for s in symbols])}")
        for symbol in symbols:
            tasks[symbol] = asyncio.create_task(self.update_hlcs_1m_single_new(symbol))
            await asyncio.sleep(sleep_time_seconds)
            print("init_hlcs_1m", symbol)
        for symbol in tasks:
            await tasks[symbol]

    async def update_hlcs_1m_single_new(self, symbol, since=None):
        if symbol in self.hlcs_1m and self.hlcs_1m[symbol]:
            candles = await self.fetch_hlcs_1m(symbol)
        else:
            self.hlcs_1m[symbol] = SortedDict()
            candles = await self.fetch_hlcs_1m(symbol, since)
        for x in candles:
            self.hlcs_1m[symbol][x[0]] = x
        if candles:
            logging.info(f"updated hlcs_1m for {symbol} since {ts_to_date_utc(candles[0][0])}")
        while len(self.hlcs_1m[symbol]) > 10080:
            del self.hlcs_1m[symbol][self.hlcs_1m[symbol].peekitem(0)]

    async def update_hlcs_1m_single(self, symbol, since):
        if symbol in self.hlcs_1m and self.hlcs_1m[symbol]:
            candles = await self.fetch_hlcs_1m(symbol)
        else:
            self.hlcs_1m[symbol] = []
            candles = await self.fetch_hlcs_1m(symbol, since)
        all_candles_d = {x[0]: x for x in self.hlcs_1m[symbol]}
        all_candles_d.update({x[0]: x for x in candles})
        logging.info(f"updated hlcs_1m for {symbol} since {ts_to_date_utc(since)}")
        self.hlcs_1m[symbol] = sorted(all_candles_d.values(), key=lambda x: x[0])
        if since is not None:
            self.hlcs_1m[symbol] = [
                x for x in self.hlcs_1m[symbol] if x[0] >= since - 1000 * 60 * 60 * 3
            ]
            if not self.hlcs_1m[symbol]:
                logging.info(f"debug hlcs_1m is empty for {symbol}")

    def update_trailing_data(self):
        if not hasattr(self, "trailing_prices"):
            self.trailing_prices = {}
        last_position_changes = self.get_last_position_changes()
        symbols = set(self.trailing_prices) | set(last_position_changes) | set(self.active_symbols)
        for symbol in symbols:
            self.trailing_prices[symbol] = {
                "long": {
                    "max_since_open": 0.0,
                    "min_since_max": np.inf,
                    "min_since_open": np.inf,
                    "max_since_min": 0.0,
                },
                "short": {
                    "max_since_open": 0.0,
                    "min_since_max": np.inf,
                    "min_since_open": np.inf,
                    "max_since_min": 0.0,
                },
            }
            if symbol not in last_position_changes:
                continue
            for pside in last_position_changes[symbol]:
                if symbol not in self.hlcs_1m:
                    logging.info(f"debug: {symbol} missing from self.hlcs_1m")
                    continue
                for x in self.hlcs_1m[symbol]:
                    if x[0] <= last_position_changes[symbol][pside]:
                        continue
                    if x[1] > self.trailing_prices[symbol][pside]["max_since_open"]:
                        self.trailing_prices[symbol][pside]["max_since_open"] = x[1]
                        self.trailing_prices[symbol][pside]["min_since_max"] = x[3]
                    else:
                        self.trailing_prices[symbol][pside]["min_since_max"] = min(
                            self.trailing_prices[symbol][pside]["min_since_max"], x[2]
                        )
                    if x[2] < self.trailing_prices[symbol][pside]["min_since_open"]:
                        self.trailing_prices[symbol][pside]["min_since_open"] = x[2]
                        self.trailing_prices[symbol][pside]["max_since_min"] = x[3]
                    else:
                        self.trailing_prices[symbol][pside]["max_since_min"] = max(
                            self.trailing_prices[symbol][pside]["max_since_min"], x[1]
                        )

    async def init_bot(self):
        logging.info(f"setting hedge mode...")
        await self.update_exchange_config()
        logging.info(f"initiating markets...")
        await self.init_markets_dict()
        await self.init_flags()
        logging.info(f"initiating tickers...")
        await self.update_tickers()
        logging.info(f"initiating balance, positions...")
        await self.update_positions()
        logging.info(f"initiating open orders...")
        await self.update_open_orders()
        self.set_live_configs()
        if self.forager_mode:
            await self.update_ohlcvs_multi(list(self.eligible_symbols), verbose=True)
        logging.info(f"initiating pnl history...")
        await self.update_pnls()
        await self.update_hlcs()
        self.update_PB_modes()
        await self.update_exchange_configs()

    async def get_active_symbols(self):
        # get symbols with open orders and/or positions
        positions, balance = await self.fetch_positions()
        open_orders = await self.fetch_open_orders()
        return sorted(set([elm["symbol"] for elm in positions + open_orders]))

    def format_symbol(self, symbol: str) -> str:
        try:
            return self.formatted_symbols_map[symbol]
        except (KeyError, AttributeError):
            pass
        if not hasattr(self, "formatted_symbols_map"):
            self.formatted_symbols_map = {}
            self.formatted_symbols_map_inv = defaultdict(set)
        formatted = f"{symbol_to_coin(symbol.replace(',', ''))}/{self.quote}:{self.quote}"
        self.formatted_symbols_map[symbol] = formatted
        self.formatted_symbols_map_inv[formatted].add(symbol)
        return formatted

    def symbol_is_eligible(self, symbol):
        # defined for each child class
        return True

    async def init_markets_dict(self):
        await self.update_exchange_config()
        self.init_markets_last_update_ms = utc_ms()
        self.markets_dict = {elm["symbol"]: elm for elm in (await self.cca.fetch_markets())}
        await self.determine_utc_offset()
        self.markets_dict_all = deepcopy(self.markets_dict)
        # remove ineligible symbols from markets dict
        ineligible_symbols = {}
        for symbol in list(self.markets_dict):
            if not self.markets_dict[symbol]["active"]:
                ineligible_symbols[symbol] = "not active"
                del self.markets_dict[symbol]
            elif not self.markets_dict[symbol]["swap"]:
                ineligible_symbols[symbol] = "wrong market type"
                del self.markets_dict[symbol]
            elif not self.markets_dict[symbol]["linear"]:
                ineligible_symbols[symbol] = "not linear"
                del self.markets_dict[symbol]
            elif not symbol.endswith(f"/{self.quote}:{self.quote}"):
                ineligible_symbols[symbol] = "wrong quote"
                del self.markets_dict[symbol]
            elif not self.symbol_is_eligible(symbol):
                ineligible_symbols[symbol] = f"not eligible on {self.exchange}"
                del self.markets_dict[symbol]
            elif not self.symbol_is_eligible(symbol):
                ineligible_symbols[symbol] = f"not eligible on {self.exchange}"
                del self.markets_dict[symbol]
        for line in set(ineligible_symbols.values()):
            syms_ = [s for s in ineligible_symbols if ineligible_symbols[s] == line]
            if len(syms_) > 12:
                logging.info(f"{line}: {len(syms_)} symbols")
            elif len(syms_) > 0:
                logging.info(f"{line}: {','.join(sorted(set([s for s in syms_])))}")

        for symbol in self.ineligible_symbols_with_pos:
            if symbol not in self.markets_dict and symbol in self.markets_dict_all:
                logging.info(f"There is a position in an ineligible market: {symbol}.")
                self.markets_dict[symbol] = self.markets_dict_all[symbol]
                self.config["live"]["ignored_symbols"].append(symbol)

        self.set_market_specific_settings()
        for symbol in self.markets_dict:
            self.format_symbol(symbol)
        # for prettier printing
        self.max_len_symbol = max([len(s) for s in self.markets_dict])
        self.sym_padding = max(self.sym_padding, self.max_len_symbol + 1)
        await self.init_flags()
        self.set_live_configs()

    def set_market_specific_settings(self):
        # set min cost, min qty, price step, qty step, c_mult
        # defined individually for each exchange
        self.symbol_ids = {symbol: self.markets_dict[symbol]["id"] for symbol in self.markets_dict}
        self.symbol_ids_inv = {v: k for k, v in self.symbol_ids.items()}

    def set_wallet_exposure_limits(self):
        for pside in ["long", "short"]:
            changed = {}
            n_actives = len(self.is_active[pside])
            WE_limit_div = pbr.round_(
                (
                    self.config["bot"][pside]["total_wallet_exposure_limit"] / n_actives
                    if n_actives > 0
                    else 0.001
                ),
                0.0001,
            )
            for symbol in self.is_active[pside]:
                new_WE_limit = (
                    getattr(self.flags[symbol], f"WE_limit_{pside}")
                    if symbol in self.flags
                    and getattr(self.flags[symbol], f"WE_limit_{pside}") is not None
                    else WE_limit_div
                )
                if "wallet_exposure_limit" not in self.live_configs[symbol][pside]:
                    changed[symbol] = (0.0, new_WE_limit)
                elif self.live_configs[symbol][pside]["wallet_exposure_limit"] != new_WE_limit:
                    changed[symbol] = (
                        self.live_configs[symbol][pside]["wallet_exposure_limit"],
                        new_WE_limit,
                    )
                self.live_configs[symbol][pside]["wallet_exposure_limit"] = new_WE_limit
            if changed:
                inv = defaultdict(set)
                for symbol in changed:
                    inv[changed[symbol]].add(symbol)
                for k, v in inv.items():
                    syms = ", ".join(sorted([symbol_to_coin(s) for s in v]))
                    logging.info(f"changed {pside} WE limit from {k[0]} to {k[1]} for {syms}")

    async def update_exchange_configs(self):
        if not hasattr(self, "already_updated_exchange_config_symbols"):
            self.already_updated_exchange_config_symbols = set()
        symbols_not_done = [
            x for x in self.active_symbols if x not in self.already_updated_exchange_config_symbols
        ]
        if symbols_not_done:
            await self.update_exchange_config_by_symbols(symbols_not_done)
            self.already_updated_exchange_config_symbols.update(symbols_not_done)

    async def update_exchange_config_by_symbols(self, symbols):
        # defined by each exchange child class
        pass

    async def update_exchange_config(self):
        # defined by each exchange child class
        pass

    def reformat_symbol(self, symbol: str, verbose=False) -> str:
        # tries to reformat symbol to correct variant for exchange
        # (e.g. BONK -> 1000BONK/USDT:USDT, PEPE - kPEPE/USDC:USDC)
        # if no reformatting is possible, return empty string
        fsymbol = self.format_symbol(symbol)
        if fsymbol in self.markets_dict:
            return fsymbol
        else:
            if verbose:
                logging.info(f"{symbol} missing from {self.exchange}")
            if fsymbol in self.formatted_symbols_map_inv:
                for x in self.formatted_symbols_map_inv[fsymbol]:
                    if x in self.markets_dict:
                        if verbose:
                            logging.info(f"changing {symbol} -> {x}")
                        return x
        return ""

    async def init_flags(self):
        self.ignored_symbols = {
            self.reformat_symbol(x) for x in self.config["live"]["ignored_symbols"]
        }
        self.flags = {}
        self.eligible_symbols = set()  # symbols which may be approved for trading

        for symbol in self.config["common"]["approved_symbols"]:
            reformatted_symbol = self.reformat_symbol(symbol, verbose=True)
            if reformatted_symbol:
                self.flags[reformatted_symbol] = (
                    self.config["common"]["approved_symbols"][symbol]
                    if isinstance(self.config["common"]["approved_symbols"], dict)
                    else ""
                )
                self.eligible_symbols.add(reformatted_symbol)
        if not self.config["common"]["approved_symbols"]:
            self.eligible_symbols = set(self.markets_dict)

        # this argparser is used only internally
        parser = argparse.ArgumentParser(prog="passivbot", description="run passivbot")
        parser.add_argument(
            "-sm", type=expand_PB_mode, required=False, dest="short_mode", default=None
        )
        parser.add_argument(
            "-lm", type=expand_PB_mode, required=False, dest="long_mode", default=None
        )
        parser.add_argument("-lw", type=float, required=False, dest="WE_limit_long", default=None)
        parser.add_argument("-sw", type=float, required=False, dest="WE_limit_short", default=None)
        parser.add_argument("-lev", type=float, required=False, dest="leverage", default=None)
        parser.add_argument("-lc", type=str, required=False, dest="live_config_path", default=None)
        self.forced_modes = {"long": {}, "short": {}}
        for symbol in self.markets_dict:
            self.flags[symbol] = parser.parse_args(
                self.flags[symbol].split() if symbol in self.flags else []
            )
            for pside in ["long", "short"]:
                if (mode := getattr(self.flags[symbol], f"{pside}_mode")) is None:
                    if symbol in self.ignored_symbols:
                        setattr(
                            self.flags[symbol],
                            f"{pside}_mode",
                            "graceful_stop" if self.config["live"]["auto_gs"] else "manual",
                        )
                        self.forced_modes[pside][symbol] = getattr(
                            self.flags[symbol], f"{pside}_mode"
                        )
                    elif self.config["live"][f"forced_mode_{pside}"]:
                        try:
                            setattr(
                                self.flags[symbol],
                                f"{pside}_mode",
                                expand_PB_mode(self.config["live"][f"forced_mode_{pside}"]),
                            )
                            self.forced_modes[pside][symbol] = getattr(
                                self.flags[symbol], f"{pside}_mode"
                            )
                        except Exception as e:
                            logging.error(
                                f"failed to set PB mode {self.config['live'][f'forced_mode_{pside}']} {e}"
                            )
                else:
                    self.forced_modes[pside][symbol] = mode
                if not self.markets_dict[symbol]["active"]:
                    self.forced_modes[pside][symbol] = "tp_only"

        if self.forager_mode and self.minimum_market_age_millis > 0:
            if not hasattr(self, "first_timestamps"):
                self.first_timestamps = await get_first_ohlcv_timestamps(cc=self.cca)
                for symbol in sorted(self.first_timestamps):
                    self.first_timestamps[self.format_symbol(symbol)] = self.first_timestamps[symbol]
        else:
            self.first_timestamps = None

    def is_old_enough(self, symbol):
        if self.forager_mode and self.minimum_market_age_millis > 0:
            if symbol in self.first_timestamps:
                return utc_ms() - self.first_timestamps[symbol] > self.minimum_market_age_millis
            else:
                return False
        else:
            return True

    def update_PB_modes(self):
        # update passivbot modes for all symbols
        if hasattr(self, "PB_modes"):
            previous_PB_modes = deepcopy(self.PB_modes)
        else:
            previous_PB_modes = None

        # set modes for all symbols
        self.PB_modes = {
            "long": {},
            "short": {},
        }  # options: normal, graceful_stop, manual, tp_only, panic
        self.actual_actives = {"long": set(), "short": set()}  # symbols with position
        self.is_active = {"long": set(), "short": set()}  # actual actives plus symbols on "normal""
        self.ideal_actives = {"long": {}, "short": {}}  # dicts as ordered sets

        # actual actives, symbols with pos and/or open orders
        for elm in self.fetched_positions + self.fetched_open_orders:
            self.actual_actives[elm["position_side"]].add(elm["symbol"])

        # find ideal actives
        # set forced modes first
        for pside in self.forced_modes:
            for symbol in self.forced_modes[pside]:
                if self.forced_modes[pside][symbol] == "normal":
                    self.PB_modes[pside][symbol] = self.forced_modes[pside][symbol]
                    self.ideal_actives[pside][symbol] = ""
                if symbol in self.actual_actives[pside]:
                    self.PB_modes[pside][symbol] = self.forced_modes[pside][symbol]
        if self.forager_mode:
            if self.config["common"]["relative_volume_filter_clip_pct"] > 0.0:
                self.calc_volumes()
                # filter by relative volume
                eligible_symbols = sorted(self.volumes, key=lambda x: self.volumes[x])[
                    int(
                        round(
                            len(self.volumes)
                            * self.config["common"]["relative_volume_filter_clip_pct"]
                        )
                    ) :
                ]
            else:
                eligible_symbols = list(self.eligible_symbols)
            self.calc_noisiness()  # ideal symbols are high noise symbols

            # calc ideal actives for long and short separately
            for pside in self.actual_actives:
                if self.config["bot"][pside]["n_positions"] > 0:
                    self.warn_on_high_effective_min_cost(pside)
                for symbol in sorted(eligible_symbols, key=lambda x: self.noisiness[x], reverse=True):
                    if (
                        symbol not in self.eligible_symbols
                        or not self.is_old_enough(symbol)
                        or not self.effective_min_cost_is_low_enough(pside, symbol)
                    ):
                        continue
                    slots_full = (
                        len(self.ideal_actives[pside]) >= self.config["bot"][pside]["n_positions"]
                    )
                    if slots_full:
                        break
                    if symbol not in self.ideal_actives[pside]:
                        self.ideal_actives[pside][symbol] = ""

                # actual actives fill slots first
                for symbol in self.actual_actives[pside]:
                    if symbol in self.forced_modes[pside]:
                        continue  # is a forced mode
                    if symbol in self.ideal_actives[pside]:
                        self.PB_modes[pside][symbol] = "normal"
                    else:
                        self.PB_modes[pside][symbol] = (
                            "graceful_stop" if self.config["live"]["auto_gs"] else "manual"
                        )
                # fill remaining slots with ideal actives
                # a slot is filled if symbol in [normal, graceful_stop]
                # symbols on other modes are ignored
                for symbol in self.ideal_actives[pside]:
                    if symbol in self.PB_modes[pside] or symbol in self.forced_modes[pside]:
                        continue
                    slots_filled = {
                        k for k, v in self.PB_modes[pside].items() if v in ["normal", "graceful_stop"]
                    }
                    if len(slots_filled) >= self.config["bot"][pside]["n_positions"]:
                        break
                    self.PB_modes[pside][symbol] = "normal"
        else:
            # if not forager mode, all eligible symbols are ideal symbols, unless symbol in forced_modes
            for pside in ["long", "short"]:
                if self.config[f"{pside}_enabled"]:
                    self.warn_on_high_effective_min_cost(pside)
                    for symbol in self.eligible_symbols:
                        if not self.effective_min_cost_is_low_enough(pside, symbol):
                            continue
                        if symbol not in self.forced_modes[pside]:
                            self.PB_modes[pside][symbol] = "normal"
                            self.ideal_actives[pside][symbol] = ""
                for symbol in self.actual_actives[pside]:
                    if symbol not in self.PB_modes[pside]:
                        self.PB_modes[pside][symbol] = (
                            "graceful_stop" if self.config["live"]["auto_gs"] else "manual"
                        )
        self.active_symbols = sorted(
            {s for subdict in self.PB_modes.values() for s in subdict.keys()}
        )
        self.is_active = deepcopy(self.actual_actives)
        for pside in self.PB_modes:
            for symbol in self.PB_modes[pside]:
                if self.PB_modes[pside][symbol] == "normal":
                    self.is_active[pside].add(symbol)

        for symbol in self.active_symbols:
            for pside in self.PB_modes:
                if symbol in self.PB_modes[pside]:
                    self.live_configs[symbol][pside]["mode"] = self.PB_modes[pside][symbol]
                    self.live_configs[symbol][pside]["enabled"] = (
                        self.PB_modes[pside][symbol] == "normal"
                    )
                else:
                    if self.config["live"]["auto_gs"]:
                        self.live_configs[symbol][pside]["mode"] = "graceful_stop"
                        self.PB_modes[pside][symbol] = "graceful_stop"
                    else:
                        self.live_configs[symbol][pside]["mode"] = "manual"
                        self.PB_modes[pside][symbol] = "manual"

                    self.live_configs[symbol][pside]["enabled"] = False
            if symbol not in self.positions:
                self.positions[symbol] = {
                    "long": {"size": 0.0, "price": 0.0},
                    "short": {"size": 0.0, "price": 0.0},
                }
            if symbol not in self.open_orders:
                self.open_orders[symbol] = []
        self.set_wallet_exposure_limits()
        self.update_trailing_data()

        # log changes
        for pside in self.PB_modes:
            if previous_PB_modes is None:
                for mode in set(self.PB_modes[pside].values()):
                    coins = [
                        symbol_to_coin(s)
                        for s in self.PB_modes[pside]
                        if self.PB_modes[pside][s] == mode
                    ]
                    logging.info(f" setting {pside: <5} {mode}: {','.join(coins)}")
            else:
                if previous_PB_modes[pside] != self.PB_modes[pside]:
                    for symbol in self.active_symbols:
                        if symbol in self.PB_modes[pside]:
                            if symbol in previous_PB_modes[pside]:
                                if self.PB_modes[pside][symbol] != previous_PB_modes[pside][symbol]:
                                    logging.info(
                                        f"changing {pside: <5} {self.pad_sym(symbol)}: {previous_PB_modes[pside][symbol]} -> {self.PB_modes[pside][symbol]}"
                                    )
                            else:
                                logging.info(
                                    f" setting {pside: <5} {self.pad_sym(symbol)}: {self.PB_modes[pside][symbol]}"
                                )
                        else:
                            if symbol in previous_PB_modes[pside]:
                                logging.info(
                                    f"removing {pside: <5} {self.pad_sym(symbol)}: {previous_PB_modes[pside][symbol]}"
                                )

    def warn_on_high_effective_min_cost(self, pside):
        if not self.config["live"]["filter_by_min_effective_cost"]:
            return
        eligible_symbols_filtered = [
            x for x in self.eligible_symbols if self.effective_min_cost_is_low_enough(pside, x)
        ]
        if len(eligible_symbols_filtered) == 0:
            logging.info(
                f"Warning: No {pside} symbols are approved due to min effective cost too high. "
                + f"Suggestions: 1) increase account balance, 2) "
                + f"set 'filter_by_min_effective_cost' to false, 3) if in forager mode, reduce n_{pside}s"
            )

    def effective_min_cost_is_low_enough(self, pside, symbol):
        if not self.config["live"]["filter_by_min_effective_cost"]:
            return True
        try:
            WE_limit = self.live_configs[symbol][pside]["wallet_exposure_limit"]
            assert WE_limit > 0.0
        except:
            if self.forager_mode:
                WE_limit = (
                    self.config["bot"][pside]["total_wallet_exposure_limit"]
                    / self.config["bot"][pside]["n_positions"]
                    if self.config["bot"][pside]["n_positions"] > 0
                    else 0.0
                )
            else:
                WE_limit = (
                    self.config["bot"][pside]["total_wallet_exposure_limit"]
                    / len(self.config["common"]["approved_symbols"])
                    if len(self.config["common"]["approved_symbols"]) > 0
                    else 0.0
                )
        return (
            self.balance * WE_limit * self.live_configs[symbol][pside]["entry_initial_qty_pct"]
            >= self.tickers[symbol]["effective_min_cost"]
        )

    def add_new_order(self, order, source="WS"):
        try:
            if not order or "id" not in order:
                return False
            if "symbol" not in order or order["symbol"] is None:
                logging.info(f"{order}")
                return False
            if order["symbol"] not in self.open_orders:
                self.open_orders[order["symbol"]] = []
            if order["id"] not in {x["id"] for x in self.open_orders[order["symbol"]]}:
                self.open_orders[order["symbol"]].append(order)
                logging.info(
                    f"  created {self.pad_sym(order['symbol'])} {order['side']} {order['qty']} {order['position_side']} @ {order['price']} source: {source}"
                )
                return True
        except Exception as e:
            logging.error(f"failed to add order to self.open_orders {order} {e}")
            traceback.print_exc()
            return False

    def remove_cancelled_order(self, order: dict, source="WS"):
        try:
            if not order or "id" not in order:
                return False
            if "symbol" not in order or order["symbol"] is None:
                logging.info(f"{order}")
                return False
            if order["symbol"] not in self.open_orders:
                self.open_orders[order["symbol"]] = []
            if order["id"] in {x["id"] for x in self.open_orders[order["symbol"]]}:
                self.open_orders[order["symbol"]] = [
                    x for x in self.open_orders[order["symbol"]] if x["id"] != order["id"]
                ]
                logging.info(
                    f"cancelled {self.pad_sym(order['symbol'])} {order['side']} {order['qty']} {order['position_side']} @ {order['price']} source: {source}"
                )
                return True
        except Exception as e:
            logging.error(f"failed to remove order from self.open_orders {order} {e}")
            traceback.print_exc()
            return False

    def handle_order_update(self, upd_list):
        try:
            for upd in upd_list:
                if upd["status"] == "closed" or (
                    "filled" in upd and upd["filled"] is not None and upd["filled"] > 0.0
                ):
                    # There was a fill, partial or full. Schedule update of open orders, pnls, position.
                    logging.info(
                        f"   filled {self.pad_sym(upd['symbol'])} {upd['side']} {upd['qty']} {upd['position_side']} @ {upd['price']} source: WS"
                    )
                    self.recent_fill = True
                elif upd["status"] in ["canceled", "expired", "rejected"]:
                    # remove order from open_orders
                    self.remove_cancelled_order(upd)
                elif upd["status"] == "open":
                    # add order to open_orders
                    self.add_new_order(upd)
                else:
                    print("debug open orders unknown type", upd)
        except Exception as e:
            logging.error(f"error updating open orders from websocket {upd_list} {e}")
            traceback.print_exc()

    def handle_balance_update(self, upd, source="WS"):
        try:
            upd[self.quote]["total"] = round_dynamic(upd[self.quote]["total"], 10)
            equity = upd[self.quote]["total"] + self.calc_upnl_sum()
            if self.balance != upd[self.quote]["total"]:
                logging.info(
                    f"balance changed: {self.balance} -> {upd[self.quote]['total']} equity: {equity:.4f} source: {source}"
                )
            self.balance = max(upd[self.quote]["total"], 1e-12)
        except Exception as e:
            logging.error(f"error updating balance from websocket {upd} {e}")
            traceback.print_exc()

    def handle_ticker_update(self, upd):
        if isinstance(upd, list):
            for x in upd:
                self.handle_ticker_update(x)
        elif isinstance(upd, dict):
            if len(upd) == 1:
                # sometimes format is {symbol: {ticker}}
                upd = upd[next(iter(upd))]
            if "bid" not in upd and "bids" in upd and "ask" not in upd and "asks" in upd:
                # order book, not ticker
                upd["bid"], upd["ask"] = upd["bids"][0][0], upd["asks"][0][0]
            if all([key in upd for key in ["bid", "ask", "symbol"]]):
                if "last" not in upd or upd["last"] is None:
                    upd["last"] = np.mean([upd["bid"], upd["ask"]])
                self.update_hlc_ws(upd["symbol"], upd["bid"], upd["ask"], upd["timestamp"])
                for key in ["bid", "ask", "last"]:
                    if upd[key] is not None:
                        if upd[key] != self.tickers[upd["symbol"]][key]:
                            self.tickers[upd["symbol"]][key] = upd[key]
                    else:
                        logging.info(f"ticker {upd['symbol']} {key} is None")
            else:
                logging.info(f"unexpected WS ticker formatting: {upd}")

    def handle_ohlcv_update(self, symbol, upd):
        for elm in upd:
            if symbol not in self.hlcs_1m:
                self.hlcs_1m[symbol] = SortedDict()
            self.hlcs_1m[symbol][elm[0]] = self.ohlcv_to_hlc(elm)

    def ohlcv_to_hlc(self, elm):
        return [elm[0], elm[2], elm[3], elm[4]]

    def calc_upnl_sum(self):
        try:
            upnl_sum = 0.0
            for elm in self.fetched_positions:
                upnl_sum += calc_pnl(
                    elm["position_side"],
                    elm["price"],
                    self.tickers[elm["symbol"]]["last"],
                    elm["size"],
                    self.inverse,
                    self.c_mults[elm["symbol"]],
                )
            return upnl_sum
        except Exception as e:
            logging.error(f"error calculating upnl sum {e}")
            traceback.print_exc()
            return 0.0

    async def update_fills(self):
        # called whenever a position changes
        # some exchanges have unified pnl and fill data, some have them separate
        pass

    async def update_pnls(self):
        # fetch latest pnls
        # dump new pnls to cache
        age_limit = utc_ms() - 1000 * 60 * 60 * 24 * self.config["live"]["pnls_max_lookback_days"]
        if utc_ms() - self.upd_timestamps["pnls"] > 1000 * 60 * 5:
            missing_pnls = []
            if len(self.pnls) == 0:
                # load pnls from cache
                pnls_cache = []
                try:
                    if os.path.exists(self.pnls_cache_filepath):
                        pnls_cache = json.load(open(self.pnls_cache_filepath))
                except Exception as e:
                    logging.error(f"error loading {self.pnls_cache_filepath} {e}")
                # fetch pnls since latest timestamp
                if len(pnls_cache) > 0:
                    if pnls_cache[0]["timestamp"] > age_limit + 1000 * 60 * 60 * 4:
                        # fetch missing pnls
                        res = await self.fetch_pnls(
                            start_time=age_limit - 1000, end_time=pnls_cache[0]["timestamp"]
                        )
                        if res in [None, False]:
                            return False
                        missing_pnls = res
                        pnls_cache = sorted(
                            {
                                elm["id"]: elm
                                for elm in pnls_cache + missing_pnls
                                if elm["timestamp"] >= age_limit
                            }.values(),
                            key=lambda x: x["timestamp"],
                        )
                self.pnls = pnls_cache
            start_time = self.pnls[-1]["timestamp"] if self.pnls else age_limit
            res = await self.fetch_pnls(start_time=start_time)
            if res in [None, False]:
                return False
        else:
            res = await self.fetch_pnls()
        new_pnls = [x for x in res if x["id"] not in {elm["id"] for elm in self.pnls}]
        self.pnls = sorted(
            {elm["id"]: elm for elm in self.pnls + new_pnls if elm["timestamp"] > age_limit}.values(),
            key=lambda x: x["timestamp"],
        )
        if new_pnls:
            new_income = sum([x["pnl"] for x in new_pnls])
            if new_income != 0.0:
                logging.info(
                    f"{len(new_pnls)} new pnl{'s' if len(new_pnls) > 1 else ''} {new_income} {self.quote}"
                )
            try:
                json.dump(self.pnls, open(self.pnls_cache_filepath, "w"))
            except Exception as e:
                logging.error(f"error dumping pnls to {self.pnls_cache_filepath} {e}")
        self.upd_timestamps["pnls"] = utc_ms()
        return True

    async def check_for_inactive_markets(self):
        self.ineligible_symbols_with_pos = [
            elm["symbol"]
            for elm in self.fetched_positions + self.fetched_open_orders
            if elm["symbol"] not in self.markets_dict
            or not self.markets_dict[elm["symbol"]]["active"]
        ]
        update = False
        if self.ineligible_symbols_with_pos:
            logging.info(
                f"Caught symbol with pos for ineligible market: {self.ineligible_symbols_with_pos}"
            )
            update = True
        if utc_ms() - self.init_markets_last_update_ms > (1000 * 60 * 60 * 3):
            logging.info(f"Force updating markets every three hours.")
            update = True
        if update:
            await self.init_markets_dict()
            await self.init_flags()
            self.set_live_configs()
            self.update_PB_modes()

    async def update_open_orders(self):
        if not hasattr(self, "open_orders"):
            self.open_orders = {}
        res = await self.fetch_open_orders()
        if res in [None, False]:
            return False
        self.fetched_open_orders = res
        await self.check_for_inactive_markets()
        open_orders = res
        oo_ids_old = {elm["id"] for sublist in self.open_orders.values() for elm in sublist}
        created_prints, cancelled_prints = [], []
        for oo in open_orders:
            if oo["id"] not in oo_ids_old:
                # there was a new open order not caught by websocket
                created_prints.append(
                    f"new order {self.pad_sym(oo['symbol'])} {oo['side']} {oo['qty']} {oo['position_side']} @ {oo['price']} source: REST"
                )
        oo_ids_new = {elm["id"] for elm in open_orders}
        for oo in [elm for sublist in self.open_orders.values() for elm in sublist]:
            if oo["id"] not in oo_ids_new:
                # there was an order cancellation not caught by websocket
                cancelled_prints.append(
                    f"cancelled {self.pad_sym(oo['symbol'])} {oo['side']} {oo['qty']} {oo['position_side']} @ {oo['price']} source: REST"
                )
        self.open_orders = {}
        for elm in open_orders:
            if elm["symbol"] not in self.open_orders:
                self.open_orders[elm["symbol"]] = []
            self.open_orders[elm["symbol"]].append(elm)
        if len(created_prints) > 12:
            logging.info(f"{len(created_prints)} new open orders")
        else:
            for line in created_prints:
                logging.info(line)
        if len(cancelled_prints) > 12:
            logging.info(f"{len(created_prints)} cancelled open orders")
        else:
            for line in cancelled_prints:
                logging.info(line)
        self.upd_timestamps["open_orders"] = utc_ms()
        return True

    async def determine_utc_offset(self):
        # returns millis to add to utc to get exchange timestamp
        # call some endpoint which includes timestamp for exchange's server
        # if timestamp is not included in self.cca.fetch_balance(),
        # implement method in exchange child class
        result = await self.cca.fetch_balance()
        self.utc_offset = round((result["timestamp"] - utc_ms()) / (1000 * 60 * 60)) * (
            1000 * 60 * 60
        )
        logging.info(f"Exchange time offset is {self.utc_offset}ms compared to UTC")

    def get_exchange_time(self):
        return utc_ms() + self.utc_offset

    async def update_positions(self):
        # also updates balance
        if not hasattr(self, "positions"):
            self.positions = {}
        res = await self.fetch_positions()
        if all(x in [None, False] for x in res):
            return False
        positions_list_new, balance_new = res
        self.fetched_positions = positions_list_new
        await self.check_for_inactive_markets()
        self.handle_balance_update({self.quote: {"total": balance_new}})
        positions_new = {
            sym: {
                "long": {"size": 0.0, "price": 0.0},
                "short": {"size": 0.0, "price": 0.0},
            }
            for sym in set(list(self.positions) + list(self.active_symbols))
        }
        position_changes = []
        for elm in positions_list_new:
            symbol, pside, pprice = elm["symbol"], elm["position_side"], elm["price"]
            psize = abs(elm["size"]) * (-1.0 if elm["position_side"] == "short" else 1.0)
            if symbol not in positions_new:
                positions_new[symbol] = {
                    "long": {"size": 0.0, "price": 0.0},
                    "short": {"size": 0.0, "price": 0.0},
                }
            positions_new[symbol][pside] = {"size": psize, "price": pprice}
            # check if changed
            if symbol not in self.positions or self.positions[symbol][pside]["size"] != psize:
                position_changes.append((symbol, pside))
        try:
            self.log_position_changes(position_changes, positions_new)
        except Exception as e:
            logging.error(f"error printing position changes {e}")
        self.positions = positions_new
        self.upd_timestamps["positions"] = utc_ms()
        return True

    def log_position_changes(self, position_changes, positions_new, rd=6) -> str:
        if not position_changes:
            return ""
        table = PrettyTable()
        table.border = False
        table.header = False
        table.padding_width = 0  # Reduces padding between columns to zero
        for symbol, pside in position_changes:
            wallet_exposure = (
                qty_to_cost(
                    positions_new[symbol][pside]["size"],
                    positions_new[symbol][pside]["price"],
                    self.inverse,
                    self.c_mults[symbol],
                )
                / self.balance
            )
            try:
                WE_ratio = wallet_exposure / self.live_configs[symbol][pside]["wallet_exposure_limit"]
            except:
                WE_ratio = 0.0
            try:
                pprice_diff = calc_pprice_diff(
                    pside, positions_new[symbol][pside]["price"], self.tickers[symbol]["last"]
                )
            except:
                pprice_diff = 0.0
            try:
                upnl = calc_pnl(
                    pside,
                    positions_new[symbol][pside]["price"],
                    self.tickers[symbol]["last"],
                    positions_new[symbol][pside]["size"],
                    self.inverse,
                    self.c_mults[symbol],
                )
            except Exception as e:
                upnl = 0.0
            table.add_row(
                [
                    symbol + " ",
                    pside + " ",
                    (
                        round_dynamic(self.positions[symbol][pside]["size"], rd)
                        if symbol in self.positions
                        else 0.0
                    ),
                    " @ ",
                    (
                        round_dynamic(self.positions[symbol][pside]["price"], rd)
                        if symbol in self.positions
                        else 0.0
                    ),
                    " -> ",
                    round_dynamic(positions_new[symbol][pside]["size"], rd),
                    " @ ",
                    round_dynamic(positions_new[symbol][pside]["price"], rd),
                    " WE: ",
                    round_dynamic(wallet_exposure, max(3, rd - 2)),
                    " WE ratio: ",
                    round(WE_ratio, 3),
                    " PA dist: ",
                    round(pprice_diff, 4),
                    " upnl: ",
                    round_dynamic(upnl, max(3, rd - 1)),
                ]
            )
        string = table.get_string()
        for line in string.splitlines():
            logging.info(line)
        return string

    async def update_tickers(self):
        res = await self.fetch_tickers()
        if res in [None, False]:
            return False
        tickers_new = res
        for symbol in tickers_new:
            if tickers_new[symbol]["bid"] is None or tickers_new[symbol]["ask"] is None:
                continue
            if "last" not in tickers_new[symbol] or tickers_new[symbol]["last"] is None:
                tickers_new[symbol]["last"] = np.mean(
                    [tickers_new[symbol]["bid"], tickers_new[symbol]["ask"]]
                )
            self.tickers[symbol] = {k: tickers_new[symbol][k] for k in ["bid", "ask", "last"]}
            if symbol in self.markets_dict:
                try:
                    self.tickers[symbol]["effective_min_cost"] = max(
                        qty_to_cost(
                            self.min_qtys[symbol],
                            tickers_new[symbol]["last"],
                            self.inverse,
                            self.c_mults[symbol],
                        ),
                        self.min_costs[symbol],
                    )
                except Exception as e:
                    logging.info(f"debug effective_min_cost update tickers {symbol} {e}")
                    self.tickers[symbol]["effective_min_cost"] = 0.0
            else:
                self.tickers[symbol]["effective_min_cost"] = 0.0
        self.upd_timestamps["tickers"] = utc_ms()
        return True

    def is_enabled(self, symbol, pside=None):
        if pside is None:
            return self.is_enabled(symbol, "long") or self.is_enabled(symbol, "short")
        return (
            self.live_configs[symbol][pside]["wallet_exposure_limit"] > 0.0
            and self.live_configs[symbol][pside]["n_positions"] > 0.0
        )

    def calc_ideal_orders(self):
        ideal_orders = {symbol: [] for symbol in self.active_symbols}
        for symbol in self.active_symbols:
            if self.hedge_mode:
                do_long = self.is_enabled(symbol, "long") or self.has_position(symbol, "long")
                do_short = self.is_enabled(symbol, "short") or self.has_position(symbol, "short")
            else:
                no_pos = not self.has_position(symbol)
                do_long = (no_pos and self.is_enabled(symbol, "long")) or self.has_position(
                    symbol, "long"
                )
                do_short = (no_pos and self.is_enabled(symbol, "short")) or self.has_position(
                    symbol, "short"
                )
            if self.live_configs[symbol]["long"]["mode"] == "panic":
                if self.has_position(symbol, "long"):
                    # if in panic mode, only one close order at current market price
                    ideal_orders[symbol].append(
                        (
                            -abs(self.positions[symbol]["long"]["size"]),
                            self.tickers[symbol]["ask"],
                            "panic_close_long",
                        )
                    )
                # otherwise, no orders
            elif self.live_configs[symbol]["long"][
                "mode"
            ] == "graceful_stop" and not self.has_position(symbol, "long"):
                # if graceful stop and no pos, don't open new pos
                pass
            elif do_long:
                entries_long = pbr.calc_entries_long_py(
                    self.qty_steps[symbol],
                    self.price_steps[symbol],
                    self.min_qtys[symbol],
                    self.min_costs[symbol],
                    self.c_mults[symbol],
                    self.live_configs[symbol]["long"]["entry_grid_double_down_factor"],
                    self.live_configs[symbol]["long"]["entry_grid_spacing_weight"],
                    self.live_configs[symbol]["long"]["entry_grid_spacing_pct"],
                    self.live_configs[symbol]["long"]["entry_initial_ema_dist"],
                    self.live_configs[symbol]["long"]["entry_initial_qty_pct"],
                    self.live_configs[symbol]["long"]["entry_trailing_retracement_pct"],
                    self.live_configs[symbol]["long"]["entry_trailing_grid_ratio"],
                    self.live_configs[symbol]["long"]["entry_trailing_threshold_pct"],
                    self.live_configs[symbol]["long"]["wallet_exposure_limit"],
                    self.balance,
                    self.positions[symbol]["long"]["size"],
                    self.positions[symbol]["long"]["price"],
                    self.trailing_prices[symbol]["long"]["min_since_open"],
                    self.trailing_prices[symbol]["long"]["max_since_min"],
                    self.emas["long"][symbol].min(),
                    self.tickers[symbol]["bid"],
                )
                closes_long = pbr.calc_closes_long_py(
                    self.qty_steps[symbol],
                    self.price_steps[symbol],
                    self.min_qtys[symbol],
                    self.min_costs[symbol],
                    self.c_mults[symbol],
                    self.live_configs[symbol]["long"]["close_grid_markup_range"],
                    self.live_configs[symbol]["long"]["close_grid_min_markup"],
                    self.live_configs[symbol]["long"]["close_grid_qty_pct"],
                    self.live_configs[symbol]["long"]["close_trailing_retracement_pct"],
                    self.live_configs[symbol]["long"]["close_trailing_grid_ratio"],
                    self.live_configs[symbol]["long"]["close_trailing_threshold_pct"],
                    self.live_configs[symbol]["long"]["wallet_exposure_limit"],
                    self.balance,
                    self.positions[symbol]["long"]["size"],
                    self.positions[symbol]["long"]["price"],
                    self.trailing_prices[symbol]["long"]["max_since_open"],
                    self.trailing_prices[symbol]["long"]["min_since_max"],
                    self.tickers[symbol]["ask"],
                )
                ideal_orders[symbol] += entries_long + closes_long

            if self.live_configs[symbol]["short"]["mode"] == "panic":
                if self.has_position(symbol, "short"):
                    # if in panic mode, only one close order at current market price
                    ideal_orders[symbol].append(
                        (
                            abs(self.positions[symbol]["short"]["size"]),
                            self.tickers[symbol]["bid"],
                            "panic_close_short",
                        )
                    )
                # otherwise, no orders
            elif self.live_configs[symbol]["short"][
                "mode"
            ] == "graceful_stop" and not self.has_position(symbol, "short"):
                # if graceful stop and no pos, don't open new pos
                pass
            elif do_short:
                entries_short = pbr.calc_entries_short_py(
                    self.qty_steps[symbol],
                    self.price_steps[symbol],
                    self.min_qtys[symbol],
                    self.min_costs[symbol],
                    self.c_mults[symbol],
                    self.live_configs[symbol]["short"]["entry_grid_double_down_factor"],
                    self.live_configs[symbol]["short"]["entry_grid_spacing_weight"],
                    self.live_configs[symbol]["short"]["entry_grid_spacing_pct"],
                    self.live_configs[symbol]["short"]["entry_initial_ema_dist"],
                    self.live_configs[symbol]["short"]["entry_initial_qty_pct"],
                    self.live_configs[symbol]["short"]["entry_trailing_retracement_pct"],
                    self.live_configs[symbol]["short"]["entry_trailing_grid_ratio"],
                    self.live_configs[symbol]["short"]["entry_trailing_threshold_pct"],
                    self.live_configs[symbol]["short"]["wallet_exposure_limit"],
                    self.balance,
                    self.positions[symbol]["short"]["size"],
                    self.positions[symbol]["short"]["price"],
                    self.trailing_prices[symbol]["short"]["max_since_open"],
                    self.trailing_prices[symbol]["short"]["min_since_max"],
                    self.emas["short"][symbol].max(),
                    self.tickers[symbol]["ask"],
                )
                closes_short = pbr.calc_closes_short_py(
                    self.qty_steps[symbol],
                    self.price_steps[symbol],
                    self.min_qtys[symbol],
                    self.min_costs[symbol],
                    self.c_mults[symbol],
                    self.live_configs[symbol]["short"]["close_grid_markup_range"],
                    self.live_configs[symbol]["short"]["close_grid_min_markup"],
                    self.live_configs[symbol]["short"]["close_grid_qty_pct"],
                    self.live_configs[symbol]["short"]["close_trailing_retracement_pct"],
                    self.live_configs[symbol]["short"]["close_trailing_grid_ratio"],
                    self.live_configs[symbol]["short"]["close_trailing_threshold_pct"],
                    self.live_configs[symbol]["short"]["wallet_exposure_limit"],
                    self.balance,
                    self.positions[symbol]["short"]["size"],
                    self.positions[symbol]["short"]["price"],
                    self.trailing_prices[symbol]["short"]["min_since_open"],
                    self.trailing_prices[symbol]["short"]["max_since_min"],
                    self.tickers[symbol]["bid"],
                )
                ideal_orders[symbol] += entries_short + closes_short

        unstucking_symbol, unstucking_close = self.calc_unstucking_close(ideal_orders)
        if unstucking_close[0] != 0.0:
            ideal_orders[unstucking_symbol] = [
                x for x in ideal_orders[unstucking_symbol] if not "close" in x[2]
            ]
            ideal_orders[unstucking_symbol].append(unstucking_close)

        ideal_orders_f = {}
        for symbol in ideal_orders:
            ideal_orders_f[symbol] = []
            with_pprice_diff = [
                (calc_diff(x[1], self.tickers[symbol]["last"]), x) for x in ideal_orders[symbol]
            ]
            seen = set()
            for pprice_diff, order in sorted(with_pprice_diff):
                if order[0] == 0.0 or (
                    any([x in order[2] for x in ["initial", "unstuck"]])
                    and pprice_diff > self.config["live"]["price_distance_threshold"]
                ):
                    continue
                seen_key = str(abs(order[0])) + str(order[1])
                if seen_key in seen:
                    logging.info(f"debug duplicate ideal order {symbol} {order}")
                    continue
                ideal_orders_f[symbol].append(
                    {
                        "symbol": symbol,
                        "side": determine_side_from_order_tuple(order),
                        "position_side": "long" if "long" in order[2] else "short",
                        "qty": abs(order[0]),
                        "price": order[1],
                        "reduce_only": "close" in order[2],
                        "custom_id": order[2],
                    }
                )
                seen.add(seen_key)
        return ideal_orders_f

    def calc_unstucking_close(self, ideal_orders):
        stuck_positions = []
        for symbol in self.positions:
            for pside in ["long", "short"]:
                if self.has_position(symbol, pside):
                    wallet_exposure = pbr.calc_wallet_exposure(
                        self.c_mults[symbol],
                        self.balance,
                        self.positions[symbol][pside]["size"],
                        self.positions[symbol][pside]["price"],
                    )
                    if (
                        self.live_configs[symbol][pside]["wallet_exposure_limit"] == 0.0
                        or wallet_exposure / self.live_configs[symbol][pside]["wallet_exposure_limit"]
                        > self.live_configs[symbol][pside]["unstuck_threshold"]
                    ):
                        pprice_diff = calc_pprice_diff(
                            pside,
                            self.positions[symbol][pside]["price"],
                            self.tickers[symbol]["last"],
                        )
                        stuck_positions.append((symbol, pside, pprice_diff))
        if not stuck_positions:
            return "", (0.0, 0.0, "")
        stuck_positions.sort(key=lambda x: x[2])
        for symbol, pside, _ in stuck_positions:
            if pside == "long":
                close_price = max(
                    self.tickers[symbol]["ask"],
                    pbr.round_up(
                        self.emas[pside][symbol].max()
                        * (1.0 + self.live_configs[symbol][pside]["unstuck_ema_dist"]),
                        self.price_steps[symbol],
                    ),
                )
                ideal_closes = (
                    [x for x in ideal_orders[symbol] if "close" in x[2] and pside in x[2]]
                    if symbol in ideal_orders
                    else []
                )
                if not ideal_closes or close_price >= ideal_closes[0][1]:
                    continue
                close_qty = -min(
                    self.positions[symbol][pside]["size"],
                    max(
                        calc_min_entry_qty(
                            close_price,
                            False,
                            self.c_mults[symbol],
                            self.qty_steps[symbol],
                            self.min_qtys[symbol],
                            self.min_costs[symbol],
                        ),
                        pbr.round_dn(
                            pbr.cost_to_qty(
                                self.balance
                                * self.live_configs[symbol][pside]["wallet_exposure_limit"]
                                * self.live_configs[symbol][pside]["unstuck_close_pct"],
                                close_price,
                                self.c_mults[symbol],
                            ),
                            self.qty_steps[symbol],
                        ),
                    ),
                )
                if close_qty != 0.0:
                    return symbol, (close_qty, close_price, "unstuck_close_long")
            elif pside == "short":
                close_price = min(
                    self.tickers[symbol]["bid"],
                    pbr.round_dn(
                        self.emas[pside][symbol].min()
                        * (1.0 - self.live_configs[symbol][pside]["unstuck_ema_dist"]),
                        self.price_steps[symbol],
                    ),
                )
                ideal_closes = (
                    [x for x in ideal_orders[symbol] if "close" in x[2] and pside in x[2]]
                    if symbol in ideal_orders
                    else []
                )
                if not ideal_closes or close_price <= ideal_closes[0]["price"]:
                    continue
                close_qty = min(
                    abs(self.positions[symbol][pside]["size"]),
                    max(
                        calc_min_entry_qty(
                            close_price,
                            False,
                            self.c_mults[symbol],
                            self.qty_steps[symbol],
                            self.min_qtys[symbol],
                            self.min_costs[symbol],
                        ),
                        pbr.round_dn(
                            pbr.cost_to_qty(
                                self.balance
                                * self.live_configs[symbol][pside]["wallet_exposure_limit"]
                                * self.live_configs[symbol][pside]["unstuck_close_pct"],
                                close_price,
                                self.c_mults[symbol],
                            ),
                            self.qty_steps[symbol],
                        ),
                    ),
                )
                if close_qty != 0.0:
                    return symbol, (close_qty, close_price, "unstuck_close_short")
        return "", (0.0, 0.0, "")

    def calc_orders_to_cancel_and_create(self):
        ideal_orders = self.calc_ideal_orders()
        actual_orders = {}
        for symbol in self.active_symbols:
            actual_orders[symbol] = []
            for x in self.open_orders[symbol] if symbol in self.open_orders else []:
                try:
                    actual_orders[symbol].append(
                        {
                            "symbol": x["symbol"],
                            "side": x["side"],
                            "position_side": x["position_side"],
                            "qty": abs(x["qty"]),
                            "price": x["price"],
                            "reduce_only": (x["position_side"] == "long" and x["side"] == "sell")
                            or (x["position_side"] == "short" and x["side"] == "buy"),
                            "id": x["id"],
                        }
                    )
                except Exception as e:
                    logging.error(f"error in calc_orders_to_cancel_and_create {e}")
                    traceback.print_exc()
                    print(x)
        keys = ("symbol", "side", "position_side", "qty", "price")
        to_cancel, to_create = [], []
        for symbol in actual_orders:
            to_cancel_, to_create_ = filter_orders(actual_orders[symbol], ideal_orders[symbol], keys)
            for pside in ["long", "short"]:
                if self.live_configs[symbol][pside]["mode"] == "manual":
                    # neither create nor cancel orders
                    to_cancel_ = [x for x in to_cancel_ if x["position_side"] != pside]
                    to_create_ = [x for x in to_create_ if x["position_side"] != pside]
                elif self.live_configs[symbol][pside]["mode"] == "tp_only":
                    # if take profit only mode, neither cancel nor create entries
                    to_cancel_ = [
                        x
                        for x in to_cancel_
                        if (
                            x["position_side"] != pside
                            or (x["position_side"] == pside and x["reduce_only"])
                        )
                    ]
                    to_create_ = [
                        x
                        for x in to_create_
                        if (
                            x["position_side"] != pside
                            or (x["position_side"] == pside and x["reduce_only"])
                        )
                    ]
            to_cancel += to_cancel_
            to_create += to_create_
        return sorted(
            to_cancel, key=lambda x: calc_diff(x["price"], self.tickers[x["symbol"]]["last"])
        ), sorted(to_create, key=lambda x: calc_diff(x["price"], self.tickers[x["symbol"]]["last"]))

    async def force_update(self, force=False):
        # if some information has not been updated in a while, force update via REST
        coros_to_call = []
        now = utc_ms()
        for key in self.upd_timestamps:
            if force or now - self.upd_timestamps[key] > self.force_update_age_millis:
                # logging.info(f"forcing update {key}")
                coros_to_call.append((key, getattr(self, f"update_{key}")()))
        res = await asyncio.gather(*[x[1] for x in coros_to_call])
        return res

    async def execute_to_exchange_old(self):
        # cancels wrong orders and creates missing orders
        # check whether to call any self.update_*()
        if utc_ms() - self.execution_delay_millis < self.previous_execution_ts:
            return True
        self.previous_execution_ts = utc_ms()
        try:
            self.update_PB_modes()
            await self.add_new_symbols_to_maintainer_EMAs()
            await self.update_exchange_configs()
            if self.recent_fill:
                self.upd_timestamps["positions"] = 0.0
                self.upd_timestamps["open_orders"] = 0.0
                self.upd_timestamps["pnls"] = 0.0
                self.recent_fill = False
            update_res = await self.force_update()
            if not all(update_res):
                for i, key in enumerate(self.upd_timestamps):
                    if not update_res[i]:
                        logging.error(f"error with {key}")
                return
            to_cancel, to_create = self.calc_orders_to_cancel_and_create()

            # debug duplicates
            seen = set()
            for elm in to_cancel:
                key = str(elm["price"]) + str(elm["qty"])
                if key in seen:
                    print("debug duplicate", elm)
                seen.add(key)

            # format custom_id
            to_create = self.format_custom_ids(to_create)
            res = await self.execute_cancellations(
                to_cancel[: self.config["live"]["max_n_cancellations_per_batch"]]
            )
            if res:
                for elm in res:
                    self.remove_cancelled_order(elm, source="POST")
            res = await self.execute_orders(
                to_create[: self.config["live"]["max_n_creations_per_batch"]]
            )
            if res:
                for elm in res:
                    self.add_new_order(elm, source="POST")
            if to_cancel or to_create:
                await asyncio.gather(self.update_open_orders(), self.update_positions())

        except Exception as e:
            logging.error(f"error executing to exchange {e}")
            traceback.print_exc()
            await self.restart_bot_on_too_many_errors()
        finally:
            self.previous_execution_ts = utc_ms()

    async def restart_bot_on_too_many_errors(self):
        if not hasattr(self, "error_counts"):
            self.error_counts = []
        now = utc_ms()
        self.error_counts = [x for x in self.error_counts if x > now - 1000 * 60 * 60] + [now]
        max_n_errors_per_hour = 10
        logging.info(
            f"error count: {len(self.error_counts)} of {max_n_errors_per_hour} errors per hour"
        )
        if len(self.error_counts) >= max_n_errors_per_hour:
            raise Exception("too many errors... restarting bot.")

    def format_custom_ids(self, orders: [dict]) -> [dict]:
        new_orders = []
        for order in orders:
            order["custom_id"] = (
                shorten_custom_id(order["custom_id"] if "custom_id" in order else "") + uuid4().hex
            )[: self.custom_id_max_length]
            new_orders.append(order)
        return new_orders

    async def execution_loop(self):
        while True:
            if self.stop_websocket:
                break
            if utc_ms() - self.execution_delay_millis > self.previous_execution_ts:
                await self.execute_to_exchange_old()
            await asyncio.sleep(1.0)
            # self.debug_dump_bot_state_to_disk()

    def debug_dump_bot_state_to_disk(self):
        if not hasattr(self, "tmp_debug_ts"):
            self.tmp_debug_ts = 0
            self.tmp_debug_cache = make_get_filepath(f"caches/{self.exchange}/{self.user}_debug/")
        if utc_ms() - self.tmp_debug_ts > 1000 * 60 * 3:
            logging.info(f"debug dumping bot state to disk")
            for k, v in vars(self).items():
                try:
                    json.dump(
                        denumpyize(v), open(os.path.join(self.tmp_debug_cache, k + ".json"), "w")
                    )
                except Exception as e:
                    logging.error(f"debug failed to dump to disk {k} {e}")
            self.tmp_debug_ts = utc_ms()

    async def start_bot(self):
        await self.start_data_maintainers()
        await self.run_execution_loop()
        return
        await self.init_bot()
        logging.info("done initiating bot")
        asyncio.create_task(self.start_data_maintainers_old())
        for i in range(30):
            await asyncio.sleep(1)
            if set(self.emas["long"]) == set(self.active_symbols):
                break
        logging.info("starting websockets...")
        await asyncio.gather(self.execution_loop(), self.start_websockets())

    def get_ohlcv_fpath(self, symbol) -> str:
        return os.path.join(
            self.ohlcvs_cache_dirpath, symbol.replace(f"/{self.quote}:{self.quote}", "") + ".json"
        )

    def load_ohlcv_from_cache(self, symbol, suppress_error_log=False):
        fpath = self.get_ohlcv_fpath(symbol)
        try:
            ohlcvs = json.load(open(fpath))
            return ohlcvs
        except Exception as e:
            if not suppress_error_log:
                logging.error(f"failed to load ohlcvs from cache for {symbol}")
                traceback.print_exc()
            return None

    def dump_ohlcv_to_cache(self, symbol, ohlcv):
        fpath = self.get_ohlcv_fpath(symbol)
        try:
            json.dump(ohlcv, open(fpath, "w"))
            self.ohlcv_upd_timestamps[symbol] = get_file_mod_utc(self.get_ohlcv_fpath(symbol))
        except Exception as e:
            logging.error(f"failed to dump ohlcvs to cache for {symbol}")
            traceback.print_exc()

    def get_oldest_updated_ohlcv_symbol(self):
        for _ in range(100):
            symbol = sorted(self.ohlcv_upd_timestamps.items(), key=lambda x: x[1])[0][0]
            # check if has been modified by other PB instance
            try:
                self.ohlcv_upd_timestamps[symbol] = get_file_mod_utc(self.get_ohlcv_fpath(symbol))
            except Exception as e:
                logging.error(f"error with get_file_mod_utc {e}")
                self.ohlcv_upd_timestamps[symbol] = 0.0
            if symbol == sorted(self.ohlcv_upd_timestamps.items(), key=lambda x: x[1])[0][0]:
                break
        return symbol

    def calc_noisiness(self, symbol=None):
        if not hasattr(self, "noisiness"):
            self.noisiness = {}
        symbols = self.eligible_symbols if symbol is None else [symbol]
        for symbol in symbols:
            if symbol in self.ohlcvs and self.ohlcvs[symbol] and len(self.ohlcvs[symbol]) > 0:
                self.noisiness[symbol] = np.mean([(x[2] - x[3]) / x[4] for x in self.ohlcvs[symbol]])
            else:
                self.noisiness[symbol] = 0.0

    def calc_volumes(self):
        if not hasattr(self, "volumes"):
            self.volumes = {}
        for symbol in self.eligible_symbols:
            if symbol in self.ohlcvs and self.ohlcvs[symbol] and len(self.ohlcvs[symbol]) > 0:
                self.volumes[symbol] = sum([x[4] * x[5] for x in self.ohlcvs[symbol]])
            else:
                self.volumes[symbol] = 0.0

    async def add_new_symbols_to_maintainer_EMAs(self, symbols=None):
        if symbols is None:
            to_add = sorted(set(self.active_symbols) - set(self.emas["long"]))
        else:
            to_add = [s for s in set(symbols) if s not in self.emas["long"]]
        if to_add:
            logging.info(f"adding to EMA maintainer: {','.join([symbol_to_coin(s) for s in to_add])}")
            await self.init_EMAs_multi(to_add)
            if self.forager_mode:
                await self.update_ohlcvs_multi(to_add)

    async def maintain_EMAs_old(self):
        # maintain EMAs for active symbols
        # if a new symbol appears (e.g. new forager symbol or user manually opens a position), add this symbol to EMA maintainer
        try:
            logging.info(
                f"initiating EMAs for {','.join([symbol_to_coin(s) for s in self.active_symbols])}"
            )
            await self.init_EMAs_multi(sorted(self.active_symbols))
        except Exception as e:
            logging.error(f"Error starting maintain_EMAs_old: {e}")
            traceback.print_exc()
        logging.info(f"starting EMA maintainer...")
        while True:
            try:
                now_minute = int(utc_ms() // (1000 * 60) * (1000 * 60))
                symbols_to_update = [
                    s
                    for s in self.emas["long"]
                    if s not in self.upd_minute_emas or now_minute > self.upd_minute_emas[s]
                ]
                await self.update_EMAs_multi(symbols_to_update)
                await asyncio.sleep(30)
            except Exception as e:
                logging.error(f"Error with maintain_EMAs_old: {e}")
                traceback.print_exc()
                logging.info("restarting EMA maintainer in")
                for i in range(10, 0, -1):
                    logging.info(f"{i} seconds")
                    await asyncio.sleep(1)

    async def maintain_ohlcvs(self, timeframe=None):
        timeframe = self.config["common"]["ohlcv_interval"] if timeframe is None else timeframe
        # if in forager mode, maintain ohlcvs for all candidate symbols
        # else, fetch ohlcvs once for EMA initialization
        if not self.forager_mode:
            return
        sleep_interval_sec = max(5.0, (60.0 * 60.0) / len(self.eligible_symbols))
        logging.info(
            f"Starting ohlcvs maintainer. Will sleep {sleep_interval_sec:.1f}s between each fetch."
        )
        while True:
            try:
                all_syms = set(self.active_symbols) | self.eligible_symbols
                missing_symbols = [s for s in all_syms if s not in self.ohlcv_upd_timestamps]
                if missing_symbols:
                    coins_ = [symbol_to_coin(s) for s in missing_symbols]
                    logging.info(f"adding missing symbols to ohlcv maintainer: {','.join(coins_)}")
                    await self.update_ohlcvs_multi(missing_symbols)
                    await asyncio.sleep(3)
                else:
                    symbol = self.get_oldest_updated_ohlcv_symbol()
                    start_ts = utc_ms()
                    res = await self.update_ohlcvs_single(
                        symbol, age_limit_ms=(sleep_interval_sec * 1000)
                    )
                    # logging.info(f"updated ohlcvs for {symbol} {res}")
                    await asyncio.sleep(max(0.0, sleep_interval_sec - (utc_ms() - start_ts) / 1000))
            except Exception as e:
                logging.error(f"Error with maintain_ohlcvs: {e}")
                traceback.print_exc()
                logging.info("restarting ohlcvs maintainer in")
                for i in range(10, 0, -1):
                    logging.info(f"{i} seconds")
                    await asyncio.sleep(1)

    async def update_EMAs_multi(self, symbols, n_fetches=10, verbose=False):
        all_res = []
        for sym_sublist in [symbols[i : i + n_fetches] for i in range(0, len(symbols), n_fetches)]:
            try:
                res = await asyncio.gather(
                    *[self.update_EMAs_single(symbol, verbose=False) for symbol in sym_sublist]
                )
                if verbose:
                    if any(res):
                        logging.info(
                            f"initiated EMAs for {','.join([symbol_to_coin(s) for s, r in zip(sym_sublist, res) if r])}"
                        )
            except Exception as e:
                logging.error(f"error in update_EMAs_multi {sym_sublist} {e}")
            all_res += res
        return all_res

    async def update_EMAs_single(self, symbol, verbose=False):
        # updates EMAs for single symbol
        try:
            # if EMAs for symbol has not been initiated, initiate
            if not all([symbol in x for x in [self.emas["long"], self.upd_minute_emas]]):
                # initiate EMA for symbol
                await self.init_EMAs_single(symbol, verbose)
            now_minute = int(utc_ms() // (1000 * 60) * (1000 * 60))
            if now_minute <= self.upd_minute_emas[symbol]:
                return True
            for pside in ["long", "short"]:
                self.emas[pside][symbol] = calc_ema(
                    self.ema_alphas[pside][symbol][0],
                    self.ema_alphas[pside][symbol][1],
                    self.emas[pside][symbol],
                    self.tickers[symbol]["last"],
                )
            self.upd_minute_emas[symbol] = now_minute
            return True
        except Exception as e:
            logging.error(f"failed to update EMAs for {symbol}: {e}")
            traceback.print_exc()
            return False

    async def init_EMAs_multi(self, symbols, n_fetches=10):
        all_res = []
        for sym_sublist in [symbols[i : i + n_fetches] for i in range(0, len(symbols), n_fetches)]:
            try:
                res = await asyncio.gather(*[self.init_EMAs_single(symbol) for symbol in sym_sublist])
                all_res += res
                if verbose:
                    if any(res):
                        logging.info(
                            f"initiated EMAs for {','.join([symbol_to_coin(s) for s, r in zip(sym_sublist, res) if r])}"
                        )
            except Exception as e:
                logging.error(f"error in init_EMAs_multi {sym_sublist} {e}")
        return all_res

    async def init_EMAs_single(self, symbol, verbose=True):
        # check if ohlcvs are in cache for symbol
        # if not, or if too old, update them
        # if it fails, print warning and use ticker as first EMA

        ema_spans = {}
        for pside in ["long", "short"]:
            # if computing EMAs from ohlcvs fails, use recent tickers
            self.emas[pside][symbol] = np.repeat(self.tickers[symbol]["last"], 3)
            lc = self.live_configs[symbol][pside]
            es = [lc["ema_span_0"], lc["ema_span_1"], (lc["ema_span_0"] * lc["ema_span_1"]) ** 0.5]
            ema_spans[pside] = numpyize(sorted(es))
            self.ema_alphas[pside][symbol] = (a := (2.0 / (ema_spans[pside] + 1)), 1.0 - a)
        try:
            # allow up to 5 mins old ohlcvs
            await self.update_ohlcvs_single(symbol, age_limit_ms=1000 * 60 * 5)
            samples1m = calc_samples(
                numpyize(self.ohlcvs[symbol])[:, [0, 5, 4]], sample_size_ms=60000
            )
            for pside in ["long", "short"]:
                self.emas[pside][symbol] = calc_emas_last(samples1m[:, 2], ema_spans[pside])
            if verbose:
                logging.info(f"initiated EMAs for {symbol}")
        except Exception as e:
            logging.error(f"error initiating EMAs for {self.pad_sym(symbol)}; using ticker as EMAs")
        self.upd_minute_emas[symbol] = int(utc_ms() // (1000 * 60) * (1000 * 60))

    async def update_ohlcvs_multi(self, symbols, timeframe=None, n_fetches=10, verbose=False):
        timeframe = self.config["common"]["ohlcv_interval"] if timeframe is None else timeframe
        all_res = []
        for sym_sublist in [symbols[i : i + n_fetches] for i in range(0, len(symbols), n_fetches)]:
            try:
                res = await asyncio.gather(
                    *[
                        self.update_ohlcvs_single(symbol, timeframe=timeframe)
                        for symbol in sym_sublist
                    ]
                )
                all_res += res
                if verbose:
                    if any(res):
                        logging.info(
                            f"updated ohlcvs for {','.join([symbol_to_coin(s) for s, r in zip(sym_sublist, res) if r])}"
                        )
            except Exception as e:
                logging.error(f"error with fetch_ohlcv in update_ohlcvs_multi {sym_sublist} {e}")

    async def update_ohlcvs_single(self, symbol, timeframe=None, age_limit_ms=1000 * 60 * 60):
        timeframe = self.config["common"]["ohlcv_interval"] if timeframe is None else timeframe
        last_ts_modified = 0.0
        try:
            last_ts_modified = get_file_mod_utc(self.get_ohlcv_fpath(symbol))
        except:
            pass
        self.ohlcv_upd_timestamps[symbol] = last_ts_modified
        try:
            self.ohlcvs[symbol] = self.load_ohlcv_from_cache(symbol)
        except:
            self.ohlcvs[symbol] = []
        try:
            if utc_ms() - last_ts_modified > age_limit_ms:
                self.ohlcvs[symbol] = await self.fetch_ohlcv(symbol, timeframe=timeframe)
                self.dump_ohlcv_to_cache(symbol, self.ohlcvs[symbol])
            """
            if len(self.ohlcvs[symbol]) < self.config["common"]["n_ohlcvs"]:
                logging.info(
                    f"too few ohlcvs fetched for {symbol}: fetched {len(self.ohlcvs[symbol])}, ideally: {self.config['common']['n_ohlcvs']}"
                )
            """
            self.ohlcvs[symbol] = self.ohlcvs[symbol][-self.config["common"]["n_ohlcvs"] :]
            return True
        except Exception as e:
            logging.error(f"error with update_ohlcvs_single {symbol} {e}")
            traceback.print_exc()
            return False

    async def execute_multiple(self, orders: [dict], type_: str, max_n_executions: int):
        if not orders:
            return []
        executions = []
        for order in orders[:max_n_executions]:  # sorted by PA dist
            execution = None
            try:
                execution = asyncio.create_task(getattr(self, type_)(order))
                executions.append((order, execution))
            except Exception as e:
                logging.error(f"error executing {type_} {order} {e}")
                print_async_exception(execution)
                traceback.print_exc()
        results = []
        for execution in executions:
            result = None
            try:
                result = await execution[1]
                results.append(result)
            except Exception as e:
                logging.error(f"error executing {type_} {execution} {e}")
                print_async_exception(result)
                traceback.print_exc()
        return results

    async def close(self):
        logging.info(f"Stopped data maintainers: {self.stop_data_maintainers()}")
        await self.cca.close()
        await self.ccp.close()


def setup_bot(config):
    # returns bot instance
    user_info = load_user_info(config["live"]["user"])
    if user_info["exchange"] == "bybit":
        from exchanges_forager.bybit import BybitBot

        bot = BybitBot(config)
    elif user_info["exchange"] == "binance":
        from exchanges_forager.binance import BinanceBot

        bot = BinanceBot(config)
    elif user_info["exchange"] == "bitget":
        from exchanges_forager.bitget import BitgetBot

        bot = BitgetBot(config)
    elif user_info["exchange"] == "okx":
        from exchanges_forager.okx import OKXBot

        bot = OKXBot(config)
    elif user_info["exchange"] == "bingx":
        from exchanges_forager.bingx import BingXBot

        bot = BingXBot(config)
    elif user_info["exchange"] == "hyperliquid":
        from exchanges_forager.hyperliquid import HyperliquidBot

        bot = HyperliquidBot(config)
    else:
        raise Exception(f"unknown exchange {user_info['exchange']}")
    return bot


async def main():
    parser = argparse.ArgumentParser(prog="passivbot", description="run passivbot")
    parser.add_argument("hjson_config_path", type=str, help="path to hjson passivbot meta config")
    parser_items = [
        (
            "s",
            "approved_symbols",
            "approved_symbols",
            str,
            ", comma separated (SYM1USDT,SYM2USDT,...)",
        ),
        ("i", "ignored_symbols", "ignored_symbols", str, ", comma separated (SYM1USDT,SYM2USDT,...)"),
        ("le", "long_enabled", "long_enabled", str2bool, " (y/n or t/f)"),
        ("se", "short_enabled", "short_enabled", str2bool, " (y/n or t/f)"),
        ("tl", "total_wallet_exposure_long", "TWE_long", float, ""),
        ("ts", "total_wallet_exposure_short", "TWE_short", float, ""),
        ("u", "user", "user", str, ""),
        ("lap", "loss_allowance_pct", "loss_allowance_pct", float, " (set to 0.0 to disable)"),
        ("pml", "pnls_max_lookback_days", "pnls_max_lookback_days", float, ""),
        ("st", "stuck_threshold", "stuck_threshold", float, ""),
        ("ucp", "unstuck_close_pct", "unstuck_close_pct", float, ""),
        ("eds", "execution_delay_seconds", "execution_delay_seconds", float, ""),
        ("lcd", "live_configs_dir", "live_configs_dir", str, ""),
        ("dcp", "default_config_path", "default_config_path", str, ""),
        ("ag", "auto_gs", "auto_gs", str2bool, " enabled (y/n or t/f)"),
        ("nca", "max_n_cancellations_per_batch", "max_n_cancellations_per_batch", int, ""),
        ("ncr", "max_n_creations_per_batch", "max_n_creations_per_batch", int, ""),
        ("pt", "price_threshold", "price_threshold", float, ""),
    ]
    for k0, k1, d, t, h in parser_items:
        parser.add_argument(
            *[f"-{k0}", f"--{k1}"] + ([f"--{k1.replace('_', '-')}"] if "_" in k1 else []),
            type=t,
            required=False,
            dest=d,
            default=None,
            help=f"specify {k1}{h}, overriding value from live hjson config.",
        )
    max_n_restarts_per_day = 5
    cooldown_secs = 60
    restarts = []
    while True:
        args = parser.parse_args()
        config = load_config(args.hjson_config_path)
        """
        for key in [x[2] for x in parser_items]:
            if getattr(args, key) is not None:
                if key.endswith("symbols"):
                    old_value = sorted(set(config[key]))
                    new_value = sorted(set(getattr(args, key).split(",")))
                else:
                    old_value = config[key]
                    new_value = getattr(args, key)
                logging.info(f"changing {key}: {old_value} -> {new_value}")
                config[key] = new_value
        """
        bot = setup_bot(config)
        try:
            await bot.start_bot()
        except Exception as e:
            logging.error(f"passivbot error {e}")
            traceback.print_exc()
        finally:
            try:
                bot.stop_data_maintainers()
                await bot.ccp.close()
                await bot.cca.close()
            except:
                pass
        logging.info(f"restarting bot...")
        print()
        for z in range(cooldown_secs, -1, -1):
            print(f"\rcountdown {z}...  ")
            await asyncio.sleep(1)
        print()
        restarts.append(utc_ms())
        restarts = [x for x in restarts if x > utc_ms() - 1000 * 60 * 60 * 24]
        if len(restarts) > max_n_restarts_per_day:
            break


if __name__ == "__main__":
    asyncio.run(main())
