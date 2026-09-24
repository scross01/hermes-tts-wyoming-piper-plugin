# Changelog

## [0.4.0] - Unreleased

- Recover automatically from failed Piper connections instead of requiring a plugin restart.
- Bound and cancel long-running synthesis to prevent runaway resource use.
- Fix streaming audio output, incremental delivery, and ffmpeg process cleanup.
- Report empty audio responses as errors instead of returning silent or invalid files.
- Preserve each response’s audio format and support non-16-bit PCM conversion.
- Publish the plugin configuration with Hermes’ supported `config_schema` field.
- Add local-versus-remote Piper benchmarking and document the results.
