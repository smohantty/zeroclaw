use serde::{Deserialize, Serialize};

/// Token usage information from a single API call.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct TokenUsage {
    /// Model identifier (e.g., "anthropic/claude-sonnet-4-20250514")
    pub model: String,
    /// Input/prompt tokens
    pub input_tokens: u64,
    /// Output/completion tokens
    pub output_tokens: u64,
    /// Total tokens
    pub total_tokens: u64,
    /// Calculated cost in USD
    pub cost_usd: f64,
    /// Timestamp of the request
    pub timestamp: chrono::DateTime<chrono::Utc>,
}

impl TokenUsage {
    fn sanitize_price(value: f64) -> f64 {
        if value.is_finite() && value > 0.0 {
            value
        } else {
            0.0
        }
    }

    /// Create a new token usage record (no cache-aware pricing).
    pub fn new(
        model: impl Into<String>,
        input_tokens: u64,
        output_tokens: u64,
        input_price_per_million: f64,
        output_price_per_million: f64,
    ) -> Self {
        Self::with_cache(
            model,
            input_tokens,
            output_tokens,
            0,
            0,
            input_price_per_million,
            output_price_per_million,
            0.0,
            0.0,
        )
    }

    /// Create a token usage record with cache-aware pricing.
    ///
    /// Anthropic reports token counts as:
    /// - `input_tokens`: fresh (non-cached) input tokens
    /// - `cache_read_tokens`: tokens served from prompt cache
    /// - `cache_write_tokens`: tokens written to prompt cache
    /// - `output_tokens`: generated tokens
    ///
    /// The user-facing `input_tokens` field is the **total** of all input
    /// (fresh + cache_read + cache_write). Cost is calculated per-bucket.
    /// When `cache_write_price` or `cache_read_price` is 0, falls back to `input_price`.
    pub fn with_cache(
        model: impl Into<String>,
        fresh_input_tokens: u64,
        output_tokens: u64,
        cache_read_tokens: u64,
        cache_write_tokens: u64,
        input_price_per_million: f64,
        output_price_per_million: f64,
        cache_write_price_per_million: f64,
        cache_read_price_per_million: f64,
    ) -> Self {
        let model = model.into();
        let input_price = Self::sanitize_price(input_price_per_million);
        let output_price = Self::sanitize_price(output_price_per_million);
        // Fall back to input price when cache prices are unset (negative sentinel).
        // Explicit 0.0 means "free" (no charge for cached tokens).
        let cache_write_price = if cache_write_price_per_million >= 0.0
            && cache_write_price_per_million.is_finite()
        {
            cache_write_price_per_million
        } else {
            input_price
        };
        let cache_read_price = if cache_read_price_per_million >= 0.0
            && cache_read_price_per_million.is_finite()
        {
            cache_read_price_per_million
        } else {
            input_price
        };

        // User-visible totals: all input lumped together, output separate.
        let total_input = fresh_input_tokens
            .saturating_add(cache_read_tokens)
            .saturating_add(cache_write_tokens);
        let total_tokens = total_input.saturating_add(output_tokens);

        // Actual cost per bucket.
        let fresh_cost = (fresh_input_tokens as f64 / 1_000_000.0) * input_price;
        let cache_write_cost = (cache_write_tokens as f64 / 1_000_000.0) * cache_write_price;
        let cache_read_cost = (cache_read_tokens as f64 / 1_000_000.0) * cache_read_price;
        let output_cost = (output_tokens as f64 / 1_000_000.0) * output_price;
        let cost_usd = fresh_cost + cache_write_cost + cache_read_cost + output_cost;

        Self {
            model,
            input_tokens: total_input,
            output_tokens,
            total_tokens,
            cost_usd,
            timestamp: chrono::Utc::now(),
        }
    }

    /// Get the total cost.
    pub fn cost(&self) -> f64 {
        self.cost_usd
    }
}

/// Time period for cost aggregation.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub enum UsagePeriod {
    Session,
    Day,
    Month,
}

/// A single cost record for persistent storage.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct CostRecord {
    /// Unique identifier
    pub id: String,
    /// Token usage details
    pub usage: TokenUsage,
    /// Session identifier (for grouping)
    pub session_id: String,
}

impl CostRecord {
    /// Create a new cost record.
    pub fn new(session_id: impl Into<String>, usage: TokenUsage) -> Self {
        Self {
            id: uuid::Uuid::new_v4().to_string(),
            usage,
            session_id: session_id.into(),
        }
    }
}

/// Budget enforcement result.
#[derive(Debug, Clone)]
pub enum BudgetCheck {
    /// Within budget, request can proceed
    Allowed,
    /// Warning threshold exceeded but request can proceed
    Warning {
        current_usd: f64,
        limit_usd: f64,
        period: UsagePeriod,
    },
    /// Budget exceeded, request blocked
    Exceeded {
        current_usd: f64,
        limit_usd: f64,
        period: UsagePeriod,
    },
}

/// Cost summary for reporting.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct CostSummary {
    /// Total cost for the session
    pub session_cost_usd: f64,
    /// Total cost for the day
    pub daily_cost_usd: f64,
    /// Total cost for the month
    pub monthly_cost_usd: f64,
    /// Total tokens used
    pub total_tokens: u64,
    /// Number of requests
    pub request_count: usize,
    /// Breakdown by model
    pub by_model: std::collections::HashMap<String, ModelStats>,
}

/// Statistics for a specific model.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ModelStats {
    /// Model name
    pub model: String,
    /// Total cost for this model
    pub cost_usd: f64,
    /// Total tokens for this model
    pub total_tokens: u64,
    /// Number of requests for this model
    pub request_count: usize,
}

impl Default for CostSummary {
    fn default() -> Self {
        Self {
            session_cost_usd: 0.0,
            daily_cost_usd: 0.0,
            monthly_cost_usd: 0.0,
            total_tokens: 0,
            request_count: 0,
            by_model: std::collections::HashMap::new(),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn token_usage_calculation() {
        let usage = TokenUsage::new("test/model", 1000, 500, 3.0, 15.0);

        // Expected: (1000/1M)*3 + (500/1M)*15 = 0.003 + 0.0075 = 0.0105
        assert!((usage.cost_usd - 0.0105).abs() < 0.0001);
        assert_eq!(usage.input_tokens, 1000);
        assert_eq!(usage.output_tokens, 500);
        assert_eq!(usage.total_tokens, 1500);
    }

    #[test]
    fn token_usage_zero_tokens() {
        let usage = TokenUsage::new("test/model", 0, 0, 3.0, 15.0);
        assert!(usage.cost_usd.abs() < f64::EPSILON);
        assert_eq!(usage.total_tokens, 0);
    }

    #[test]
    fn token_usage_negative_or_non_finite_prices_are_clamped() {
        let usage = TokenUsage::new("test/model", 1000, 1000, -3.0, f64::NAN);
        assert!(usage.cost_usd.abs() < f64::EPSILON);
        assert_eq!(usage.total_tokens, 2000);
    }

    #[test]
    fn cost_record_creation() {
        let usage = TokenUsage::new("test/model", 100, 50, 1.0, 2.0);
        let record = CostRecord::new("session-123", usage);

        assert_eq!(record.session_id, "session-123");
        assert!(!record.id.is_empty());
        assert_eq!(record.usage.model, "test/model");
    }
}
