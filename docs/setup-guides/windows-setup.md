# Windows Setup Guide

This guide covers building and installing ZeroClaw on Windows.

## Quick Start

### Option A: One-click setup script

From the repository root:

```cmd
setup.bat
```

The script auto-detects your environment and walks you through installation.
You can also pass flags to skip the interactive menu:

| Flag | Description |
|------|-------------|
| `--prebuilt` | Download pre-compiled binary (fastest) |
| `--minimal` | Build with default features only |
| `--standard` | Build with Matrix + Lark/Feishu + Postgres |
| `--full` | Build with all features |

### Option B: Scoop (package manager)

```powershell
scoop bucket add zeroclaw https://github.com/zeroclaw-labs/scoop-zeroclaw
scoop install zeroclaw
```

### Option C: Manual build

```cmd
rustup target add x86_64-pc-windows-msvc
cargo build --release --locked --features channel-matrix,channel-lark --target x86_64-pc-windows-msvc
copy target\x86_64-pc-windows-msvc\release\zeroclaw.exe %USERPROFILE%\.zeroclaw\bin\
```

## Prerequisites

| Requirement | Required? | Notes |
|-------------|-----------|-------|
| Git | Yes | [git-scm.com/download/win](https://git-scm.com/download/win) |
| Rust 1.87+ | Yes | Auto-installed by `setup.bat` if missing |
| Visual Studio Build Tools | Yes (source builds) | C++ workload required for MSVC linker |

### Installing Visual Studio Build Tools

If you don't have Visual Studio installed, install the Build Tools:

1. Download from [visualstudio.microsoft.com/visual-cpp-build-tools](https://visualstudio.microsoft.com/visual-cpp-build-tools/)
2. Select the **"Desktop development with C++"** workload
3. Install and restart your terminal

Alternatively, if you have Visual Studio 2019+ installed with the C++ workload, you're already set.

## Feature Flags

ZeroClaw uses Cargo feature flags to control which integrations are compiled in:

| Feature | Description | Default? |
|---------|-------------|----------|
| `agent-runtime` | Full agent loop, channels, tools, security | Yes |
| `browser-native` | Headless browser | No |
| `rag-pdf` | PDF extraction for RAG | No |
| `plugins-wasm` | WASM plugin system | No |

## Post-Installation

1. **Restart your terminal** for PATH changes to take effect
2. **Initialize ZeroClaw:**
   ```cmd
   zeroclaw init
   ```
3. **Configure your API key** in `%USERPROFILE%\.zeroclaw\config.toml`

## Troubleshooting

### Build fails with linker errors

Install Visual Studio Build Tools with the C++ workload. The MSVC linker is required.

### `cargo build` runs out of memory

Source builds need at least 2 GB free RAM. Use `setup.bat --prebuilt` to download a pre-compiled binary instead.

### Feishu/Lark not available

Feishu and Lark are the same platform. Build with the `channel-lark` feature:

```cmd
cargo build --release --locked --features channel-lark --target x86_64-pc-windows-msvc
```
