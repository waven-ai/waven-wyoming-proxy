# Waven Wyoming Proxy

A standalone [Wyoming protocol](https://github.com/rhasspy/wyoming) server that
fronts Waven's hosted STT + TTS API. Home Assistant connects to it through the
built-in **Wyoming** integration — no custom integration code runs inside HA.

This is the privacy-minded / clean-process-boundary path (the custom
integration in `custom_components/waven` (the [home-assistant-waven](https://github.com/waven-ai/home-assistant-waven/tree/main/custom_components/waven) repo) is the five-click HACS path most
people want). It also works for any Wyoming client — Rhasspy, homemade
satellites — not just Home Assistant.

## What stays local

Wake word, voice activity detection, and intent matching never reach this
process. It only ever sees the **post-wake utterance** (which it transcribes)
and the **response text** (which it synthesises). One endpoint advertises both
an ASR program and a TTS program.

## Run it

### As a Home Assistant add-on

1. **Settings → Add-ons → Add-on Store → ⋮ → Repositories**, paste
   `https://github.com/waven-ai/waven-wyoming-proxy`, and **Add**. That repo is
   a [Home Assistant add-on repository](https://developers.home-assistant.io/docs/add-ons/repository)
   (`repository.yaml` at the root, the add-on in `waven-wyoming-proxy/`), so
   **Waven Wyoming Proxy** appears in the store as soon as it is added.
2. Install it. Supervisor pulls the prebuilt image (`aarch64` and `amd64`),
   so there is no long local build — an emulated build on a Raspberry Pi is
   painfully slow.
3. In the add-on **Configuration** tab, paste your `wvn_...` API key and pick a
   default voice. Start the add-on.
4. **Settings → Devices & Services → Add Integration → Wyoming Protocol**,
   host = the add-on, port = `10300`. HA discovers both the STT and TTS
   services; select them in your Assist pipeline.

**Manual install (fallback).** If you would rather not add a repository, or you
are running your own fork, copy the `waven-wyoming-proxy/` directory into your
Home Assistant `/addons` share (that share is itself an add-on repository — one
directory per add-on), then **Reload** the add-on store: it shows up under
*Local add-ons*. Steps 3 and 4 are unchanged.

### As a plain Docker container

```bash
docker run -d --name waven-wyoming -p 10300:10300 \
  -e TZ=Europe/Paris \
  -e WAVEN_API_KEY=wvn_your_key_here \
  -e WAVEN_DEFAULT_VOICE=kokoro:af_heart \
  -e WAVEN_DAILY_CAP_MINUTES=30 \
  -v waven-wyoming-data:/data \
  ghcr.io/waven-ai/waven-wyoming-proxy:0.1.1
```

Then add it in HA via **Wyoming Protocol** as above (host = the Docker host IP).

Two notes for the plain-Docker path that the add-on handles for you:

- **`TZ`.** The daily cap resets at the container's *local* midnight. The
  Supervisor injects the household's timezone into an add-on; a bare container
  defaults to **UTC**, so without `-e TZ=...` the reset lands at a surprising
  hour for anyone not on UTC.
- **`/data`.** The cap counter is persisted to `/data/waven_cap.json`. Without
  a volume, every restart re-arms a cap the household may already have spent.

The image runs the proxy as an unprivileged `waven` user (it starts as root
only long enough to take ownership of the runtime-mounted `/data`, then
`exec`s via `gosu`).

## Configuration

See [`config.yaml.example`](https://github.com/waven-ai/waven-wyoming-proxy/blob/main/waven-wyoming-proxy/config.yaml.example) for every option and its
`WAVEN_*` environment-variable equivalent. The essentials:

| Option | Default | Meaning |
|---|---|---|
| `api_key` | — | Your Waven API key (`wvn_…`). Required. |
| `host` | `https://api.waven.ai` | Region edge. |
| `stt_model` | `auto` | Batch STT model: `auto`, `parakeet` (English), `voxtral` (multilingual). |
| `default_voice` | `kokoro:af_heart` | `kokoro:<id>` for a stock voice or `gallery:<voice_id>` for a clone. |
| `daily_cap_minutes` | `30` | Hard per-day minute cap (0 = unlimited). When hit, voice goes quiet: Home Assistant receives an empty clip and stays silent for each spoken response, and speech-to-text returns an empty transcript. Automations keep running. Warns in the log at 80% and 100%, and the counter is persisted to `/data/waven_cap.json` so a restart doesn't re-arm a cap you already spent. |
| `retain_audio` | `true` | `false` sends a retention opt-out header on every request, which the backend enforces: each synthesized clip is deleted from Waven's servers as soon as the proxy has downloaded it, and a clip that is never downloaded is removed by the hourly server-side cleanup (worst case a little over an hour). Because the clip is gone after that first download, a response whose download is interrupted is lost rather than kept. `true` keeps clips in Waven's standard 72-hour output cache. Speech-to-text audio is never stored either way. See [Retention](#retention) below. |

**Utterance ceiling.** One buffered utterance is capped at
`WAVEN_MAX_UTTERANCE_SECONDS` (default 600 s ≈ 19 MB at 16 kHz/16-bit/mono).
Nothing in the Wyoming protocol guarantees an `AudioStop` — a satellite that
dies mid-stream, or a stuck microphone with VAD disabled, just keeps sending
audio — so past the ceiling the proxy transcribes what it has, answers the
client, and ignores the rest of the stream rather than growing until the OS
kills the add-on.

**Audio format.** The proxy always speaks **WAV**: it asks `/api/v1/generate`
for WAV and decodes the response into the raw PCM frames the Wyoming protocol
streams, so no other container would parse. There is no format option; a
leftover `tts_format` / `WAVEN_TTS_FORMAT` is accepted and ignored, with a
warning in the log.

### Retention

With `retain_audio: false` the proxy stamps `X-Waven-Retain-Audio: false` on
all three of its calls (batch STT, `POST /generate`, the audio `GET`). The
backend enforces it on `POST /api/v1/generate`, the only TTS endpoint this
proxy calls:

- The synthesized clip is deleted from Waven's servers right after the proxy's
  single, complete `GET` of it. A ranged/partial read would not delete a clip —
  only a complete download does — and this proxy always does a complete one. A
  download that dies mid-transfer still deletes the clip, so that utterance is
  lost (the proxy does not retry the fetch; Home Assistant receives an empty
  clip and stays silent for that response).
- A clip that is never downloaded is removed by the hourly server-side cleanup
  sweep: `NO_RETAIN_AUDIO_TTL_MINUTES` (10) + the `AUDIO_CLEANUP_MIN_INTERVAL_SECONDS`
  (3600) minimum gap between sweeps + one `CLEANUP_INTERVAL_SECONDS` (300) tick
  ≈ 75 minutes worst case at the defaults; a sweep that stops running entirely
  is alerted on at 2 h (`CleanupWedged`).
- With `retain_audio: true` (the default) clips follow Waven's standard
  `AUDIO_TTL_HOURS` (72 h) output cache.
- The utterance audio this proxy sends for STT (`POST /api/v1/stt/transcribe`)
  is never stored server-side either way — it is held only for the length of
  the request and never written to the output cache. (Waven's *async* STT job
  endpoint, which this proxy never calls, does hold uploads in Redis for 1 h
  and does not yet honour the header.)
- Gallery-voice reference audio is user-managed persistent storage and is not
  affected — delete the voice in the Waven dashboard to remove it.

## Notes vs. the custom integration

The Wyoming protocol's STT path delivers a **single final transcript** to the
client (no streaming partials), so this proxy uses Waven's **batch**
`/api/v1/stt/transcribe` endpoint rather than the streaming WebSocket. If you
want streaming partials and the richest voice-routing options, use the custom
integration instead. The streaming-partial latency advantage is most visible in
the integration; the proxy trades a little latency for a clean process boundary.

### Shared code with the integration (follow-up)

`proxy.py` is a deliberate fork of a few things the custom integration also
owns: the voice-value routing (`custom_components/waven/routing.py`), the daily
cap (`quota.py`), and the curated Kokoro voice list plus the source/retention
header constants (`const.py`). The duplication is a packaging constraint — the
add-on image builds from this directory alone and must not import
`custom_components` — but it is a real drift hazard, and the forks *had* already
drifted (case-sensitive voice routing, a cap with no persistence or
notifications, five missing voices).

`tests/test_proxy_parity.py` now pins the two implementations together: it
imports the integration's modules and asserts the same inputs produce the same
routing tuples and the same voice ids. That catches drift but does not remove
it. **The follow-up is a build step that vendors the shared pure modules into
this directory at image-build time** (they are already HA-free by design), so
there is one implementation instead of two. Out of scope here; until it lands,
any change to routing, the cap, or the voice list must be made in both places
and the parity test kept green.

> **Both** the batch STT and TTS endpoints require the Waven account to have
> accepted the current Terms of Service and Privacy Policy (HTTP 428 otherwise).
> Accept them once in the dashboard before using the add-on; failures are logged
> by the proxy.
