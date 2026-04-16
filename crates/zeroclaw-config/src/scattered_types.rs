//! Config types that were originally defined in their home modules (agent, channels, tools, trust)
//! but are needed by the config schema. Moved here to break circular dependencies.

use crate::traits::{HasPropKind, PropKind};
use serde::{Deserialize, Serialize};
use zeroclaw_macros::Configurable;

// ── Agent config types ──────────────────────────────────────────

/// How deeply the model should reason for a given message.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize, Default)]
#[serde(rename_all = "lowercase")]
pub enum ThinkingLevel {
    Off,
    Minimal,
    Low,
    #[default]
    Medium,
    High,
    Max,
}

impl HasPropKind for ThinkingLevel {
    const PROP_KIND: PropKind = PropKind::Enum;
}

impl ThinkingLevel {
    pub fn from_str_insensitive(s: &str) -> Option<Self> {
        match s.to_lowercase().as_str() {
            "off" | "none" => Some(Self::Off),
            "minimal" | "min" => Some(Self::Minimal),
            "low" => Some(Self::Low),
            "medium" | "med" | "default" => Some(Self::Medium),
            "high" => Some(Self::High),
            "max" | "maximum" => Some(Self::Max),
            _ => None,
        }
    }
}

/// Configuration for thinking/reasoning level control.
#[derive(Debug, Clone, Serialize, Deserialize, Configurable)]
#[prefix = "agent.thinking"]
pub struct ThinkingConfig {
    #[serde(default)]
    pub default_level: ThinkingLevel,
}

impl Default for ThinkingConfig {
    fn default() -> Self {
        Self {
            default_level: ThinkingLevel::Medium,
        }
    }
}

fn default_max_tokens() -> usize {
    8192
}
fn default_keep_recent() -> usize {
    4
}
fn default_collapse() -> bool {
    true
}

#[derive(Debug, Clone, Serialize, Deserialize, Configurable)]
#[prefix = "agent.history-pruning"]
pub struct HistoryPrunerConfig {
    #[serde(default)]
    pub enabled: bool,
    #[serde(default = "default_max_tokens")]
    pub max_tokens: usize,
    #[serde(default = "default_keep_recent")]
    pub keep_recent: usize,
    #[serde(default = "default_collapse")]
    pub collapse_tool_results: bool,
}

impl Default for HistoryPrunerConfig {
    fn default() -> Self {
        Self {
            enabled: false,
            max_tokens: 8192,
            keep_recent: 4,
            collapse_tool_results: true,
        }
    }
}

fn default_cost_optimized_hint() -> String {
    "cost-optimized".to_string()
}

#[derive(Debug, Clone, Serialize, Deserialize, Configurable)]
#[prefix = "agent.auto-classify"]
pub struct AutoClassifyConfig {
    #[serde(default)]
    pub simple_hint: Option<String>,
    #[serde(default)]
    pub standard_hint: Option<String>,
    #[serde(default)]
    pub complex_hint: Option<String>,
    #[serde(default = "default_cost_optimized_hint")]
    pub cost_optimized_hint: String,
}

impl Default for AutoClassifyConfig {
    fn default() -> Self {
        Self {
            simple_hint: None,
            standard_hint: None,
            complex_hint: None,
            cost_optimized_hint: default_cost_optimized_hint(),
        }
    }
}

fn default_min_quality_score() -> f64 {
    0.5
}
fn default_eval_max_retries() -> u32 {
    1
}

#[derive(Debug, Clone, Serialize, Deserialize, Configurable)]
#[prefix = "agent.eval"]
pub struct EvalConfig {
    #[serde(default)]
    pub enabled: bool,
    #[serde(default = "default_min_quality_score")]
    pub min_quality_score: f64,
    #[serde(default = "default_eval_max_retries")]
    pub max_retries: u32,
}

impl Default for EvalConfig {
    fn default() -> Self {
        Self {
            enabled: false,
            min_quality_score: default_min_quality_score(),
            max_retries: default_eval_max_retries(),
        }
    }
}

fn default_cc_enabled() -> bool {
    true
}
fn default_threshold_ratio() -> f64 {
    0.50
}
fn default_protect_first_n() -> usize {
    3
}
fn default_protect_last_n() -> usize {
    4
}
fn default_cc_max_passes() -> u32 {
    3
}
fn default_summary_max_chars() -> usize {
    4000
}
fn default_source_max_chars() -> usize {
    50_000
}
fn default_cc_timeout_secs() -> u64 {
    60
}
fn default_identifier_policy() -> String {
    "strict".to_string()
}
fn default_tool_result_retrim_chars() -> usize {
    2_000
}

#[derive(Debug, Clone, Serialize, Deserialize, Configurable)]
#[prefix = "agent.context-compression"]
pub struct ContextCompressionConfig {
    #[serde(default = "default_cc_enabled")]
    pub enabled: bool,
    #[serde(default = "default_threshold_ratio")]
    pub threshold_ratio: f64,
    #[serde(default = "default_protect_first_n")]
    pub protect_first_n: usize,
    #[serde(default = "default_protect_last_n")]
    pub protect_last_n: usize,
    #[serde(default = "default_cc_max_passes")]
    pub max_passes: u32,
    #[serde(default = "default_summary_max_chars")]
    pub summary_max_chars: usize,
    #[serde(default = "default_source_max_chars")]
    pub source_max_chars: usize,
    #[serde(default = "default_cc_timeout_secs")]
    pub timeout_secs: u64,
    #[serde(default)]
    pub summary_model: Option<String>,
    #[serde(default = "default_identifier_policy")]
    pub identifier_policy: String,
    #[serde(default = "default_tool_result_retrim_chars")]
    pub tool_result_retrim_chars: usize,
    #[serde(default)]
    pub tool_result_trim_exempt: Vec<String>,
}

impl Default for ContextCompressionConfig {
    fn default() -> Self {
        Self {
            enabled: default_cc_enabled(),
            threshold_ratio: default_threshold_ratio(),
            protect_first_n: default_protect_first_n(),
            protect_last_n: default_protect_last_n(),
            max_passes: default_cc_max_passes(),
            summary_max_chars: default_summary_max_chars(),
            source_max_chars: default_source_max_chars(),
            timeout_secs: default_cc_timeout_secs(),
            summary_model: None,
            identifier_policy: default_identifier_policy(),
            tool_result_retrim_chars: default_tool_result_retrim_chars(),
            tool_result_trim_exempt: Vec::new(),
        }
    }
}

// ── Tools config types ──────────────────────────────────────────

fn default_browser_cli() -> String {
    "claude".into()
}
fn default_browser_task_timeout() -> u64 {
    120
}

#[derive(Debug, Clone, Serialize, Deserialize, Configurable)]
#[prefix = "browser-delegate"]
pub struct BrowserDelegateConfig {
    #[serde(default)]
    pub enabled: bool,
    #[serde(default = "default_browser_cli")]
    pub cli_binary: String,
    #[serde(default)]
    pub chrome_profile_dir: String,
    #[serde(default)]
    pub allowed_domains: Vec<String>,
    #[serde(default)]
    pub blocked_domains: Vec<String>,
    #[serde(default = "default_browser_task_timeout")]
    pub task_timeout_secs: u64,
}

impl Default for BrowserDelegateConfig {
    fn default() -> Self {
        Self {
            enabled: false,
            cli_binary: default_browser_cli(),
            chrome_profile_dir: String::new(),
            allowed_domains: Vec::new(),
            blocked_domains: Vec::new(),
            task_timeout_secs: default_browser_task_timeout(),
        }
    }
}

// ── Trust config types ──────────────────────────────────────────

fn default_initial_score() -> f64 {
    0.8
}
fn default_decay_half_life() -> f64 {
    30.0
}
fn default_regression_threshold() -> f64 {
    0.5
}
fn default_correction_penalty() -> f64 {
    0.05
}
fn default_success_boost() -> f64 {
    0.01
}

#[derive(Debug, Clone, Serialize, Deserialize, Configurable)]
#[prefix = "trust"]
pub struct TrustConfig {
    #[serde(default = "default_initial_score")]
    pub initial_score: f64,
    #[serde(default = "default_decay_half_life")]
    pub decay_half_life_days: f64,
    #[serde(default = "default_regression_threshold")]
    pub regression_threshold: f64,
    #[serde(default = "default_correction_penalty")]
    pub correction_penalty: f64,
    #[serde(default = "default_success_boost")]
    pub success_boost: f64,
}

impl Default for TrustConfig {
    fn default() -> Self {
        Self {
            initial_score: default_initial_score(),
            decay_half_life_days: default_decay_half_life(),
            regression_threshold: default_regression_threshold(),
            correction_penalty: default_correction_penalty(),
            success_boost: default_success_boost(),
        }
    }
}
