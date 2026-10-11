"""Person-owned GNSIS persona and voice preferences."""

from __future__ import annotations

import base64
import io
import secrets
import wave
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select

from . import orm
from .db import session_scope

DEFAULT_VOICE_PROFILE_ID = "gnsis-default"
DEFAULT_VOICE_PACE = "relaxed"
MAX_REFERENCE_AUDIO_BYTES = 5 * 1024 * 1024
VOICE_PACES = ("relaxed", "normal", "quick")
PRESET_VOICE_PROFILES = (
    {
        "id": DEFAULT_VOICE_PROFILE_ID,
        "name": "GNSIS Default",
        "description": "The approved default GNSIS voice.",
        "kind": "preset",
    },
)


class DesktopPreferenceError(ValueError):
    pass


@dataclass(frozen=True)
class DesktopPreferences:
    person_id: str
    persona_description: str
    voice_profile_id: str
    voice_pace: str
    voice_profiles: tuple[dict[str, Any], ...]
    can_upload_voice: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "person_id": self.person_id,
            "persona_description": self.persona_description,
            "voice_profile_id": self.voice_profile_id,
            "voice_pace": self.voice_pace,
            "available_voice_profiles": list(self.voice_profiles),
            "available_voice_paces": list(VOICE_PACES),
            "can_upload_voice": self.can_upload_voice,
        }


def _voice_profiles(session: Any, person_id: str) -> tuple[dict[str, Any], ...]:
    custom = session.scalars(
        select(orm.DesktopVoiceProfile)
        .where(orm.DesktopVoiceProfile.person_id == person_id)
        .order_by(orm.DesktopVoiceProfile.created_at, orm.DesktopVoiceProfile.id)
    ).all()
    return tuple(PRESET_VOICE_PROFILES) + tuple(
        {
            "id": profile.id,
            "name": profile.name,
            "description": "Private voice for this account.",
            "kind": "custom",
        }
        for profile in custom
    )


def _view(
    session: Any,
    person: orm.DesktopPerson,
    *,
    can_upload_voice: bool,
) -> DesktopPreferences:
    return DesktopPreferences(
        person_id=person.id,
        persona_description=person.persona_description or "",
        voice_profile_id=person.voice_profile_id or DEFAULT_VOICE_PROFILE_ID,
        voice_pace=person.voice_pace or DEFAULT_VOICE_PACE,
        voice_profiles=_voice_profiles(session, person.id),
        can_upload_voice=can_upload_voice,
    )


def get_preferences(
    person_id: str,
    *,
    can_upload_voice: bool = False,
) -> DesktopPreferences:
    with session_scope() as session:
        person = session.get(orm.DesktopPerson, person_id)
        if person is None:
            raise DesktopPreferenceError("desktop person was not found")
        return _view(session, person, can_upload_voice=can_upload_voice)


def update_preferences(
    person_id: str,
    *,
    persona_description: str | None = None,
    voice_profile_id: str | None = None,
    voice_pace: str | None = None,
    can_upload_voice: bool = False,
) -> DesktopPreferences:
    if voice_pace is not None and voice_pace not in VOICE_PACES:
        raise DesktopPreferenceError("voice pace is invalid")
    with session_scope() as session:
        person = session.get(orm.DesktopPerson, person_id)
        if person is None:
            raise DesktopPreferenceError("desktop person was not found")
        if voice_profile_id is not None:
            preset_ids = {profile["id"] for profile in PRESET_VOICE_PROFILES}
            custom = (
                session.get(orm.DesktopVoiceProfile, voice_profile_id)
                if voice_profile_id not in preset_ids
                else None
            )
            if voice_profile_id not in preset_ids and (
                custom is None or custom.person_id != person_id
            ):
                raise DesktopPreferenceError("voice profile is not available")
        if persona_description is not None:
            person.persona_description = persona_description.strip()
        if voice_profile_id is not None:
            person.voice_profile_id = voice_profile_id
        if voice_pace is not None:
            person.voice_pace = voice_pace
        session.flush()
        return _view(session, person, can_upload_voice=can_upload_voice)


def create_custom_voice_profile(
    person_id: str,
    *,
    name: str,
    transcript: str,
    audio_wav_b64: str,
    can_upload_voice: bool,
) -> DesktopPreferences:
    if not can_upload_voice:
        raise DesktopPreferenceError("custom voice upload is not enabled")
    clean_name = name.strip()
    clean_transcript = transcript.strip()
    if not clean_name:
        raise DesktopPreferenceError("voice name is required")
    if not clean_transcript:
        raise DesktopPreferenceError("reference transcript is required")
    try:
        audio_wav = base64.b64decode(audio_wav_b64, validate=True)
    except (ValueError, TypeError) as exc:
        raise DesktopPreferenceError("reference audio is not valid base64") from exc
    if not audio_wav or len(audio_wav) > MAX_REFERENCE_AUDIO_BYTES:
        raise DesktopPreferenceError("reference audio must be at most 5 MB")
    try:
        with wave.open(io.BytesIO(audio_wav), "rb") as reader:
            if reader.getnchannels() not in {1, 2}:
                raise DesktopPreferenceError("reference audio must be mono or stereo")
            if reader.getsampwidth() != 2:
                raise DesktopPreferenceError("reference audio must be 16-bit PCM WAV")
            frame_rate = reader.getframerate()
            duration = reader.getnframes() / frame_rate if frame_rate else 0.0
    except (wave.Error, EOFError) as exc:
        raise DesktopPreferenceError(
            "reference audio must be a valid PCM WAV"
        ) from exc
    if not 3.0 <= duration <= 30.0:
        raise DesktopPreferenceError("reference audio must be 3 to 30 seconds")

    with session_scope() as session:
        person = session.get(orm.DesktopPerson, person_id)
        if person is None:
            raise DesktopPreferenceError("desktop person was not found")
        profile = orm.DesktopVoiceProfile(
            id=f"voice_{secrets.token_hex(12)}",
            person_id=person_id,
            name=clean_name,
            transcript=clean_transcript,
            audio_wav=audio_wav,
        )
        session.add(profile)
        person.voice_profile_id = profile.id
        session.flush()
        return _view(session, person, can_upload_voice=True)


def delete_custom_voice_profile(
    person_id: str,
    profile_id: str,
    *,
    can_upload_voice: bool,
) -> DesktopPreferences:
    if not can_upload_voice:
        raise DesktopPreferenceError("custom voice upload is not enabled")
    with session_scope() as session:
        person = session.get(orm.DesktopPerson, person_id)
        profile = session.get(orm.DesktopVoiceProfile, profile_id)
        if person is None or profile is None or profile.person_id != person_id:
            raise DesktopPreferenceError("voice profile was not found")
        if person.voice_profile_id == profile_id:
            person.voice_profile_id = DEFAULT_VOICE_PROFILE_ID
        session.delete(profile)
        session.flush()
        return _view(session, person, can_upload_voice=True)


def get_voice_reference(person_id: str, profile_id: str) -> tuple[bytes, str] | None:
    if profile_id == DEFAULT_VOICE_PROFILE_ID:
        return None
    with session_scope() as session:
        profile = session.get(orm.DesktopVoiceProfile, profile_id)
        if profile is None or profile.person_id != person_id:
            raise DesktopPreferenceError("voice profile was not found")
        return bytes(profile.audio_wav), profile.transcript
