# Security

Cutdown is a single-operator CLI for trusted recordings and configurations. On
a server, run it through SSH or the provided Compose service using a dedicated
working directory and an unprivileged account. The service publishes no ports.
Keep each job's output folder separate from its source files. Render replaces
its three generated audio folders when it runs.

## Credentials and data

- Store API keys and tokens in a private `.env` or in the process environment.
  Cutdown passes the Hugging Face token to WhisperX in the child environment.
- WhisperX runs locally and downloads dependencies and model weights when needed.
  Model-assisted stages send transcript text and supplied episode context to
  the configured provider. Review the provider's terms and account settings
  before processing private interviews. The default OpenRouter configuration
  requests `data_collection = "deny"`. See
  [OpenRouter's routing documentation](https://openrouter.ai/docs/guides/routing/provider-selection).
- Keep `runs/`, transcripts, notes, and manifests private. Raw prompts and
  replies can include the full transcript. Treat generated reports and CSVs
  as untrusted content when opening them in browsers or spreadsheets.
- Only use TOML settings, presets, episode context files, and custom CLI
  providers you trust. They can select subprocess commands or change the
  provider destination and credentials used for a job.

## Optional transcription dependency

The core package can ingest an existing WhisperX JSON and render clips without
installing WhisperX. Prefer that workflow on a shared server. The November 2026
dependency audit flagged versions of PyTorch and Transformers in the
WhisperX 3.8.6 dependency set. WhisperX still constrains PyTorch to the 2.8
series in the upstream prerelease. If using optional transcription, run it on
trusted media with trusted model weights in a dedicated environment. See
[upstream releases](https://github.com/m-bain/whisperX/releases) and
[upstream dependency declarations](https://github.com/m-bain/whisperX/blob/main/pyproject.toml).
