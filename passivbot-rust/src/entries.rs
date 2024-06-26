use crate::types::{BotParams, ExchangeParams, Order, OrderType, Position, StateParams};
use crate::utils::{
    calc_ema_price_ask, calc_ema_price_bid, calc_new_psize_pprice, calc_wallet_exposure,
    calc_wallet_exposure_if_filled, cost_to_qty, interpolate, round_, round_dn, round_up,
};

pub fn calc_initial_entry_long(
    exchange_params: &ExchangeParams,
    state_params: &StateParams,
    bot_params: &BotParams,
    position: &Position,
) -> Order {
    let initial_entry_price = calc_ema_price_bid(
        exchange_params.price_step,
        state_params.order_book.bid,
        state_params.ema_bands.lower,
        bot_params.entry_initial_ema_dist,
    );
    if initial_entry_price <= exchange_params.price_step {
        return Order::default();
    }
    let initial_entry_qty = calc_initial_entry_qty(
        exchange_params,
        bot_params,
        state_params.balance,
        initial_entry_price,
    );
    if position.size == 0.0 {
        // normal initial entry
        Order {
            qty: initial_entry_qty,
            price: initial_entry_price,
            order_type: OrderType::EntryInitialNormalLong,
        }
    } else if position.size < initial_entry_qty * 0.8 {
        Order {
            qty: f64::max(
                calc_min_entry_qty(initial_entry_price, &exchange_params),
                round_dn(initial_entry_qty - position.size, exchange_params.qty_step),
            ),
            price: initial_entry_price,
            order_type: OrderType::EntryInitialPartialLong,
        }
    } else {
        Order::default()
    }
}

pub fn calc_initial_entry_short(
    exchange_params: &ExchangeParams,
    state_params: &StateParams,
    bot_params: &BotParams,
    position: &Position,
) -> Order {
    let initial_entry_price = calc_ema_price_ask(
        exchange_params.price_step,
        state_params.order_book.ask,
        state_params.ema_bands.upper,
        bot_params.entry_initial_ema_dist,
    );
    let initial_entry_qty = calc_initial_entry_qty(
        exchange_params,
        bot_params,
        state_params.balance,
        initial_entry_price,
    );
    let position_size_abs = position.size.abs();
    if position_size_abs == 0.0 {
        // normal initial entry
        Order {
            qty: -initial_entry_qty,
            price: initial_entry_price,
            order_type: OrderType::EntryInitialNormalShort,
        }
    } else if position_size_abs < initial_entry_qty * 0.8 {
        Order {
            qty: -f64::max(
                calc_min_entry_qty(initial_entry_price, &exchange_params),
                round_dn(
                    initial_entry_qty - position_size_abs,
                    exchange_params.qty_step,
                ),
            ),
            price: initial_entry_price,
            order_type: OrderType::EntryInitialPartialShort,
        }
    } else {
        Order::default()
    }
}

pub fn calc_initial_entry_qty(
    exchange_params: &ExchangeParams,
    bot_params: &BotParams,
    balance: f64,
    entry_price: f64,
) -> f64 {
    f64::max(
        calc_min_entry_qty(entry_price, &exchange_params),
        round_(
            cost_to_qty(
                balance * bot_params.wallet_exposure_limit * bot_params.entry_initial_qty_pct,
                entry_price,
                exchange_params.c_mult,
            ),
            exchange_params.qty_step,
        ),
    )
}

pub fn calc_min_entry_qty(entry_price: f64, exchange_params: &ExchangeParams) -> f64 {
    f64::max(
        exchange_params.min_qty,
        round_up(
            cost_to_qty(
                exchange_params.min_cost,
                entry_price,
                exchange_params.c_mult,
            ),
            exchange_params.qty_step,
        ),
    )
}

pub fn calc_grid_entry_long(
    exchange_params: &ExchangeParams,
    state_params: &StateParams,
    bot_params: &BotParams,
    position: &Position,
) -> Order {
    if bot_params.wallet_exposure_limit == 0.0 || state_params.balance <= 0.0 {
        return Order::default();
    }
    let initial_entry_price = calc_ema_price_bid(
        exchange_params.price_step,
        state_params.order_book.bid,
        state_params.ema_bands.lower,
        bot_params.entry_initial_ema_dist,
    );
    if initial_entry_price <= exchange_params.price_step {
        return Order::default();
    }
    let initial_entry_qty = calc_initial_entry_qty(
        exchange_params,
        bot_params,
        state_params.balance,
        initial_entry_price,
    );
    if position.size == 0.0 {
        return Order {
            qty: initial_entry_qty,
            price: initial_entry_price,
            order_type: OrderType::EntryInitialNormalLong,
        };
    } else if position.size < initial_entry_qty * 0.8 {
        return Order {
            qty: f64::max(
                calc_min_entry_qty(initial_entry_price, &exchange_params),
                round_dn(initial_entry_qty - position.size, exchange_params.qty_step),
            ),
            price: initial_entry_price,
            order_type: OrderType::EntryInitialPartialLong,
        };
    }
    let wallet_exposure = calc_wallet_exposure(
        exchange_params.c_mult,
        state_params.balance,
        position.size,
        position.price,
    );
    if wallet_exposure >= bot_params.wallet_exposure_limit * 0.999 {
        return Order::default();
    }

    // normal re-entry
    let reentry_price = calc_reentry_price(
        state_params.balance,
        position.price,
        wallet_exposure,
        state_params.order_book.bid,
        exchange_params,
        bot_params,
    );
    if reentry_price <= 0.0 {
        return Order::default();
    }
    let reentry_qty = f64::max(
        calc_reentry_qty(
            reentry_price,
            state_params.balance,
            position.size,
            exchange_params,
            bot_params,
        ),
        initial_entry_qty,
    );
    let (wallet_exposure_if_filled, reentry_qty_cropped) = calc_cropped_reentry_qty_long(
        exchange_params,
        bot_params,
        position,
        wallet_exposure,
        state_params.balance,
        reentry_qty,
        reentry_price,
    );
    if reentry_qty_cropped < reentry_qty {
        return Order {
            qty: reentry_qty_cropped,
            price: reentry_price,
            order_type: OrderType::EntryGridCroppedLong,
        };
    }
    // preview next order to check if reentry qty is to be inflated
    let (psize_if_filled, pprice_if_filled) = calc_new_psize_pprice(
        position.size,
        position.price,
        reentry_qty,
        reentry_price,
        exchange_params.qty_step,
    );
    let next_reentry_price = calc_reentry_price(
        state_params.balance,
        pprice_if_filled,
        wallet_exposure_if_filled,
        state_params.order_book.bid,
        exchange_params,
        bot_params,
    );
    let next_reentry_qty = f64::max(
        calc_reentry_qty(
            next_reentry_price,
            state_params.balance,
            psize_if_filled,
            exchange_params,
            bot_params,
        ),
        initial_entry_qty,
    );
    let (next_wallet_exposure_if_filled, next_reentry_qty_cropped) = calc_cropped_reentry_qty_long(
        exchange_params,
        bot_params,
        &Position {
            size: psize_if_filled,
            price: pprice_if_filled,
        },
        wallet_exposure_if_filled,
        state_params.balance,
        next_reentry_qty,
        next_reentry_price,
    );
    let effective_double_down_factor = next_reentry_qty_cropped / psize_if_filled;
    if effective_double_down_factor < bot_params.entry_grid_double_down_factor * 0.25 {
        // next reentry too small. Inflate current reentry.
        let new_entry_qty = interpolate(
            bot_params.wallet_exposure_limit,
            &[wallet_exposure, wallet_exposure_if_filled],
            &[position.size, position.size + reentry_qty],
        ) - position.size;
        Order {
            qty: round_(new_entry_qty, exchange_params.qty_step),
            price: reentry_price,
            order_type: OrderType::EntryGridInflatedLong,
        }
    } else {
        Order {
            qty: reentry_qty,
            price: reentry_price,
            order_type: OrderType::EntryGridNormalLong,
        }
    }
}

pub fn calc_cropped_reentry_qty_long(
    exchange_params: &ExchangeParams,
    bot_params: &BotParams,
    position: &Position,
    wallet_exposure: f64,
    balance: f64,
    entry_qty: f64,
    entry_price: f64,
) -> (f64, f64) {
    let wallet_exposure_if_filled = calc_wallet_exposure_if_filled(
        balance,
        position.size,
        position.price,
        entry_qty,
        entry_price,
        &exchange_params,
    );
    if wallet_exposure_if_filled > bot_params.wallet_exposure_limit * 1.01 {
        // reentry too big. Crop current reentry qty.
        let entry_qty = interpolate(
            bot_params.wallet_exposure_limit,
            &[wallet_exposure, wallet_exposure_if_filled],
            &[position.size, position.size + entry_qty],
        ) - position.size;
        (
            wallet_exposure_if_filled,
            round_(entry_qty, exchange_params.qty_step),
        )
    } else {
        (wallet_exposure_if_filled, entry_qty)
    }
}

pub fn calc_next_grid_entry_short(
    exchange_params: &ExchangeParams,
    state_params: &StateParams,
    bot_params: &BotParams,
    position: &Position,
) -> Order {
    if bot_params.wallet_exposure_limit == 0.0 || state_params.balance <= 0.0 {
        return Order::default();
    }

    let initial_entry =
        calc_initial_entry_short(exchange_params, state_params, bot_params, position);
    if initial_entry.qty != 0.0 {
        return initial_entry;
    }
    // here

    let wallet_exposure = calc_wallet_exposure(
        exchange_params.c_mult,
        state_params.balance,
        position.size,
        position.price,
    );
    if wallet_exposure >= bot_params.wallet_exposure_limit * 0.999 {
        return Order::default();
    }

    // normal re-entry
    let (reentry_order, wallet_exposure_if_filled, is_cropped) = calc_reentry_order(
        &exchange_params,
        &bot_params,
        &position,
        &state_params,
        &initial_entry,
    );
    if is_cropped {
        return reentry_order;
    }
    // preview next reentry
    let (next_psize, next_pprice) = calc_new_psize_pprice(
        position.size,
        position.price,
        reentry_order.qty,
        reentry_order.price,
        exchange_params.qty_step,
    );
    let (next_reentry_order, next_wallet_exposure_if_filled, next_is_cropped) = calc_reentry_order(
        &exchange_params,
        &bot_params,
        &Position {
            size: next_psize,
            price: next_pprice,
        },
        &state_params,
        &initial_entry,
    );
    if !next_is_cropped {
        return reentry_order;
    }
    let effective_double_down_factor = next_reentry_order.qty / next_psize;
    if effective_double_down_factor < bot_params.entry_grid_double_down_factor * 0.25 {
        // next reentry too small. Inflate current reentry.
        let new_entry_qty = interpolate(
            bot_params.wallet_exposure_limit,
            &[wallet_exposure, wallet_exposure_if_filled],
            &[position.size, position.size + reentry_order.qty],
        ) - position.size;
        Order {
            qty: round_(new_entry_qty, exchange_params.qty_step),
            price: reentry_order.price,
            order_type: OrderType::EntryGridInflatedLong,
        }
    } else {
        Order {
            qty: reentry_order.qty,
            price: reentry_order.price,
            order_type: OrderType::EntryGridNormalLong,
        }
    }
}

pub fn calc_reentry_qty(
    entry_price: f64,
    balance: f64,
    position_size: f64,
    exchange_params: &ExchangeParams,
    bot_params: &BotParams,
) -> f64 {
    f64::max(
        calc_min_entry_qty(entry_price, &exchange_params),
        round_(
            f64::max(
                position_size.abs() * bot_params.entry_grid_double_down_factor,
                cost_to_qty(balance, entry_price, exchange_params.c_mult)
                    * bot_params.wallet_exposure_limit
                    * bot_params.entry_initial_qty_pct,
            ),
            exchange_params.qty_step,
        ),
    )
}

fn calc_reentry_price(
    balance: f64,
    position_price: f64,
    wallet_exposure: f64,
    order_book_bid: f64,
    exchange_params: &ExchangeParams,
    bot_params: &BotParams,
) -> f64 {
    let multiplier =
        (wallet_exposure / bot_params.wallet_exposure_limit) * bot_params.entry_grid_spacing_weight;
    let entry_price = round_dn(
        position_price * (1.0 - bot_params.entry_grid_spacing_pct * (1.0 + multiplier)),
        exchange_params.price_step,
    );
    let entry_price = f64::min(order_book_bid, entry_price);
    if entry_price <= exchange_params.price_step {
        0.0
    } else {
        entry_price
    }
}

fn calc_reentry_order(
    exchange_params: &ExchangeParams,
    bot_params: &BotParams,
    position: &Position,
    state_params: &StateParams,
    initial_entry: &Order,
) -> (Order, f64, bool) {
    // returns (Order, wallet_exposure_if_filled, is_cropped)
    let wallet_exposure = calc_wallet_exposure(
        exchange_params.c_mult,
        state_params.balance,
        position.size,
        position.price,
    );
    if wallet_exposure >= bot_params.wallet_exposure_limit * 0.999 {
        return (Order::default(), 0.0, false);
    }

    let entry_price = calc_reentry_price(
        state_params.balance,
        position.price,
        wallet_exposure,
        state_params.order_book.bid,
        &exchange_params,
        &bot_params,
    );
    if entry_price == 0.0 {
        return (
            Order {
                qty: 0.0,
                price: 0.0,
                order_type: OrderType::Empty,
            },
            0.0,
            false,
        );
    }
    let entry_qty = f64::max(
        calc_reentry_qty(
            entry_price,
            state_params.balance,
            position.size,
            &exchange_params,
            &bot_params,
        ),
        initial_entry.qty,
    );
    let wallet_exposure_if_filled = calc_wallet_exposure_if_filled(
        state_params.balance,
        position.size,
        position.price,
        entry_qty,
        entry_price,
        &exchange_params,
    );
    if wallet_exposure_if_filled > bot_params.wallet_exposure_limit * 1.01 {
        // reentry too big. Crop current reentry qty.
        let entry_qty = interpolate(
            bot_params.wallet_exposure_limit,
            &[wallet_exposure, wallet_exposure_if_filled],
            &[position.size, position.size + entry_qty],
        ) - position.size;
        (
            Order {
                qty: round_(entry_qty, exchange_params.qty_step),
                price: entry_price,
                order_type: OrderType::EntryGridCroppedLong,
            },
            wallet_exposure_if_filled,
            true,
        )
    } else {
        (
            Order {
                qty: entry_qty,
                price: entry_price,
                order_type: OrderType::EntryGridNormalLong,
            },
            wallet_exposure_if_filled,
            false,
        )
    }
}

pub fn calc_next_entry_long(
    exchange_params: &ExchangeParams,
    state_params: &StateParams,
    bot_params: &BotParams,
    position: &Position,
    min_price_since_open: f64,
    max_price_since_min: f64,
) -> Order {
    // determines whether trailing or grid order, returns Order
    if bot_params.wallet_exposure_limit == 0.0 || state_params.balance <= 0.0 {
        // no orders
        return Order::default();
    }
    if bot_params.entry_trailing_grid_ratio >= 1.0 || bot_params.entry_trailing_grid_ratio <= -1.0 {
        // return trailing only
        return calc_trailing_entry_long(
            &exchange_params,
            &state_params,
            &bot_params,
            &position,
            min_price_since_open,
            max_price_since_min,
        );
    } else if bot_params.entry_trailing_grid_ratio == 0.0 {
        // return grid only
        return calc_grid_entry_long(&exchange_params, &state_params, &bot_params, &position);
    }
    let wallet_exposure = calc_wallet_exposure(
        exchange_params.c_mult,
        state_params.balance,
        position.size,
        position.price,
    );
    let wallet_exposure_ratio = wallet_exposure / bot_params.wallet_exposure_limit;
    if bot_params.entry_trailing_grid_ratio > 0.0 {
        // trailing first
        if wallet_exposure_ratio < bot_params.entry_trailing_grid_ratio {
            // return trailing order, but crop to max bot_params.wallet_exposure_limit * bot_params.entry_trailing_grid_ratio + 1%
            if wallet_exposure == 0.0 {
                calc_trailing_entry_long(
                    &exchange_params,
                    &state_params,
                    &bot_params,
                    &position,
                    min_price_since_open,
                    max_price_since_min,
                )
            } else {
                let mut bot_params_modified = bot_params.clone();
                bot_params_modified.wallet_exposure_limit =
                    bot_params.wallet_exposure_limit * bot_params.entry_trailing_grid_ratio * 1.01;
                calc_trailing_entry_long(
                    &exchange_params,
                    &state_params,
                    &bot_params_modified,
                    &position,
                    min_price_since_open,
                    max_price_since_min,
                )
            }
        } else {
            // return grid order
            calc_grid_entry_long(&exchange_params, &state_params, &bot_params, &position)
        }
    } else {
        // grid first
        if wallet_exposure_ratio < 1.0 + bot_params.entry_trailing_grid_ratio {
            // return grid order, but crop to max bot_params.wallet_exposure_limit * (1.0 + bot_params.entry_trailing_grid_ratio) + 1%
            if wallet_exposure == 0.0 {
                calc_grid_entry_long(&exchange_params, &state_params, &bot_params, &position)
            } else {
                let mut bot_params_modified = bot_params.clone();
                if wallet_exposure != 0.0 {
                    bot_params_modified.wallet_exposure_limit = bot_params.wallet_exposure_limit
                        * (1.0 + bot_params.entry_trailing_grid_ratio)
                        * 1.01;
                }
                calc_grid_entry_long(
                    &exchange_params,
                    &state_params,
                    &bot_params_modified,
                    &position,
                )
            }
        } else {
            calc_trailing_entry_long(
                &exchange_params,
                &state_params,
                &bot_params,
                &position,
                min_price_since_open,
                max_price_since_min,
            )
        }
    }
}

/*
pub fn calc_next_entry_short(
    exchange_params: &ExchangeParams,
    state_params: &StateParams,
    bot_params: &BotParams,
    position: &Position,
    min_price_since_open: f64,
    max_price_since_min: f64,
) -> Order {
    // determines whether trailing or grid order, returns Order
    if bot_params.wallet_exposure_limit == 0.0 || state_params.balance <= 0.0 {
        // no orders
        return Order::default();
    }
    let wallet_exposure = calc_wallet_exposure(
        exchange_params.c_mult,
        state_params.balance,
        position.size,
        position.price,
    );
    if wallet_exposure >= bot_params.wallet_exposure_limit * 0.999 {
        // wallet exposure exceeded; return empty order
        return Order::default();
    }
    let initial_entry = calc_initial_entry_short(exchange_params, state_params, bot_params, position);
    if initial_entry.qty != 0.0 {
        // initial entry
        return initial_entry;
    }
    if bot_params.entry_trailing_grid_ratio >= 1.0 || bot_params.entry_trailing_grid_ratio <= -1.0 {
        // return trailing only
        return calc_trailing_entry_short(
            &exchange_params,
            &state_params,
            &bot_params,
            &position,
            min_price_since_open,
            max_price_since_min,
        );
    }
    let wallet_exposure_ratio = wallet_exposure / bot_params.wallet_exposure_limit;
    if bot_params.entry_trailing_grid_ratio > 0.0 {
        // trailing first
        if wallet_exposure_ratio < bot_params.entry_trailing_grid_ratio {
            // return trailing order, but crop to max bot_params.wallet_exposure_limit * bot_params.entry_trailing_grid_ratio + 1%
            let mut bot_params_modified = bot_params.clone();
            bot_params_modified.wallet_exposure_limit =
                bot_params.wallet_exposure_limit * bot_params.entry_trailing_grid_ratio * 1.01;
            return calc_trailing_entry_short(
                &exchange_params,
                &state_params,
                &bot_params_modified,
                &position,
                min_price_since_open,
                max_price_since_min,
            );
        } else {
            // return grid order
            return calc_next_grid_entry_short(
                &exchange_params,
                &state_params,
                &bot_params,
                &position,
            );
        }
    }
    if bot_params.entry_trailing_grid_ratio < 0.0 {
        // grid first
        if wallet_exposure_ratio < 1.0 + bot_params.entry_trailing_grid_ratio {
            // return grid order, but crop to max bot_params.wallet_exposure_limit * (1.0 + bot_params.entry_trailing_grid_ratio) + 1%
            let mut bot_params_modified = bot_params.clone();
            bot_params_modified.wallet_exposure_limit = bot_params.wallet_exposure_limit
                * (1.0 + bot_params.entry_trailing_grid_ratio)
                * 1.01;
            return calc_next_grid_entry_short(
                &exchange_params,
                &state_params,
                &bot_params_modified,
                &position,
            );
        } else {
            return calc_trailing_entry_short(
                &exchange_params,
                &state_params,
                &bot_params,
                &position,
                min_price_since_open,
                max_price_since_min,
            );
        }
    }
    // return grid only
    calc_next_grid_entry_short(&exchange_params, &state_params, &bot_params, &position)
}
*/

pub fn calc_trailing_entry_long(
    exchange_params: &ExchangeParams,
    state_params: &StateParams,
    bot_params: &BotParams,
    position: &Position,
    min_price_since_open: f64,
    max_price_since_min: f64,
) -> Order {
    let initial_entry_price = calc_ema_price_bid(
        exchange_params.price_step,
        state_params.order_book.bid,
        state_params.ema_bands.lower,
        bot_params.entry_initial_ema_dist,
    );
    if initial_entry_price <= exchange_params.price_step {
        return Order::default();
    }
    let initial_entry_qty = calc_initial_entry_qty(
        exchange_params,
        bot_params,
        state_params.balance,
        initial_entry_price,
    );
    if position.size == 0.0 {
        // normal initial entry
        return Order {
            qty: initial_entry_qty,
            price: initial_entry_price,
            order_type: OrderType::EntryInitialNormalLong,
        };
    } else if position.size < initial_entry_qty * 0.8 {
        return Order {
            qty: f64::max(
                calc_min_entry_qty(initial_entry_price, &exchange_params),
                round_dn(initial_entry_qty - position.size, exchange_params.qty_step),
            ),
            price: initial_entry_price,
            order_type: OrderType::EntryInitialPartialLong,
        };
    }
    if min_price_since_open > position.price * (1.0 - bot_params.entry_trailing_threshold_pct) {
        return Order {
            qty: 0.0,
            price: 0.0,
            order_type: OrderType::EntryTrailingNormalLong,
        };
    }
    if max_price_since_min < min_price_since_open * (1.0 + bot_params.entry_trailing_drawdown_pct) {
        return Order {
            qty: 0.0,
            price: 0.0,
            order_type: OrderType::EntryTrailingNormalLong,
        };
    }
    let wallet_exposure = calc_wallet_exposure(
        exchange_params.c_mult,
        state_params.balance,
        position.size,
        position.price,
    );
    if wallet_exposure > bot_params.wallet_exposure_limit * 0.999 {
        return Order::default();
    }
    let reentry_qty = f64::max(
        calc_reentry_qty(
            state_params.order_book.bid,
            state_params.balance,
            position.size,
            &exchange_params,
            &bot_params,
        ),
        initial_entry_qty,
    );
    let reentry_price = f64::min(
        state_params.order_book.bid,
        round_dn(
            position.price
                * (1.0 - bot_params.entry_trailing_threshold_pct
                    + bot_params.entry_trailing_drawdown_pct),
            exchange_params.price_step,
        ),
    );
    let (wallet_exposure_if_filled, reentry_qty_cropped) = calc_cropped_reentry_qty_long(
        exchange_params,
        bot_params,
        position,
        wallet_exposure,
        state_params.balance,
        reentry_qty,
        reentry_price,
    );
    if reentry_qty_cropped < reentry_qty {
        Order {
            qty: reentry_qty_cropped,
            price: reentry_price,
            order_type: OrderType::EntryTrailingCroppedLong,
        }
    } else {
        Order {
            qty: reentry_qty,
            price: reentry_price,
            order_type: OrderType::EntryTrailingNormalLong,
        }
    }
}

pub fn calc_trailing_entry_short(
    exchange_params: &ExchangeParams,
    state_params: &StateParams,
    bot_params: &BotParams,
    position: &Position,
    max_price_since_open: f64,
    min_price_since_max: f64,
) -> Order {
    if position.size == 0.0 || bot_params.wallet_exposure_limit <= 0.0 {
        return calc_initial_entry_short(exchange_params, state_params, bot_params, position);
    }
    if max_price_since_open < position.price * (1.0 + bot_params.entry_trailing_threshold_pct) {
        return Order {
            qty: 0.0,
            price: 0.0,
            order_type: OrderType::EntryTrailingNormalShort,
        };
    }
    if min_price_since_max > max_price_since_open * (1.0 - bot_params.entry_trailing_drawdown_pct) {
        return Order {
            qty: 0.0,
            price: 0.0,
            order_type: OrderType::EntryTrailingNormalShort,
        };
    }
    let wallet_exposure = calc_wallet_exposure(
        exchange_params.c_mult,
        state_params.balance,
        position.size,
        position.price,
    );
    if wallet_exposure > bot_params.wallet_exposure_limit * 0.999 {
        return Order::default();
    }
    let entry_qty = -calc_reentry_qty(
        state_params.order_book.ask,
        state_params.balance,
        position.size,
        &exchange_params,
        &bot_params,
    );
    let wallet_exposure_if_filled = calc_wallet_exposure_if_filled(
        state_params.balance,
        position.size,
        position.price,
        entry_qty,
        state_params.order_book.ask,
        &exchange_params,
    );
    let entry_price = f64::max(
        state_params.order_book.ask,
        round_up(
            position.price
                * (1.0 + bot_params.entry_trailing_threshold_pct
                    - bot_params.entry_trailing_drawdown_pct),
            exchange_params.price_step,
        ),
    );
    if wallet_exposure_if_filled > bot_params.wallet_exposure_limit * 1.01 {
        // reentry too big. Crop current reentry qty.
        let entry_qty = interpolate(
            bot_params.wallet_exposure_limit,
            &[wallet_exposure, wallet_exposure_if_filled],
            &[position.size, position.size + entry_qty],
        ) - position.size;
        Order {
            qty: round_(entry_qty, exchange_params.qty_step),
            price: entry_price,
            order_type: OrderType::EntryTrailingCroppedShort,
        }
    } else {
        Order {
            qty: entry_qty,
            price: entry_price,
            order_type: OrderType::EntryTrailingNormalShort,
        }
    }
}
