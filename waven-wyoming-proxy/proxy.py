"""Waven Wyoming proxy — a standalone Wyoming server that fronts the Waven
hosted STT + TTS API.

This is the spec's Phase 3 deliverable: a clean process boundary for users who
won't run our integration code inside Home Assistant (privacy-minded, HA Core in
a venv) and a reusable Wyoming endpoint for non-HA clients (Rhasspy, homemade
satellites). HA connects to it via the built-in **Wyoming** integration
("Add a Wyoming service" → host = this add-on, port = 10300); one endpoint
advertises both an ASR program and a TTS program.

Wake word, VAD and intent matching stay in Home Assistant. This process only
sees the post-wake utterance (transcribed) and the response text (synthesised).

It is intentionally self-contained — it does NOT import the custom_components
package, so the add-on builds from this directory alone.
"""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import logging
import os
import wave
from functools import partial

import aiohttp
from wyoming.asr import Transcribe, Transcript
from wyoming.audio import AudioChunk, AudioStart, AudioStop
from wyoming.event import Event
from wyoming.info import (
    AsrModel,
    AsrProgram,
    Attribution,
    Describe,
    Info,
    TtsProgram,
    TtsVoice,
)
from wyoming.server import AsyncEventHandler, AsyncServer
from wyoming.tts import Synthesize

_LOGGER = logging.getLogger("waven_wyoming")

VERSION = "0.1.1"
_ATTRIBUTION = Attribution(name="Waven", url="https://waven.ai")

# Curated Kokoro stock voices advertised to HA. This list MIRRORS
# `custom_components/waven/const.py::KOKORO_VOICES` — same ids, same order, same
# labels — so a household sees the same picker whichever path it runs. The two
# copies exist because the add-on image is built from this directory alone and
# must not import the integration package; `tests/test_proxy_parity.py` locks
# them together, and vendoring the shared modules at build time is the standing
# follow-up (see README "Shared code with the integration").
#
# The backend accepts any `<lang><gender>_<name>` id and falls back to af_heart
# for unknowns.
_KOKORO_VOICES: list[tuple[str, str, str]] = [
    # (voice id sent to /generate, friendly description, language)
    ("af_heart", "Heart — US English, female", "en"),
    ("af_bella", "Bella — US English, female", "en"),
    ("af_nicole", "Nicole — US English, female", "en"),
    ("af_sarah", "Sarah — US English, female", "en"),
    ("am_michael", "Michael — US English, male", "en"),
    ("am_adam", "Adam — US English, male", "en"),
    ("am_fenrir", "Fenrir — US English, male", "en"),
    ("bf_emma", "Emma — UK English, female", "en"),
    ("bf_isabella", "Isabella — UK English, female", "en"),
    ("bm_george", "George — UK English, male", "en"),
    ("bm_lewis", "Lewis — UK English, male", "en"),
    ("ef_dora", "Dora — Spanish, female", "es"),
    ("ff_siwis", "Siwis — French, female", "fr"),
]
# Rough speaking rate (chars/sec) used only to pre-flight the daily-cap gate;
# the real duration is charged after synthesis. Mirrors tts._CHARS_PER_SECOND
# (15 ran ~40% hot against measured Kokoro/OmniVoice output at speed 1.0, so
# every announcement reserved half again more budget than it spent).
_CHARS_PER_SECOND = 21.0

# Hard ceiling on one buffered utterance, in seconds of audio. Wyoming has no
# obligation to send AudioStop: a satellite that dies mid-stream, or a stuck
# microphone with VAD disabled, just keeps sending AudioChunk. Every chunk was
# appended to an unbounded bytearray, so the process grew until the OS killed
# it — on an HA Green or a Pi 4, that is the whole add-on. 600 s is ~20x the
# longest plausible Assist utterance and, at 16 kHz/16-bit/mono, ~19 MB.
MAX_UTTERANCE_SECONDS = float(os.environ.get("WAVEN_MAX_UTTERANCE_SECONDS", "600"))

_STT_LANGUAGES = ["en", "fr", "de", "es", "it", "pt"]
_TTS_LANGUAGES = ["en", "fr", "de", "es", "it", "pt", "nl", "ja", "zh", "hi"]

# Mirrors const.DEFAULT_VOICE_VALUE / DEFAULT_KOKORO_VOICE.
DEFAULT_VOICE_VALUE = "kokoro:af_heart"
DEFAULT_KOKORO_VOICE = "af_heart"
# Mirrors const.DEFAULT_DAILY_CAP_MINUTES + CAP_NOTIFY_PERCENTS.
DEFAULT_DAILY_CAP_MINUTES = 30
CAP_NOTIFY_PERCENTS = (80, 100)

# The proxy always asks /generate for WAV: `wav_to_pcm` below unpacks the
# response with the stdlib `wave` module to get the raw PCM frames Wyoming
# streams, so an mp3/ogg response would simply fail to parse. There is
# deliberately no format option — see `Config` and README.
TTS_FORMAT = "wav"

SOURCE_HEADER = "X-Waven-Source"
SOURCE_VALUE = "home-assistant-wyoming"
# Per-request retention opt-out. Enforced server-side: for a flagged
# POST /api/v1/generate the backend deletes the synthesized clip as soon as
# this proxy has downloaded it in full (a ranged read does not burn a clip —
# this proxy never sends one). A clip that is never downloaded falls to the
# cleanup sweep, which drops it past `NO_RETAIN_AUDIO_TTL_MINUTES` (default 10)
# rather than the 72-hour `AUDIO_TTL_HOURS` unflagged clips get. Worst case at
# the defaults: 10 min TTL + `AUDIO_CLEANUP_MIN_INTERVAL_SECONDS` (3600, the
# minimum gap between sweeps) + one `CLEANUP_INTERVAL_SECONDS` (300) tick that
# notices the gap has elapsed ≈ 75 min from render; a sweep that stops running
# altogether alerts at 2 h (CleanupWedged). /generate is the only TTS endpoint
# this proxy calls, and the only one wired. The STT endpoints this proxy uses
# (POST /api/v1/stt/transcribe) never persist utterance audio server-side
# either way — the async job endpoint POST /api/v1/stt/transcribe/jobs, which
# this proxy does not call, does keep the upload in Redis for 1 h and is in
# the same backend follow-up bucket as /generate-long and async TTS jobs.
# Gallery-voice reference audio is user-managed persistent storage, unaffected.
RETENTION_HEADER = "X-Waven-Retain-Audio"


# --- config ------------------------------------------------------------------
class Config:
    """Resolved from /data/options.json (HA add-on) then overlaid by env vars
    (plain ``docker run``)."""

    def __init__(self, raw: dict) -> None:
        self.api_key: str = str(raw.get("api_key", "")).strip()
        self.host: str = str(raw.get("host", "https://api.waven.ai")).rstrip("/")
        # Batch STT model: auto | parakeet | voxtral. "auto" lets the backend's
        # stt_batch_chain pick per language; Parakeet is English-only.
        self.stt_model: str = str(raw.get("stt_model", "auto"))
        self.stt_language: str = str(raw.get("stt_language", "en"))
        self.default_voice: str = str(raw.get("default_voice", DEFAULT_VOICE_VALUE))
        self.retain_audio: bool = bool(raw.get("retain_audio", True))
        self.uri: str = str(raw.get("uri", "tcp://0.0.0.0:10300"))
        # Safety rail, mirroring the integration's DEFAULT_DAILY_CAP_MINUTES: a
        # stuck microphone or a runaway automation must not be able to drain an
        # account. An explicit 0 (including a blank value) disables it.
        self.daily_cap_minutes: int = int(
            raw.get("daily_cap_minutes", DEFAULT_DAILY_CAP_MINUTES) or 0
        )

        # There is no output-format option: the proxy always requests WAV (see
        # TTS_FORMAT). Older configs may still carry `tts_format` / set
        # WAVEN_TTS_FORMAT — accepted and ignored, but say so rather than
        # silently pretending it applied.
        legacy_format = raw.get("tts_format")
        if legacy_format is not None and str(legacy_format).strip().lower() != TTS_FORMAT:
            _LOGGER.warning(
                "tts_format=%r is not supported and is ignored: the Wyoming "
                "proxy always speaks WAV (it decodes the response to raw PCM "
                "frames). Remove the option from your configuration.",
                legacy_format,
            )

    @classmethod
    def load(cls) -> "Config":
        raw: dict = {}
        options_path = os.environ.get("WAVEN_OPTIONS_PATH", "/data/options.json")
        if os.path.exists(options_path):
            try:
                with open(options_path, encoding="utf-8") as handle:
                    raw = json.load(handle)
            except (OSError, ValueError) as err:  # pragma: no cover - defensive
                _LOGGER.warning("Could not read %s: %s", options_path, err)

        # Env overrides (WAVEN_API_KEY, WAVEN_HOST, ...).
        env_map = {
            "WAVEN_API_KEY": "api_key",
            "WAVEN_HOST": "host",
            "WAVEN_STT_MODEL": "stt_model",
            "WAVEN_STT_LANGUAGE": "stt_language",
            "WAVEN_DEFAULT_VOICE": "default_voice",
            "WAVEN_TTS_FORMAT": "tts_format",
            "WAVEN_URI": "uri",
            "WAVEN_DAILY_CAP_MINUTES": "daily_cap_minutes",
        }
        for env_key, opt_key in env_map.items():
            if env_key in os.environ:
                raw[opt_key] = os.environ[env_key]
        if "WAVEN_RETAIN_AUDIO" in os.environ:
            raw["retain_audio"] = os.environ["WAVEN_RETAIN_AUDIO"].lower() not in ("0", "false", "no")
        return cls(raw)


def parse_voice(value: str | None, default: str) -> tuple[str, str | None, str | None]:
    """``"kokoro:af_heart"`` → (model, speaker, gallery_id). A ``gallery:`` prefix
    routes to OmniVoice cloning; anything else is a Kokoro stock speaker.

    This is `routing.parse_voice_value` + `routing.selection_from_voice_value`
    from the custom integration, applied to ``value or default``, and it must
    stay bug-for-bug identical to them — a household that moves between the
    add-on and the integration has to keep the same voice. The parity is locked
    by `tests/test_proxy_parity.py`. Two behaviours the earlier `startswith`
    version got wrong:

      * the kind is **case-insensitive and whitespace-tolerant** per part, so
        ``"Gallery: abc"`` routes to OmniVoice, not to a Kokoro speaker literally
        named ``"Gallery: abc"``;
      * an unknown prefix (``"piper:x"``) falls back to the WHOLE string as a
        bare Kokoro id, matching the integration's forgiving override handling.
    """
    raw = value or default or DEFAULT_VOICE_VALUE
    if not raw:
        return "kokoro", DEFAULT_KOKORO_VOICE, None
    if ":" in raw:
        kind, _, voice_id = raw.partition(":")
        kind = kind.strip().lower()
        voice_id = voice_id.strip()
        if voice_id:
            if kind == "gallery":
                return "omnivoice", None, voice_id
            if kind == "kokoro":
                return "kokoro", voice_id, None
        # Unknown prefix (or an empty id) — treat the whole thing as a bare id.
        return "kokoro", raw.strip(), None
    return "kokoro", raw.strip(), None


# --- daily cap (process-local) ----------------------------------------------
class DailyCap:
    """Per-household daily minute cap, mirroring the integration's
    `quota.DailyCapTracker` semantics (the add-on is one household, so a single
    counter is enough). ``cap_minutes <= 0`` disables it.

    Ported from the integration so the two paths behave the same:

      * ``would_exceed`` — a preflight so a request that would blow the cap is
        declined *before* the minute is spent upstream, with the same inclusive
        boundary (landing exactly on the cap is allowed, exceeding it is not);
      * ``newly_crossed`` — the 80% / 100% thresholds, each fired at most once
        per local day (the proxy has no notification surface, so it logs);
      * ``to_dict`` / ``from_dict`` — so the counter survives a restart. Without
        it, restarting the add-on silently re-armed a cap the household had
        already spent, which defeats the point of a safety rail.
    """

    def __init__(self, cap_minutes: int, state_path: str | None = None) -> None:
        self.cap_minutes = cap_minutes
        self.state_path = state_path
        self._day = ""
        self._seconds = 0.0
        self._notified: list[int] = []

    # --- state ---------------------------------------------------------------
    @property
    def used_seconds(self) -> float:
        return self._seconds

    @property
    def used_minutes(self) -> float:
        return self._seconds / 60.0

    def _roll(self, today: str) -> None:
        if self._day != today:
            self._day = today
            self._seconds = 0.0
            self._notified = []

    # --- accounting ----------------------------------------------------------
    def allows(self, today: str) -> bool:
        if self.cap_minutes <= 0:
            return True
        self._roll(today)
        return self._seconds / 60.0 < self.cap_minutes

    def would_exceed(self, today: str, seconds: float) -> bool:
        """Would charging ``seconds`` reach or pass the cap?

        ``>=``, matching ``allows()`` and the integration's
        ``quota.DailyCapTracker.would_exceed``. Locked by
        tests/test_proxy_parity.py: a household that moves between the add-on
        and the integration must see the cap bite on the same request.
        """
        if self.cap_minutes <= 0:
            return False
        self._roll(today)
        return (self._seconds + max(0.0, seconds)) / 60.0 >= self.cap_minutes

    def add(self, today: str, seconds: float, persist: bool = True) -> None:
        """Charge ``seconds``. ``persist=False`` skips the (blocking) file
        write so an async caller can offload it — see
        ``WavenEventHandler._charge``."""
        self._roll(today)
        self._seconds += max(0.0, seconds)
        if persist:
            self.save()

    def percent_used(self) -> float:
        if self.cap_minutes <= 0:
            return 0.0
        return min(self.used_minutes / self.cap_minutes * 100.0, 100.0)

    def newly_crossed(self) -> list[int]:
        """Notify thresholds crossed since the last call, once per local day."""
        if self.cap_minutes <= 0:
            return []
        pct = self.percent_used()
        crossed = [t for t in CAP_NOTIFY_PERCENTS if pct >= t and t not in self._notified]
        self._notified.extend(crossed)
        return crossed

    # --- persistence ---------------------------------------------------------
    def to_dict(self) -> dict:
        return {"day": self._day, "seconds": self._seconds, "notified": list(self._notified)}

    def load_dict(self, data: dict | None) -> None:
        if not data:
            return
        self._day = str(data.get("day") or "")
        self._seconds = float(data.get("seconds") or 0.0)
        self._notified = [int(p) for p in (data.get("notified") or [])]

    def save(self) -> None:
        """Best-effort persist. A read-only/full /data must never take voice
        down, so failures are logged at debug and otherwise ignored."""
        if not self.state_path:
            return
        try:
            with open(self.state_path, "w", encoding="utf-8") as handle:
                json.dump(self.to_dict(), handle)
        except OSError as err:  # pragma: no cover - defensive
            _LOGGER.debug("Could not persist daily-cap state: %s", err)

    def load(self) -> None:
        if not self.state_path or not os.path.exists(self.state_path):
            return
        try:
            with open(self.state_path, encoding="utf-8") as handle:
                self.load_dict(json.load(handle))
        except (OSError, ValueError) as err:  # pragma: no cover - defensive
            _LOGGER.warning("Could not read daily-cap state: %s", err)


# --- audio helpers -----------------------------------------------------------
def pcm_to_wav(pcm: bytes, rate: int, width: int, channels: int) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav:
        wav.setnchannels(channels)
        wav.setsampwidth(width)
        wav.setframerate(rate)
        wav.writeframes(pcm)
    return buf.getvalue()


def wav_to_pcm(data: bytes) -> tuple[bytes, int, int, int]:
    with wave.open(io.BytesIO(data), "rb") as wav:
        return (
            wav.readframes(wav.getnframes()),
            wav.getframerate(),
            wav.getsampwidth(),
            wav.getnchannels(),
        )


# --- Waven client ------------------------------------------------------------
class WavenError(Exception):
    """Base for Waven API failures. Mirrors the integration's api.py hierarchy
    so the two paths classify the same HTTP statuses the same way."""


class WavenAuthError(WavenError):
    """Invalid API key, or the account is disabled (HTTP 401/403)."""


class WavenQuotaError(WavenError):
    """Server-side usage limit hit (HTTP 402/429)."""


class WavenConsentError(WavenError):
    """Account hasn't accepted the current ToS/Privacy (HTTP 428)."""


def raise_for_waven_status(status: int, body: str = "") -> None:
    """Classify a Waven HTTP status.

    `resp.raise_for_status()` collapsed 401, 428 and 429 into one
    ``ClientResponseError`` that the handler logged as a generic "STT failed" —
    so the three states a user can actually DO something about (bad key, terms
    not accepted, out of minutes) were indistinguishable in the add-on log,
    which is the only surface this headless process has.
    """
    if status in (401, 403):
        raise WavenAuthError(f"Authentication failed ({status}): {body[:200]}")
    if status == 428:
        raise WavenConsentError(
            "This Waven account hasn't accepted the current Terms of Service "
            "and Privacy Policy. Accept them in the Waven dashboard, then retry."
        )
    if status in (402, 429):
        raise WavenQuotaError(f"Usage limit reached ({status}): {body[:200]}")
    if status >= 400:
        raise WavenError(f"Request failed ({status}): {body[:200]}")


def _log_api_failure(what: str, err: BaseException) -> None:
    """One log line per failure class, worded so a user can act on it."""
    if isinstance(err, WavenAuthError):
        _LOGGER.error(
            "%s failed: Waven rejected the API key. Check `api_key` in the "
            "add-on configuration (it should begin with wvn_ and be active).",
            what,
        )
    elif isinstance(err, WavenConsentError):
        _LOGGER.error(
            "%s failed: %s (Speech-to-text is unaffected by the consent gate.)",
            what, err,
        )
    elif isinstance(err, WavenQuotaError):
        _LOGGER.error(
            "%s failed: the Waven account has reached its usage limit. This is "
            "the SERVER-side quota, not this add-on's daily cap.",
            what,
        )
    else:
        _LOGGER.error("%s failed: %s", what, err)


class WavenAPI:
    def __init__(self, session: aiohttp.ClientSession, cfg: Config) -> None:
        self._session = session
        self._cfg = cfg

    def _headers(self) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self._cfg.api_key}",
            SOURCE_HEADER: SOURCE_VALUE,
        }
        if not self._cfg.retain_audio:
            headers[RETENTION_HEADER] = "false"
        return headers

    async def transcribe(self, wav_bytes: bytes, language: str | None = None) -> str:
        form = aiohttp.FormData()
        form.add_field("file", wav_bytes, filename="utterance.wav", content_type="audio/wav")
        form.add_field("model", self._cfg.stt_model)
        form.add_field("language", language or self._cfg.stt_language)
        async with self._session.post(
            f"{self._cfg.host}/api/v1/stt/transcribe",
            headers=self._headers(),
            data=form,
            timeout=aiohttp.ClientTimeout(total=60),
        ) as resp:
            if resp.status != 200:
                raise_for_waven_status(resp.status, await resp.text())
            data = await resp.json()
        return data.get("text", "") or ""

    async def synthesize(self, text: str, voice: str | None) -> bytes:
        model, speaker, gallery_id = parse_voice(voice, self._cfg.default_voice)
        form = aiohttp.FormData()
        form.add_field("model", model)
        form.add_field("text", text)
        form.add_field("format", TTS_FORMAT)  # always WAV → wav_to_pcm needs it
        form.add_field("language", "Auto")
        if gallery_id:
            form.add_field("gallery_voice_id", gallery_id)
        elif speaker:
            form.add_field("speaker", speaker)

        async with self._session.post(
            f"{self._cfg.host}/api/v1/generate",
            headers=self._headers(),
            data=form,
            timeout=aiohttp.ClientTimeout(total=60),
        ) as resp:
            if resp.status != 200:
                raise_for_waven_status(resp.status, await resp.text())
            meta = await resp.json()

        file_path = meta.get("file")
        if not file_path:
            raise RuntimeError("TTS response missing file reference")
        url = file_path if file_path.startswith("http") else f"{self._cfg.host}{file_path}"
        # Single-shot on purpose — there is nothing safe to retry here. The POST
        # above already billed the synthesis, and under retain_audio=False the
        # backend burns the clip once this GET's response has been sent, so a
        # transfer that dies part-way has already destroyed the file and a
        # second GET would only 404. Either way the exception reaches
        # `_do_tts`, which logs it and answers with an empty
        # AudioStart/AudioStop: Home Assistant receives an empty clip and stays
        # silent for that one utterance (there is no local-TTS fallback — HA
        # plays what this Wyoming service returns, and this returns nothing).
        async with self._session.get(
            url, headers=self._headers(), timeout=aiohttp.ClientTimeout(total=60)
        ) as resp:
            if resp.status != 200:
                raise_for_waven_status(resp.status, await resp.text())
            return await resp.read()


# --- Wyoming handler ---------------------------------------------------------
class WavenEventHandler(AsyncEventHandler):
    def __init__(
        self,
        wyoming_info: Info,
        cfg: Config,
        api: WavenAPI,
        cap: DailyCap,
        *args,
        **kwargs,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._wyoming_info_event = wyoming_info.event()
        self._cfg = cfg
        self._api = api
        self._cap = cap
        self._audio = bytearray()
        self._rate = 16000
        self._width = 2
        self._channels = 1
        # Set when the buffer hit MAX_UTTERANCE_SECONDS and we answered early.
        self._overflowed = False
        # Per-connection (HA opens a fresh connection per STT request) — never
        # mutate the shared Config.
        self._language = cfg.stt_language

    @staticmethod
    def _today() -> str:
        # `date.today()` is the LOCAL date, so the cap resets at the container's
        # local midnight. As an HA add-on the Supervisor injects the household's
        # TZ, which is what we want. Under a plain `docker run` the container
        # defaults to UTC unless the operator passes `-e TZ=...` — documented in
        # the README, since a household several hours off UTC would otherwise
        # see the cap reset at a surprising hour.
        import datetime

        return datetime.date.today().isoformat()

    def _max_audio_bytes(self) -> int:
        """Byte ceiling for the current stream format (see MAX_UTTERANCE_SECONDS)."""
        per_second = self._rate * self._width * self._channels
        if per_second <= 0:
            per_second = 16000 * 2
        return int(MAX_UTTERANCE_SECONDS * per_second)

    async def handle_event(self, event: Event) -> bool:
        if Describe.is_type(event.type):
            await self.write_event(self._wyoming_info_event)
            return True

        # --- STT ---
        if Transcribe.is_type(event.type):
            transcribe = Transcribe.from_event(event)
            if transcribe.language:
                self._language = transcribe.language
            return True

        if AudioStart.is_type(event.type):
            start = AudioStart.from_event(event)
            self._rate, self._width, self._channels = start.rate, start.width, start.channels
            self._audio = bytearray()
            self._overflowed = False
            return True

        if AudioChunk.is_type(event.type):
            chunk = AudioChunk.from_event(event)
            if self._overflowed:
                # Already transcribed and answered; the rest of the stream is
                # ignored wholesale (format included) until the next AudioStop.
                return True
            # Format is latched at AudioStart, or — for a client that streams
            # chunks without one — at the first chunk. Re-latching on EVERY
            # chunk is what we must not do: a stream whose format drifts
            # mid-utterance had its already-buffered bytes reinterpreted at the
            # new rate/width, so the WAV header we build disagrees with most of
            # its own payload — garbage transcript, wrong billed duration.
            #
            # With an empty buffer there is nothing to reinterpret and no
            # declared format to trust (self._rate/_width/_channels are only
            # the constructor's 16k/16-bit/mono guess), so adopting the chunk's
            # own format is strictly better than dropping every chunk of the
            # utterance as a "mismatch". Drift is only rejected once bytes are
            # buffered under a known format.
            if not self._audio:
                self._rate, self._width, self._channels = (
                    chunk.rate, chunk.width, chunk.channels
                )
            elif (chunk.rate, chunk.width, chunk.channels) != (
                self._rate, self._width, self._channels
            ):
                _LOGGER.warning(
                    "Ignoring audio chunk with format %s/%s/%s; this "
                    "utterance is already buffered as %s/%s/%s.",
                    chunk.rate, chunk.width, chunk.channels,
                    self._rate, self._width, self._channels,
                )
                return True
            self._audio.extend(chunk.audio)
            if len(self._audio) > self._max_audio_bytes():
                # Force-finish rather than keep growing: nothing guarantees an
                # AudioStop ever arrives, and an unbounded buffer OOM-kills the
                # add-on on Pi-class hardware. Truncate to the ceiling, answer
                # with what we have, and ignore the rest of the stream.
                self._overflowed = True
                del self._audio[self._max_audio_bytes():]
                _LOGGER.warning(
                    "Utterance exceeded %.0f s without an AudioStop; "
                    "transcribing the first %.0f s and ignoring the rest.",
                    MAX_UTTERANCE_SECONDS, MAX_UTTERANCE_SECONDS,
                )
                text = await self._finish_stt()
                await self.write_event(Transcript(text=text).event())
                self._audio = bytearray()
            return True

        if AudioStop.is_type(event.type):
            if self._overflowed:
                # Already answered when the buffer overflowed; a second
                # Transcript would confuse the client's state machine.
                self._overflowed = False
                return True
            text = await self._finish_stt()
            await self.write_event(Transcript(text=text).event())
            self._audio = bytearray()
            return True

        # --- TTS ---
        if Synthesize.is_type(event.type):
            await self._do_tts(Synthesize.from_event(event))
            return True

        return True

    async def _charge(self, seconds: float) -> None:
        """Bill ``seconds`` against the cap and log any threshold crossed. The
        proxy is headless, so the integration's persistent notifications become
        log lines — the add-on log is the only surface a user can read.

        The state write is offloaded: ``DailyCap.save()`` is a synchronous
        open+json.dump on /data, and it ran on the event loop once per
        utterance. On an SD-card-backed Pi that is a multi-millisecond stall in
        the middle of the voice path, blocking the very socket writes that
        stream audio back."""
        today = self._today()
        self._cap.add(today, seconds, persist=False)
        await asyncio.to_thread(self._cap.save)
        for threshold in self._cap.newly_crossed():
            if threshold >= 100:
                _LOGGER.warning(
                    "Daily voice cap reached (%s min). Cloud voice is paused "
                    "until local midnight: Home Assistant receives an empty "
                    "clip and stays silent for each spoken response, and "
                    "speech-to-text returns an empty transcript. Nothing else "
                    "changes — automations keep running.",
                    self._cap.cap_minutes,
                )
            else:
                _LOGGER.warning(
                    "Daily voice cap %d%% used (%.1f of %s min).",
                    threshold,
                    self._cap.used_minutes,
                    self._cap.cap_minutes,
                )

    async def _finish_stt(self) -> str:
        if not self._audio:
            return ""
        # The whole utterance is buffered by now, so unlike the streaming
        # integration we can pre-flight against its REAL duration and decline
        # before spending the minute upstream.
        seconds = len(self._audio) / float(self._rate * self._width * self._channels or 1)
        if self._cap.would_exceed(self._today(), seconds):
            _LOGGER.warning("STT declined: daily cap reached")
            return ""
        wav_bytes = pcm_to_wav(bytes(self._audio), self._rate, self._width, self._channels)
        try:
            text = await self._api.transcribe(wav_bytes, language=self._language)
        except Exception as err:  # noqa: BLE001 - degrade gracefully, never crash the conn
            _log_api_failure("STT", err)
            return ""
        # Bill the captured duration locally.
        await self._charge(seconds)
        return text

    async def _do_tts(self, synth: Synthesize) -> None:
        voice = synth.voice.name if synth.voice else None
        # Same rough speaking-rate estimate the integration uses for its
        # pre-flight; the real duration is charged after synthesis.
        estimate = max(1.0, len(synth.text or "") / _CHARS_PER_SECOND)
        if self._cap.would_exceed(self._today(), estimate):
            _LOGGER.warning("TTS declined: daily cap reached")
            # Emit an empty start/stop so the client isn't left hanging.
            await self.write_event(AudioStart(rate=22050, width=2, channels=1).event())
            await self.write_event(AudioStop().event())
            return
        try:
            wav_bytes = await self._api.synthesize(synth.text, voice)
            pcm, rate, width, channels = wav_to_pcm(wav_bytes)
        except Exception as err:  # noqa: BLE001
            _log_api_failure("TTS", err)
            await self.write_event(AudioStart(rate=22050, width=2, channels=1).event())
            await self.write_event(AudioStop().event())
            return

        await self.write_event(AudioStart(rate=rate, width=width, channels=channels).event())
        # Stream in ~50 ms frames so playback can start promptly.
        frame_bytes = max(1, rate // 20) * width * channels
        for offset in range(0, len(pcm), frame_bytes):
            await self.write_event(
                AudioChunk(
                    rate=rate,
                    width=width,
                    channels=channels,
                    audio=pcm[offset : offset + frame_bytes],
                ).event()
            )
        await self.write_event(AudioStop().event())
        await self._charge(len(pcm) / float(rate * width * channels or 1))


def build_info() -> Info:
    voices = [
        TtsVoice(
            name=f"kokoro:{vid}",
            description=desc,
            attribution=_ATTRIBUTION,
            installed=True,
            version=None,
            languages=[lang],
        )
        for vid, desc, lang in _KOKORO_VOICES
    ]
    return Info(
        asr=[
            AsrProgram(
                name="waven",
                description="Waven hosted speech-to-text",
                attribution=_ATTRIBUTION,
                installed=True,
                version=VERSION,
                models=[
                    AsrModel(
                        name="waven-stt",
                        description="Waven cloud STT (Parakeet / Voxtral)",
                        attribution=_ATTRIBUTION,
                        installed=True,
                        languages=_STT_LANGUAGES,
                        version=None,
                    )
                ],
            )
        ],
        tts=[
            TtsProgram(
                name="waven",
                description="Waven hosted text-to-speech",
                attribution=_ATTRIBUTION,
                installed=True,
                version=VERSION,
                voices=voices,
            )
        ],
    )


async def main() -> None:
    parser = argparse.ArgumentParser(description="Waven Wyoming proxy")
    parser.add_argument("--uri", help="Wyoming bind URI (overrides config)")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.debug else logging.INFO)

    cfg = Config.load()
    if args.uri:
        cfg.uri = args.uri
    if not cfg.api_key:
        raise SystemExit("No Waven API key configured (set api_key / WAVEN_API_KEY).")

    info = build_info()
    # /data is the add-on's persistent volume; plain `docker run` users can point
    # WAVEN_STATE_PATH at a mounted file (or leave it unset for no persistence).
    cap = DailyCap(
        cfg.daily_cap_minutes,
        state_path=os.environ.get("WAVEN_STATE_PATH", "/data/waven_cap.json"),
    )
    cap.load()

    _LOGGER.info(
        "Waven Wyoming proxy %s listening on %s (host=%s, daily cap=%s)",
        VERSION,
        cfg.uri,
        cfg.host,
        f"{cfg.daily_cap_minutes} min" if cfg.daily_cap_minutes > 0 else "disabled",
    )
    async with aiohttp.ClientSession() as session:
        api = WavenAPI(session, cfg)
        server = AsyncServer.from_uri(cfg.uri)
        await server.run(partial(WavenEventHandler, info, cfg, api, cap))


if __name__ == "__main__":
    asyncio.run(main())
