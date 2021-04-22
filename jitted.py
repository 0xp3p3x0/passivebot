import sys


if '--nojit' in sys.argv:
    print('not using numba')
    def njit(pyfunc=None, **kwargs):
        def wrap(func):
            return func

        if pyfunc is not None:
            return wrap(pyfunc)
        else:
            return wrap
else:
    print('using numba')
    from numba import njit


@njit
def round_up(n: float, step: float, safety_rounding=10) -> float:
    return np.round(np.ceil(n / step) * step, safety_rounding)


@njit
def round_dn(n: float, step: float, safety_rounding=10) -> float:
    return np.round(np.floor(n / step) * step, safety_rounding)


@njit
def round_(n: float, step: float, safety_rounding=10) -> float:
    return np.round(np.round(n / step) * step, safety_rounding)


@njit
def calc_diff(x, y):
    return abs(x - y) / abs(y)


@njit
def nan_to_0(x: float) -> float:
    return x if x == x else 0.0


@njit
def calc_ema(alpha: float, alpha_: float, prev_ema: float, new_val: float) -> float:
    return prev_ema * alpha_ + new_val * alpha


@njit
def calc_emas(xs: [float], span: int) -> np.ndarray:
    alpha = 2 / (span + 1)
    alpha_ = 1 - alpha
    emas = np.empty_like(xs)
    emas[0] = xs[0]
    for i in range(1, len(xs)):
        emas[i] = emas[i-1] * alpha_ + xs[i] * alpha
    return emas


@njit
def calc_stds(xs: [float], span: int) -> np.ndarray:
    stds = np.empty_like(xs)
    stds.fill(np.nan)
    if len(stds) <= span:
        return stds
    xsum = xs[:span].sum()
    xsum_sq = (xs[:span]**2).sum()
    stds[span] = np.sqrt((xsum_sq / span) - (xsum / span)**2)
    for i in range(span, len(xs)):
        xsum += xs[i] - xs[i-span]
        xsum_sq += xs[i]**2 - xs[i-span]**2
        stds[i] = np.sqrt((xsum_sq / span) - (xsum / span)**2)
    return stds


@njit
def calc_initial_long_entry_price(xk: tuple, ema: float,  highest_bid: float) -> float:
    return min(highest_bid, round_dn(ema * (1 - xk[11]), xk[1]))


@njit
def calc_initial_shrt_entry_price(xk: tuple, ema: float, lowest_ask: float) -> float:
    return max(lowest_ask, round_up(ema * (1 + xk[11]), xk[1]))


@njit
def calc_min_entry_qty(xk: tuple, price: float) -> float:
    return max(xk[3], round_up(xk[4] * (price / xk[5] if xk[16] else 1 / price), xk[1]))


@njit
def calc_initial_entry_qty(xk: tuple, balance: float, price: float, available_margin: float) -> float:
    min_entry_qty = calc_min_entry_qty(xk, price)
    if xk[16]:
        qty = round_dn(min(available_margin * price / xk[5],
                           max(min_entry_qty, (balance / xk[5]) * price * xk[8] * xk[7])), xk[0])
    else:
        qty = round_dn(min(available_margin / price,
                           max(min_entry_qty, (balance / price) * xk[8] * xk[7])), xk[0])
    return qty if qty >= min_entry_qty else 0.0


@njit
def calc_reentry_qty(xk: tuple, psize: float, price: float, available_margin: float) -> float:
    min_entry_qty = calc_min_entry_qty(xk, price)
    qty = min(round_dn(available_margin * (price / xk[5] if xk[16] else 1 / price), xk[0]),
              max(min_entry_qty, round_dn(abs(psize) * xk[6], xk[0])))
    return qty if qty >= min_entry_qty else 0.0


@njit
def calc_reentry_price(xk: tuple,
                       balance: float,
                       psize: float,
                       pprice: float,
                       long_: bool) -> float:
    modified_grid_spacing = xk[9] * (1 + (calc_margin_cost(xk, psize, pprice) / balance) * xk[10])
    return round_dn(pprice * (1 + (modified_grid_spacing * (-1 if long_ else 1)), xk[1]))


@njit
def calc_new_psize_pprice(xk: tuple,
                          psize: float,
                          pprice: float,
                          qty: float,
                          price: float) -> (float, float):
    if qty == 0.0:
        return psize, pprice
    new_psize = round_(psize + qty, xk[0])
    return new_psize, nan_to_0(pprice) * (psize / new_psize) + price * (qty / new_psize)

@njit
def calc_long_pnl(xk: tuple, entry_price: float, close_price: float, qty: float) -> float:
    if xk[16]:
        return abs(qty) * xk[5] * (1 / entry_price - 1 / close_price)
    else:
        return abs(qty) * (close_price - entry_price)


@njit
def calc_shrt_pnl(xk: tuple, entry_price: float, close_price: float, qty: float) -> float:
    if xk[16]:
        return abs(qty) * xk[5] * (1 / close_price - 1 / entry_price)
    else:
        return abs(qty) * (entry_price - close_price)


@njit
def calc_cost(xk: tuple, qty: float, price: float) -> float:
    return abs(qty / price) * xk[5] if xk[16] else abs(qty * price)


@njit
def calc_margin_cost(xk: tuple, qty: float, price: float) -> float:
    return calc_cost(xk, qty, price) / leverage


@njit
def calc_available_margin(xk: float,
                          balance: float,
                          long_psize: float,
                          long_pprice: float,
                          shrt_psize: float,
                          shrt_pprice: float,
                          last_price: float) -> float:
    used_margin = 0.0
    equity = balance
    if long_pprice and long_psize:
        long_psize_real = long_psize * xk[5]
        equity += calc_long_pnl(xk, long_pprice, last_price, long_psize_real)
        used_margin += calc_cost(xk, long_psize_real, long_pprice) / xk[8]
    if shrt_pprice and shrt_psize:
        shrt_psize_real = shrt_psize * xk[5]
        equity += calc_shrt_pnl(xk, shrt_pprice, last_price, shrt_psize_real)
        used_margin += calc_cost(xk, shrt_psize_real, shrt_pprice) / xk[8]
    return equity - used_margin


@njit
def iter_entries(xk: tuple,
                 balance: float,
                 long_psize: float,
                 long_pprice: float,
                 shrt_psize: float,
                 shrt_pprice: float,
                 liq_price: float,
                 highest_bid: float,
                 lowest_ask: float,
                 ema: float,
                 last_price: float):
    '''
    xk index/value
     0 qty_step              6 ddown_factor      11 ema_spread          16 inverse
     1 price_step            7 qty_pct           12 stop_loss_liq_diff  17 min_markup
     3 min_qty               8 leverage          13 stop_loss_pos_pct   18 markup_range
     4 min_cost              9 grid_spacing      14 do_long             19 n_close_orders
     5 contract_multiplier  10 grid_coefficient  15 do_shrt             20
    '''

    available_margin = calc_available_margin(xk[5], leverage, balance, long_psize, long_pprice,
                                             shrt_psize, shrt_pprice, last_price)
    stop_loss_order = calc_stop_loss(xk, balance, long_psize, long_pprice, shrt_psize, shrt_pprice,
                                     liq_price, highest_bid, lowest_ask, last_price, available_margin)
    if stop_loss_order[0] != 0.0:
        yield stop_loss_order

    while True:
        if xk[14]:
            long_entry = calc_next_long_entry(xk, balance, long_psize, long_pprice, shrt_psize,
                                              highest_bid, ema, available_margin)
        else:
            long_entry = (0.0, np.nan, long_psize, long_pprice, '')

        if xk[15]:
            shrt_entry = calc_next_shrt_entry(xk, balance, long_psize, shrt_psize, shrt_pprice,
                                              lowest_ask, ema, available_margin)
        else:
            shrt_entry = (0.0, np.nan, shrt_psize, shrt_pprice, '')

        if long_entry[0] > 0.0:
            if shrt_entry[0] == 0.0:
                long_first = True
            else:
                long_first = calc_diff(long_entry[1], last_price) < calc_diff(shrt_entry[1], last_price)
        elif shrt_entry[0] < 0.0:
            long_first = False
        else:
            break
        if long_first:
            yield long_entry
            long_psize = long_entry[2]
            long_pprice = long_entry[3]
            if long_entry[1]:
                available_margin -= calc_margin_cost(xk, long_entry[0] * contract_multiplier,
                                                     long_entry[1])
        else:
            yield shrt_entry
            shrt_psize = shrt_entry[2]
            shrt_pprice = shrt_entry[3]
            if shrt_entry[1]:
                available_margin -= calc_margin_cost(xk, shrt_entry[0] * contract_multiplier,
                                                     shrt_entry[1])


@njit
def calc_stop_loss(xk: tuple,
                   balance: float,
                   long_psize: float,
                   long_pprice: float,
                   shrt_psize: float,
                   shrt_pprice: float,
                   liq_price: float,
                   highest_bid: float,
                   lowest_ask: float,
                   last_price: float,
                   available_margin: float):
    '''
    xk index/value
     0 qty_step              6 ddown_factor      11 ema_spread          16 inverse
     1 price_step            7 qty_pct           12 stop_loss_liq_diff  17 min_markup
     3 min_qty               8 leverage          13 stop_loss_pos_pct   18 markup_range
     4 min_cost              9 grid_spacing      14 do_long             19 n_close_orders
     5 contract_multiplier  10 grid_coefficient  15 do_shrt             20
    '''
    # returns (qty, price, psize if taken, pprice if taken, comment)
    abs_shrt_psize = abs(shrt_psize)
    if calc_diff(liq_price, last_price) < xk[12]:
        if long_psize > abs_shrt_psize:
            stop_loss_qty = min(long_psize, max(calc_min_entry_qty(xk, balance, lowest_ask),
                                                round_dn(long_psize * xk[13], xk[0])))
            # if sufficient margin available, increase short pos, otherwise reduce long pos
            margin_cost = calc_margin_cost(xk, stop_loss_qty, lowest_ask)
            if margin_cost < available_margin and xk[15]:
                # add to shrt pos
                shrt_psize, shrt_pprice = calc_new_psize_pprice(xk, shrt_psize, shrt_pprice,
                                                                -stop_loss_qty, lowest_ask)
                return -stop_loss_qty, lowest_ask, shrt_psize, shrt_pprice, 'stop_loss_shrt_entry'
            else:
                # reduce long pos
                long_psize = round_(long_psize - stop_loss_qty, xk[0])
                return -stop_loss_qty, lowest_ask, long_psize, long_pprice, 'stop_loss_long_close'
        else:
            stop_loss_qty = min(abs_shrt_psize, max(calc_min_entry_qty(xk, balance, highest_bid),
                                                    round_dn(abs_shrt_psize * xk[13], xk[0])))
            # if sufficient margin available, increase long pos, otherwise, reduce shrt pos
            margin_cost = calc_margin_cost(xk, stop_loss_qty, highest_bid)
            if margin_cost < available_margin and xk[14]:
                # add to long pos
                long_psize, long_pprice = calc_new_psize_pprice(xk, long_psize, long_pprice,
                                                                stop_loss_qty, highest_bid)
                return stop_loss_qty, highest_bid, long_psize, long_pprice, 'stop_loss_long_entry'
            else:
                # reduce shrt pos
                shrt_psize = round_(shrt_psize + stop_loss_qty, xk[0])
                return stop_loss_qty, highest_bid, shrt_psize, shrt_pprice, 'stop_loss_shrt_close'
    return 0.0, 0.0, 0.0, 0.0, ''


@njit
def calc_next_long_entry(xk: tuple,
                         balance: float,
                         psize: float,
                         pprice: float,
                         highest_bid: float,
                         ema: float,
                         available_margin: float):
    if psize == 0.0:
        price = calc_initial_long_entry_price(xk, ema, highest_bid)
        qty = calc_initial_entry_qty(xk, balance, price, available_margin)
        return qty, price, qty, price, 'initial_long_entry'
    else:
        price = min(round_(highest_bid, xk[1]), calc_reentry_price(xk, balance, psize, pprice, True))
        if price <= 0.0:
            return 0.0, 0.0, psize, pprice, 'long_reentry'
        qty = calc_reentry_qty(xk, psize, price, available_margin)
        psize, pprice = calc_new_psize_pprice(qty_step, psize, pprice, qty, price)
        return qty, price, psize, pprice, 'long_reentry'


@njit
def calc_next_shrt_entry(xk: tuple,
                         balance: float,
                         psize: float,
                         pprice: float,
                         lowest_ask: float,
                         ema: float,
                         available_margin: float):
    if psize == 0.0:
        price = calc_initial_shrt_entry_price(xk, ema, lowest_ask)
        qty = -calc_initial_entry_qty(xk, balance, price, available_margin)
        return qty, price, qty, price, 'initial_long_entry'
    else:
        price = max(round_(lowest_ask, xk[1]), calc_reentry_price(xk, balance, psize, pprice, False))
        if price <= 0.0:
            return 0.0, 0.0, psize, pprice, 'long_reentry'
        qty = calc_reentry_qty(xk, psize, price, available_margin)
        psize, pprice = calc_new_psize_pprice(qty_step, psize, pprice, qty, price)
        return qty, price, psize, pprice, 'long_reentry'


@njit
def calc_next_shrt_entry(xk: float,
                         grid_spacing: float,
                         grid_coefficient: float,
                         ema_spread: float,
                         balance: float,
                         long_psize: float,
                         shrt_psize: float,
                         shrt_pprice: float,
                         lowest_ask: float,
                         ema: float,
                         available_margin: float):
    if shrt_psize == 0.0:
        price = max(lowest_ask, round_up(ema * (1 + ema_spread), price_step))
        shrt_qty = min(round_dn((available_margin / contract_multiplier) * price * leverage, qty_step),
                       calc_min_entry_qty_inverse(qty_step, min_qty, min_cost, qty_pct, leverage,
                                                  balance / contract_multiplier, price))
        if shrt_qty < calc_min_qty_inverse(qty_step, min_qty, min_cost, price):
            shrt_qty = 0.0
        shrt_pprice = price
        return -shrt_qty, price, -shrt_qty, shrt_pprice, 'initial_shrt_entry'
    else:
        pos_margin = calc_margin_cost_inverse(leverage, shrt_psize * contract_multiplier, shrt_pprice)
        price = max(round_(lowest_ask, price_step),
                    calc_shrt_reentry_price(price_step, grid_spacing, grid_coefficient,
                                            balance, pos_margin, shrt_pprice))
        '''
        min_order_qty = -calc_min_entry_qty_inverse(qty_step, min_qty, min_cost, qty_pct,
                                                    leverage, balance, price)
        '''
        min_order_qty = calc_min_qty_inverse(qty_step, min_qty, min_cost, price)

        max_order_qty = round_dn((available_margin / contract_multiplier) * price * leverage, qty_step)
        qty = calc_reentry_qty(qty_step, ddown_factor, min_order_qty, max_order_qty, shrt_psize)
        if qty >= min_order_qty:
            new_psize = shrt_psize - qty
            shrt_pprice = nan_to_0(shrt_pprice) * (shrt_psize / new_psize) + price * (-qty / new_psize)
            margin_cost = calc_margin_cost_inverse(leverage, qty, price)
            return -qty, price, round_(new_psize, qty_step), shrt_pprice, 'shrt_reentry'
        else:
            return 0.0, np.nan, shrt_psize, shrt_pprice, 'shrt_reentry'


@njit
def iter_long_closes(xk: float, balance: float, psize: float, pprice: float, lowest_ask: float):
    '''
    xk index/value
     0 qty_step              6 ddown_factor      11 ema_spread          16 inverse
     1 price_step            7 qty_pct           12 stop_loss_liq_diff  17 min_markup
     3 min_qty               8 leverage          13 stop_loss_pos_pct   18 markup_range
     4 min_cost              9 grid_spacing      14 do_long             19 n_close_orders
     5 contract_multiplier  10 grid_coefficient  15 do_shrt             20
    '''
    # yields (qty, price, psize_if_taken)
    if psize == 0.0:
        return
    minm = pprice * (1 + xk[17])
    prices = np.linspace(minm, pprice * (1 + xk[17] + xk[18]), int(xk[19]))
    prices = [p for p in sorted(set([round_up(p_, xk[1]) for p_ in prices])) if p >= lowest_ask]
    if len(prices) == 0:
        yield -psize, max(lowest_ask, round_up(minm, xk[1])), 0.0
    else:
        n_orders = int(min([xk[19], len(prices), int(psize / xk[3])]))
        for price in prices:
            if n_orders == 0:
                break
            else:
                qty = min(psize, max(calc_initial_entry_qty(xk, balance, lowest_ask),
                                     round_up(psize / n_orders, xk[0])))
                if psize != 0.0 and qty / psize > 0.75:
                    qty = psize
            if qty == 0.0:
                break
            psize = round_(psize - qty, rk[0])
            yield -qty, price, psize
            lowest_ask = price
            n_orders -= 1
        if psize > 0.0:
            yield -psize, max(lowest_ask, round_up(minm, xk[1])), 0.0


@njit
def iter_shrt_closes(xk: float, balance: float, psize: float, pprice: float, highest_bid: float):
    '''
    xk index/value
     0 qty_step              6 ddown_factor      11 ema_spread          16 inverse
     1 price_step            7 qty_pct           12 stop_loss_liq_diff  17 min_markup
     3 min_qty               8 leverage          13 stop_loss_pos_pct   18 markup_range
     4 min_cost              9 grid_spacing      14 do_shrt             19 n_close_orders
     5 contract_multiplier  10 grid_coefficient  15 do_shrt             20
    '''
    # yields (qty, price, psize_if_taken)
    abs_psize = abs(psize)
    if psize == 0.0:
        return
    minm = pprice * (1 - xk[17])
    prices = np.linspace(minm, pprice * (1 - (xk[17] + xk[18])), int(xk[19]))
    prices = [p for p in sorted(set([round_dn(p_, xk[1]) for p_ in prices]), reverse=True)
              if p <= highest_bid]
    if len(prices) == 0:
        yield abs_psize, min(highest_bid, round_dn(minm, xk[1])), 0.0
    else:
        n_orders = int(min([xk[19], len(prices), int(abs_psize / xk[3])]))
        for price in prices:
            if n_orders == 0:
                break
            else:
                qty = min(abs_psize, max(calc_initial_entry_qty(xk, balance, highest_bid),
                                         round_up(abs_psize / n_orders, xk[0])))
                if abs_psize != 0.0 and qty / abs_psize > 0.75:
                    qty = abs_psize
            if qty == 0.0:
                break
            abs_psize = round_(abs_psize - qty, xk[0])
            yield qty, price, abs_psize
            highest_bid = price
            n_orders -= 1
        if abs_psize > 0.0:
            yield abs_psize, min(highest_bid, round_dn(minm, xk[1])), 0.0








