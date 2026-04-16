//! Channel implementations and orchestration for messaging platform integrations.

pub mod orchestrator;
pub mod util;

pub mod cli;
pub mod link_enricher;
pub mod transcription;
pub mod tts;

#[cfg(feature = "channel-telegram")]
pub mod telegram;
#[cfg(feature = "channel-webhook")]
pub mod webhook;
