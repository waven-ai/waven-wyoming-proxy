# Changelog — Waven Wyoming Proxy

## 0.2.0 — required update

**Install this release if you use a cloned voice.** The Waven server retired
OmniVoice, the engine earlier versions used for `gallery:<voice_id>` voices,
and now refuses it with HTTP 400 `model_retired`. On 0.1.1, a cloned
`default_voice` (or a `gallery:` voice picked in Home Assistant) produces
silence; stock `kokoro:` voices and speech-to-text are unaffected.

- Cloned voices now synthesise with **Chatterbox** (`model=chatterbox`). No
  configuration change is needed.
- A cloned voice now sends Home Assistant's requested language when Chatterbox
  supports it (23 languages; Norwegian `nb`/`nn` map to `no`), otherwise
  `Auto`. Stock voices keep sending `Auto`.
- A `model_retired` refusal is logged as its own error telling you to update
  the add-on.

## 0.1.1

- First public release (2026-09-01).
