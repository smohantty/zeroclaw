use crate::config::schema::ModelPricing;
use crate::cost::types::{BudgetCheck, TokenUsage as CostTokenUsage};
use crate::cost::CostTracker;
use std::sync::Arc;

// ── Cost tracking via task-local ──

/// Context for cost tracking within the tool call loop.
/// Scoped via `tokio::task_local!` at call sites (channels, gateway).
#[derive(Clone)]
pub(crate) struct ToolLoopCostTrackingContext {
    pub tracker: Arc<CostTracker>,
    pub prices: Arc<std::collections::HashMap<String, ModelPricing>>,
}

impl ToolLoopCostTrackingContext {
    pub(crate) fn new(
        tracker: Arc<CostTracker>,
        prices: Arc<std::collections::HashMap<String, ModelPricing>>,
    ) -> Self {
        Self { tracker, prices }
    }
}

tokio::task_local! {
    pub(crate) static TOOL_LOOP_COST_TRACKING_CONTEXT: Option<ToolLoopCostTrackingContext>;
}

/// 3-tier model pricing lookup: direct name → provider/model → suffix after last `/`.
fn lookup_pricing<'a>(
    prices: &'a std::collections::HashMap<String, ModelPricing>,
    provider_name: &str,
    model: &str,
) -> Option<&'a ModelPricing> {
    prices
        .get(model)
        .or_else(|| prices.get(&format!("{provider_name}/{model}")))
        .or_else(|| {
            model
                .rsplit_once('/')
                .and_then(|(_, suffix)| prices.get(suffix))
        })
}

/// Build a `CostTokenUsage` from provider token usage and pricing,
/// accounting for cache read/write token buckets.
fn build_cost_usage(
    model: &str,
    usage: &crate::providers::traits::TokenUsage,
    pricing: Option<&ModelPricing>,
) -> Option<CostTokenUsage> {
    let input_tokens = usage.input_tokens.unwrap_or(0);
    let output_tokens = usage.output_tokens.unwrap_or(0);
    let cache_read = usage.cached_input_tokens.unwrap_or(0);
    let cache_write = usage.cache_creation_input_tokens.unwrap_or(0);
    let total = input_tokens
        .saturating_add(output_tokens)
        .saturating_add(cache_read)
        .saturating_add(cache_write);
    if total == 0 {
        return None;
    }

    Some(CostTokenUsage::with_cache(
        model,
        input_tokens,
        output_tokens,
        cache_read,
        cache_write,
        pricing.map_or(0.0, |p| p.input),
        pricing.map_or(0.0, |p| p.output),
        pricing.map_or(0.0, |p| p.cache_write),
        pricing.map_or(0.0, |p| p.cache_read),
    ))
}

/// Record token usage from an LLM response via the task-local cost tracker.
/// Returns `(total_tokens, cost_usd)` on success, `None` when not scoped or no usage.
pub(crate) fn record_tool_loop_cost_usage(
    provider_name: &str,
    model: &str,
    usage: &crate::providers::traits::TokenUsage,
) -> Option<(u64, f64)> {
    let ctx = TOOL_LOOP_COST_TRACKING_CONTEXT
        .try_with(Clone::clone)
        .ok()
        .flatten()?;

    let pricing = lookup_pricing(&ctx.prices, provider_name, model);
    let cost_usage = build_cost_usage(model, usage, pricing)?;

    if pricing.is_none() {
        tracing::debug!(
            provider = provider_name,
            model,
            "Cost tracking recorded token usage with zero pricing (no pricing entry found)"
        );
    }

    if let Err(error) = ctx.tracker.record_usage(cost_usage.clone()) {
        tracing::warn!(
            provider = provider_name,
            model,
            "Failed to record cost tracking usage: {error}"
        );
    }

    Some((cost_usage.total_tokens, cost_usage.cost_usd))
}

/// Record token usage directly given a tracker and pricing map (no task-local needed).
/// Returns `(total_tokens, cost_usd)` on success, `None` when no usage.
pub(crate) fn record_cost_direct(
    tracker: &CostTracker,
    prices: &std::collections::HashMap<String, ModelPricing>,
    provider_name: &str,
    model: &str,
    usage: &crate::providers::traits::TokenUsage,
) -> Option<(u64, f64)> {
    let pricing = lookup_pricing(prices, provider_name, model);
    let cost_usage = build_cost_usage(model, usage, pricing)?;

    if pricing.is_none() {
        tracing::debug!(
            provider = provider_name,
            model,
            "Cost tracking (direct) recorded token usage with zero pricing (no pricing entry found)"
        );
    }

    if let Err(error) = tracker.record_usage(cost_usage.clone()) {
        tracing::warn!(
            provider = provider_name,
            model,
            "Failed to record cost tracking usage (direct): {error}"
        );
    }

    Some((cost_usage.total_tokens, cost_usage.cost_usd))
}

/// Check budget before an LLM call. Returns `None` when no cost tracking
/// context is scoped (tests, delegate, CLI without cost config).
pub(crate) fn check_tool_loop_budget() -> Option<BudgetCheck> {
    TOOL_LOOP_COST_TRACKING_CONTEXT
        .try_with(Clone::clone)
        .ok()
        .flatten()
        .map(|ctx| {
            ctx.tracker
                .check_budget(0.0)
                .unwrap_or(BudgetCheck::Allowed)
        })
}
