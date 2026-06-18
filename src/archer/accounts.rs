use anyhow::Result;

use super::config::MarketConfig;
use super::math::{base_lots_to_amount, quote_lots_to_amount};
use super::types::{MakerBook, MarketStateHeader};

#[derive(Debug, Clone, Copy)]
pub struct MakerBalances {
    pub base_free: f64,
    pub base_locked: f64,
    pub quote_free: f64,
    pub quote_locked: f64,
    pub base_total: f64,
    pub quote_total: f64,
}

pub fn parse_market_state(data: &[u8]) -> Result<&MarketStateHeader> {
    MarketStateHeader::load(data)
}

pub fn maker_balances(book: &MakerBook, config: &MarketConfig) -> MakerBalances {
    let base_free = base_lots_to_amount(book.base_free, config);
    let base_locked = base_lots_to_amount(book.base_locked, config);
    let quote_free = quote_lots_to_amount(book.quote_free, config);
    let quote_locked = quote_lots_to_amount(book.quote_locked, config);
    MakerBalances {
        base_free,
        base_locked,
        quote_free,
        quote_locked,
        base_total: base_free + base_locked,
        quote_total: quote_free + quote_locked,
    }
}

pub fn active_bid_levels(book: &MakerBook) -> usize {
    book.bid_levels
        .iter()
        .take_while(|l| l.size_in_base_lots > 0)
        .count()
}

pub fn active_ask_levels(book: &MakerBook) -> usize {
    book.ask_levels
        .iter()
        .take_while(|l| l.size_in_base_lots > 0)
        .count()
}
