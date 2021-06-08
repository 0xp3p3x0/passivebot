import asyncio
import os
from time import time
from analyze import analyze_fills

import numpy as np
import pandas as pd
import argparse
import pprint
from plotting import dump_plots

from procedures import load_live_config, make_get_ticks_cache
from downloader import Downloader
from procedures import prep_config, make_get_filepath
from pure_funcs import create_xk, calc_spans, denumpyize, ts_to_date
from njit_funcs import njit_backtest, calc_bankruptcy_price, calc_long_pnl, calc_shrt_pnl, \
    calc_available_margin, calc_diff, qty_to_cost, round_, \
    calc_new_psize_pprice
from passivbot import add_argparse_args


def backtest(config: dict, data: (np.ndarray,), do_print=False) -> (list, bool):
    xk = create_xk(config)
    return njit_backtest(data, config['starting_balance'], config['latency_simulation_ms'],
                         config['maker_fee'], **xk)


def plot_wrap(bc, data, live_config):
    n_days = round_((data[2][-1] - data[2][0]) / (1000 * 60 * 60 * 24), 0.1)
    print('n_days', round_(n_days, 0.1))
    config = {**bc, **live_config}
    print('starting_balance', config['starting_balance'])
    print('backtesting...')
    fills, stats, did_finish = backtest(config, data, do_print=True)
    if not fills:
        print('no fills')
        return
    fdf, result = analyze_fills(fills, config, data[2][-1])
    config['result'] = result
    config['plots_dirpath'] = make_get_filepath(os.path.join(
        config['plots_dirpath'], f"{ts_to_date(time())[:19].replace(':', '')}", '')
    )
    fdf.to_csv(config['plots_dirpath'] + "fills.csv")
    df = pd.DataFrame({**{'price': data[0], 'buyer_maker': data[1], 'timestamp': data[2], 'ema': data[3]},
                       **{f'ratio_{i}': data[4][:,i] for i in range(len(data[4][0]))}})
    print('dumping plots...')
    dump_plots(config, fdf, df)


async def main():

    parser = argparse.ArgumentParser(prog='Backtest', description='Backtest given passivbot config.')
    parser.add_argument('live_config_path', type=str, help='path to live config to test')
    parser = add_argparse_args(parser)
    args = parser.parse_args()

    config = await prep_config(args)
    print()
    for k in (keys := ['exchange', 'symbol', 'starting_balance', 'start_date', 'end_date',
                       'latency_simulation_ms', 'do_long', 'do_shrt']):
        if k in config:
            print(f"{k: <{max(map(len, keys)) + 2}} {config[k]}")
    print()
    if config['exchange'] == 'bybit' and not config['inverse']:
        print('bybit usdt linear backtesting not supported')
        return
    downloader = Downloader(config)
    ticks = await downloader.get_ticks(True)
    live_config = load_live_config(args.live_config_path)
    config = {**config, **live_config}
    data = make_get_ticks_cache(config, ticks)
    config['n_days'] = round_((data[2][-1] - data[2][0]) / (1000 * 60 * 60 * 24), 0.1)
    pprint.pprint(denumpyize(live_config))
    plot_wrap(config, data, live_config)


if __name__ == '__main__':
    asyncio.run(main())

