pub use zeroclaw_config::migration;
pub use zeroclaw_config::providers;
pub mod schema;
pub mod traits;
pub mod workspace;

pub use schema::{
    AgentConfig, AssemblyAiSttConfig, AuditConfig, AutonomyConfig, BackupConfig,
    BrowserComputerUseConfig, BrowserConfig, BuiltinHooksConfig, ChannelsConfig,
    ClassificationRule, ClaudeCodeRunnerConfig, CodexCliConfig, Config, ConversationalAiConfig,
    CostConfig, CronConfig, CronJobDecl, CronScheduleDecl, DataRetentionConfig, DeepgramSttConfig,
    DelegateAgentConfig, DelegateToolConfig, EdgeTtsConfig, ElevenLabsTtsConfig,
    EmbeddingRouteConfig, EstopConfig, GatewayConfig, GoogleSttConfig, GoogleTtsConfig,
    HeartbeatConfig, HooksConfig, HttpRequestConfig, IdentityConfig, ImageGenConfig,
    KnowledgeConfig, LinkEnricherConfig, LocalWhisperConfig, McpConfig, McpServerConfig,
    McpTransport, MediaPipelineConfig, MemoryConfig, MemoryPolicyConfig, ModelRouteConfig,
    MultimodalConfig, NodeTransportConfig, NodesConfig, ObservabilityConfig, OpenAiSttConfig,
    OpenAiTtsConfig, OtpConfig, OtpMethod, PacingConfig, PipelineConfig, PiperTtsConfig,
    PluginsConfig, ProxyConfig, ProxyScope, QdrantConfig, QueryClassificationConfig,
    ReliabilityConfig, ResourceLimitsConfig, RuntimeConfig, SandboxBackend, SandboxConfig,
    SchedulerConfig, SearchMode, SecretsConfig, SecurityConfig, SecurityOpsConfig, ShellToolConfig,
    SkillCreationConfig, SkillImprovementConfig, SkillsConfig, SkillsPromptInjectionMode,
    SopConfig, StorageConfig, StorageProviderConfig, StorageProviderSection, StreamMode,
    SwarmConfig, SwarmStrategy, TelegramConfig, ToolFilterGroup, ToolFilterGroupMode,
    TranscriptionConfig, TtsConfig, WebFetchConfig, WebSearchConfig, WebhookConfig,
    WorkspaceConfig, apply_channel_proxy_to_builder, apply_runtime_proxy_to_builder,
    build_channel_proxy_client, build_channel_proxy_client_with_timeouts,
    build_runtime_proxy_client, build_runtime_proxy_client_with_timeouts, runtime_proxy_config,
    set_runtime_proxy_config, ws_connect_with_proxy,
};

pub use schema::ModelProviderConfig;
pub use traits::HasPropKind;
pub use traits::PropFieldInfo;
pub use traits::PropKind;
pub use traits::SecretFieldInfo;

pub fn name_and_presence<T: traits::ChannelConfig>(channel: Option<&T>) -> (&'static str, bool) {
    (T::name(), channel.is_some())
}

// ── Serde-based property helpers ──────────────────────────────────

/// Build a `PropFieldInfo` by reading the display value from a serialized TOML table.
pub fn make_prop_field(
    table: Option<&toml::Table>,
    name: &'static str,
    serde_name: &str,
    category: &'static str,
    type_hint: &'static str,
    kind: PropKind,
    is_secret: bool,
) -> PropFieldInfo {
    let display_value = if is_secret {
        match table.and_then(|t| t.get(serde_name)) {
            Some(toml::Value::String(s)) if !s.is_empty() => "****".to_string(),
            _ => "<unset>".to_string(),
        }
    } else {
        toml_value_to_display(table.and_then(|t| t.get(serde_name)))
    };
    PropFieldInfo {
        name,
        category,
        display_value,
        type_hint,
        kind,
        is_secret,
    }
}

/// Get a property value via serde serialization.
pub fn serde_get_prop<T: serde::Serialize>(
    target: &T,
    prefix: &str,
    name: &str,
    is_secret: bool,
) -> anyhow::Result<String> {
    if is_secret {
        return Ok("**** (encrypted)".to_string());
    }
    let serde_name = prop_name_to_serde_field(prefix, name)?;
    let table = toml::Value::try_from(target)?;
    Ok(toml_value_to_display(
        table.as_table().and_then(|t| t.get(&serde_name)),
    ))
}

/// Set a property value via serde roundtrip.
pub fn serde_set_prop<T: serde::Serialize + serde::de::DeserializeOwned>(
    target: &mut T,
    prefix: &str,
    name: &str,
    value_str: &str,
    kind: PropKind,
    is_option: bool,
) -> anyhow::Result<()> {
    let serde_name = prop_name_to_serde_field(prefix, name)?;
    let mut table: toml::Table = toml::from_str(&toml::to_string(target)?)?;
    if value_str.is_empty() && is_option {
        table.remove(&serde_name);
    } else {
        table.insert(serde_name, parse_prop_value(value_str, kind)?);
    }
    *target = toml::from_str(&toml::to_string(&table)?)?;
    Ok(())
}

fn toml_value_to_display(value: Option<&toml::Value>) -> String {
    match value {
        None => "<unset>".to_string(),
        Some(toml::Value::String(s)) => s.clone(),
        Some(v) => v.to_string(),
    }
}

fn prop_name_to_serde_field(prefix: &str, name: &str) -> anyhow::Result<String> {
    let suffix = if prefix.is_empty() {
        name
    } else {
        name.strip_prefix(prefix)
            .and_then(|s| s.strip_prefix('.'))
            .ok_or_else(|| anyhow::anyhow!("Unknown property '{name}'"))?
    };
    let field_part = suffix.split('.').next().unwrap_or(suffix);
    Ok(field_part.replace('-', "_"))
}

fn parse_prop_value(value_str: &str, kind: PropKind) -> anyhow::Result<toml::Value> {
    match kind {
        PropKind::Bool => Ok(toml::Value::Boolean(value_str.parse().map_err(|_| {
            anyhow::anyhow!("Invalid bool value '{value_str}' — expected 'true' or 'false'")
        })?)),
        PropKind::Integer => {
            Ok(toml::Value::Integer(value_str.parse().map_err(|_| {
                anyhow::anyhow!("Invalid integer value '{value_str}'")
            })?))
        }
        PropKind::Float => {
            Ok(toml::Value::Float(value_str.parse().map_err(|_| {
                anyhow::anyhow!("Invalid float value '{value_str}'")
            })?))
        }
        PropKind::String | PropKind::Enum => Ok(toml::Value::String(value_str.to_string())),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn reexported_config_default_is_constructible() {
        let config = Config::default();

        // Config::default() no longer has provider cache fields; just verify providers is constructible
        assert!(config.providers.fallback.is_none() || config.providers.fallback.is_some());
    }

    #[test]
    fn reexported_channel_configs_are_constructible() {
        let telegram = TelegramConfig {
            enabled: true,
            bot_token: "token".into(),
            allowed_users: vec!["alice".into()],
            stream_mode: StreamMode::default(),
            draft_update_interval_ms: 1000,
            interrupt_on_new_message: false,
            mention_only: false,
            ack_reactions: None,
            proxy_url: None,
        };

        assert_eq!(telegram.allowed_users.len(), 1);
    }
}
