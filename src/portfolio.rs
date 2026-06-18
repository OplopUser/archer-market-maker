use serde::Serialize;

#[derive(Debug, Clone, PartialEq)]
pub struct PortfolioExposureInput {
    pub run_id: String,
    pub market: String,
    pub base_asset: String,
    pub quote_asset: String,
    pub mid_price: f64,
    pub maker_book: MakerBookExposure,
    pub wallet: WalletExposure,
    pub pending: PendingExposure,
}

#[derive(Debug, Clone, Copy, PartialEq)]
pub struct MakerBookExposure {
    pub base_free: f64,
    pub base_locked: f64,
    pub quote_free: f64,
    pub quote_locked: f64,
    pub active_bid_levels: u64,
    pub active_ask_levels: u64,
}

#[derive(Debug, Clone, Copy, PartialEq)]
pub struct WalletExposure {
    pub base_wallet: f64,
    pub quote_wallet: f64,
    pub native_sol: Option<f64>,
}

#[derive(Debug, Clone, Copy, PartialEq)]
pub struct PendingExposure {
    pub pending_bid_notional: f64,
    pub pending_ask_base: f64,
    pub in_flight_clear_notional: f64,
    pub in_flight_update_notional: f64,
}

#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct PortfolioExposureExport {
    pub schema_version: u16,
    pub venue: String,
    pub run_id: String,
    pub market: String,
    pub base_asset: String,
    pub quote_asset: String,
    pub mid_price: f64,
    pub base_net: f64,
    pub quote_net: f64,
    pub base_notional_usdc: f64,
    pub maker_book: MakerBookExport,
    pub wallet: WalletExport,
    pub pending: PendingExport,
    pub active_levels: ActiveLevels,
    pub in_flight: InFlightExposure,
    pub hedge_target: PhoenixHedgeTarget,
}

#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct MakerBookExport {
    pub base_free: f64,
    pub base_locked: f64,
    pub quote_free: f64,
    pub quote_locked: f64,
}

#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct WalletExport {
    pub base_wallet: f64,
    pub quote_wallet: f64,
    pub native_sol: Option<f64>,
}

#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct PendingExport {
    pub pending_bid_notional: f64,
    pub pending_ask_base: f64,
}

#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct ActiveLevels {
    pub bid: u64,
    pub ask: u64,
    pub total: u64,
}

#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct InFlightExposure {
    pub clear_notional: f64,
    pub update_notional: f64,
    pub pending_bid_notional: f64,
    pub total_notional: f64,
}

#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct PhoenixHedgeTarget {
    pub market: String,
    pub base_asset: String,
    pub quote_asset: String,
    pub venue: String,
    pub target_base_delta: f64,
    pub target_quote_delta: f64,
    pub source: String,
}

pub fn build_portfolio_exposure_export(input: PortfolioExposureInput) -> PortfolioExposureExport {
    let base_net =
        input.maker_book.base_free + input.maker_book.base_locked + input.wallet.base_wallet
            - input.pending.pending_ask_base;
    let quote_net =
        input.maker_book.quote_free + input.maker_book.quote_locked + input.wallet.quote_wallet;
    let base_notional_usdc = base_net * input.mid_price;
    let total_in_flight = input.pending.pending_bid_notional
        + input.pending.in_flight_clear_notional
        + input.pending.in_flight_update_notional;

    PortfolioExposureExport {
        schema_version: 1,
        venue: "archer".to_string(),
        run_id: input.run_id,
        market: input.market.clone(),
        base_asset: input.base_asset.clone(),
        quote_asset: input.quote_asset.clone(),
        mid_price: input.mid_price,
        base_net,
        quote_net,
        base_notional_usdc,
        maker_book: MakerBookExport {
            base_free: input.maker_book.base_free,
            base_locked: input.maker_book.base_locked,
            quote_free: input.maker_book.quote_free,
            quote_locked: input.maker_book.quote_locked,
        },
        wallet: WalletExport {
            base_wallet: input.wallet.base_wallet,
            quote_wallet: input.wallet.quote_wallet,
            native_sol: input.wallet.native_sol,
        },
        pending: PendingExport {
            pending_bid_notional: input.pending.pending_bid_notional,
            pending_ask_base: input.pending.pending_ask_base,
        },
        active_levels: ActiveLevels {
            bid: input.maker_book.active_bid_levels,
            ask: input.maker_book.active_ask_levels,
            total: input.maker_book.active_bid_levels + input.maker_book.active_ask_levels,
        },
        in_flight: InFlightExposure {
            clear_notional: input.pending.in_flight_clear_notional,
            update_notional: input.pending.in_flight_update_notional,
            pending_bid_notional: input.pending.pending_bid_notional,
            total_notional: total_in_flight,
        },
        hedge_target: PhoenixHedgeTarget {
            market: input.market,
            base_asset: input.base_asset,
            quote_asset: input.quote_asset,
            venue: "archer".to_string(),
            target_base_delta: -base_net,
            target_quote_delta: -quote_net,
            source: "archer_portfolio_exposure_export".to_string(),
        },
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn exports_archer_inventory_fields_for_portfolio_hedge_target() {
        let export = build_portfolio_exposure_export(PortfolioExposureInput {
            run_id: "shadow-run-001".to_string(),
            market: "SOL/USDC".to_string(),
            base_asset: "SOL".to_string(),
            quote_asset: "USDC".to_string(),
            mid_price: 134.0,
            maker_book: MakerBookExposure {
                base_free: 1.0,
                base_locked: 0.25,
                quote_free: 100.0,
                quote_locked: 30.0,
                active_bid_levels: 2,
                active_ask_levels: 1,
            },
            wallet: WalletExposure {
                base_wallet: 0.5,
                quote_wallet: 70.0,
                native_sol: Some(0.2),
            },
            pending: PendingExposure {
                pending_bid_notional: 25.0,
                pending_ask_base: 0.1,
                in_flight_clear_notional: 10.0,
                in_flight_update_notional: 20.0,
            },
        });

        assert_eq!(export.venue, "archer");
        assert_eq!(export.active_levels.total, 3);
        assert_eq!(export.hedge_target.market, "SOL/USDC");
        assert!((export.base_net - 1.65).abs() < f64::EPSILON);
        assert!((export.quote_net - 200.0).abs() < f64::EPSILON);
        assert!((export.in_flight.total_notional - 55.0).abs() < f64::EPSILON);
    }
}
