use solana_sdk::pubkey::Pubkey;

use super::types::MarketStateHeader;

#[derive(Debug, Clone)]
pub struct MarketConfig {
    pub market_pubkey: Pubkey,
    pub base_mint: Pubkey,
    pub quote_mint: Pubkey,
    pub base_atoms_per_base_lot: u64,
    pub quote_atoms_per_quote_lot: u64,
    pub tick_size_in_quote_atoms_per_base_unit: u64,
    pub raw_base_units_per_base_unit: u64,
    pub maker_fee_ppm: i32,
    pub taker_fee_ppm: i32,
    pub base_decimals: u8,
    pub quote_decimals: u8,
    pub base_vault: Pubkey,
    pub quote_vault: Pubkey,
    pub base_token_program: Pubkey,
    pub quote_token_program: Pubkey,
    // Precomputed conversion factors
    ticks_to_price_factor: f64,
    lots_to_base_amount_factor: f64,
    lots_to_quote_amount_factor: f64,
}

impl MarketConfig {
    pub fn from_header(
        market_pubkey: Pubkey,
        header: &MarketStateHeader,
        base_decimals: u8,
        quote_decimals: u8,
        base_token_program: Pubkey,
        quote_token_program: Pubkey,
    ) -> Self {
        let quote_atoms_divisor = 10f64.powi(quote_decimals as i32);
        let base_atoms_divisor = 10f64.powi(base_decimals as i32);

        let ticks_to_price_factor = header.tick_size_in_quote_atoms_per_base_unit as f64
            / (header.raw_base_units_per_base_unit as f64 * quote_atoms_divisor);

        let lots_to_base_amount_factor = header.base_atoms_per_base_lot as f64 / base_atoms_divisor;

        let lots_to_quote_amount_factor =
            header.quote_atoms_per_quote_lot as f64 / quote_atoms_divisor;

        let base_vault = spl_associated_token_account::get_associated_token_address_with_program_id(
            &market_pubkey,
            &header.base_mint,
            &base_token_program,
        );
        let quote_vault =
            spl_associated_token_account::get_associated_token_address_with_program_id(
                &market_pubkey,
                &header.quote_mint,
                &quote_token_program,
            );

        Self {
            market_pubkey,
            base_mint: header.base_mint,
            quote_mint: header.quote_mint,
            base_atoms_per_base_lot: header.base_atoms_per_base_lot,
            quote_atoms_per_quote_lot: header.quote_atoms_per_quote_lot,
            tick_size_in_quote_atoms_per_base_unit: header.tick_size_in_quote_atoms_per_base_unit,
            raw_base_units_per_base_unit: header.raw_base_units_per_base_unit,
            maker_fee_ppm: header.maker_fee_ppm,
            taker_fee_ppm: header.taker_fee_ppm,
            base_decimals,
            quote_decimals,
            base_vault,
            quote_vault,
            base_token_program,
            quote_token_program,
            ticks_to_price_factor,
            lots_to_base_amount_factor,
            lots_to_quote_amount_factor,
        }
    }

    #[inline]
    pub fn ticks_to_price_factor(&self) -> f64 {
        self.ticks_to_price_factor
    }

    #[inline]
    pub fn price_to_ticks_factor(&self) -> f64 {
        1.0 / self.ticks_to_price_factor
    }

    #[inline]
    pub fn lots_to_base_factor(&self) -> f64 {
        self.lots_to_base_amount_factor
    }

    #[inline]
    pub fn lots_to_quote_factor(&self) -> f64 {
        self.lots_to_quote_amount_factor
    }

    #[inline]
    pub fn base_to_lots_factor(&self) -> f64 {
        1.0 / self.lots_to_base_amount_factor
    }

    #[inline]
    pub fn quote_to_lots_factor(&self) -> f64 {
        1.0 / self.lots_to_quote_amount_factor
    }
}
