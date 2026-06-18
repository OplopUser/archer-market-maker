use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum PolicyMode {
    Shadow,
    Live,
    OfflinePreview,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct SidePolicy {
    pub enabled: bool,
    pub spreads_bps: Vec<f64>,
    pub level_sizes_base: Vec<f64>,
    #[serde(default = "default_size_multiplier")]
    pub size_multiplier: f64,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct QuotePolicy {
    pub market_pair: String,
    pub quote_enabled: bool,
    pub mode: PolicyMode,
    pub level_count: usize,
    pub bid: SidePolicy,
    pub ask: SidePolicy,
    #[serde(default)]
    pub reason_codes: Vec<String>,
    #[serde(default)]
    pub timestamp_ms: Option<u64>,
    #[serde(default)]
    pub max_age_ms: Option<u64>,
}

fn default_size_multiplier() -> f64 {
    1.0
}
