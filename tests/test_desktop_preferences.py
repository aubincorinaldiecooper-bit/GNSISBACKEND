"""Person-owned persona and voice preference tests."""

from __future__ import annotations

import base64
import io
import os
import sys
import unittest
import wave

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from _authkit import fresh_sqlite_env  # noqa: E402


class DesktopPreferencesTests(unittest.TestCase):
    def setUp(self):
        fresh_sqlite_env()
        from gnsis.service import orm
        from gnsis.service.db import init_db, session_scope

        init_db()
        with session_scope() as session:
            session.add_all(
                [
                    orm.DesktopPerson(
                        id="person_alpha",
                        legacy_public_id="gnsis:AAAA-BBBB-CCCC",
                        recovery_key_hash="a" * 64,
                    ),
                    orm.DesktopPerson(
                        id="person_beta",
                        legacy_public_id="gnsis:DDDD-EEEE-FFFF",
                        recovery_key_hash="b" * 64,
                    ),
                ]
            )

    def test_defaults_are_server_owned_and_relaxed(self):
        from gnsis.service.desktop_preferences import get_preferences

        preferences = get_preferences("person_alpha")
        self.assertEqual(preferences.voice_profile_id, "gnsis-default")
        self.assertEqual(preferences.voice_pace, "relaxed")
        self.assertEqual(preferences.persona_description, "")

    def test_updates_follow_the_person_not_a_device(self):
        from gnsis.service.desktop_preferences import (
            get_preferences,
            update_preferences,
        )

        update_preferences(
            "person_alpha",
            persona_description="Warm, observant, and concise.",
            voice_profile_id="gnsis-default",
            voice_pace="normal",
        )
        inherited = get_preferences("person_alpha")
        self.assertEqual(inherited.voice_pace, "normal")
        self.assertEqual(
            inherited.persona_description,
            "Warm, observant, and concise.",
        )

    def test_people_cannot_leak_preferences_into_each_other(self):
        from gnsis.service.desktop_preferences import (
            get_preferences,
            update_preferences,
        )

        update_preferences("person_alpha", voice_pace="quick")
        self.assertEqual(get_preferences("person_alpha").voice_pace, "quick")
        self.assertEqual(get_preferences("person_beta").voice_pace, "relaxed")

    def test_rejects_unapproved_voice_and_invalid_pace(self):
        from gnsis.service.desktop_preferences import (
            DesktopPreferenceError,
            update_preferences,
        )

        with self.assertRaises(DesktopPreferenceError):
            update_preferences("person_alpha", voice_profile_id="unknown")
        with self.assertRaises(DesktopPreferenceError):
            update_preferences("person_alpha", voice_pace="very-fast")

    @staticmethod
    def _reference_wav() -> str:
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as writer:
            writer.setnchannels(1)
            writer.setsampwidth(2)
            writer.setframerate(16_000)
            writer.writeframes(bytes(16_000 * 2 * 3))
        return base64.b64encode(buffer.getvalue()).decode()

    def test_custom_voice_is_private_and_owner_gated(self):
        from gnsis.service.desktop_preferences import (
            DesktopPreferenceError,
            create_custom_voice_profile,
            get_preferences,
            get_voice_reference,
            delete_custom_voice_profile,
            update_preferences,
        )

        with self.assertRaises(DesktopPreferenceError):
            create_custom_voice_profile(
                "person_alpha",
                name="My voice",
                transcript="This is my reference.",
                audio_wav_b64=self._reference_wav(),
                can_upload_voice=False,
            )
        preferences = create_custom_voice_profile(
            "person_alpha",
            name="My voice",
            transcript="This is my reference.",
            audio_wav_b64=self._reference_wav(),
            can_upload_voice=True,
        )
        profile_id = preferences.voice_profile_id
        self.assertTrue(profile_id.startswith("voice_"))
        self.assertIsNotNone(get_voice_reference("person_alpha", profile_id))
        with self.assertRaises(DesktopPreferenceError):
            get_voice_reference("person_beta", profile_id)
        with self.assertRaises(DesktopPreferenceError):
            update_preferences("person_beta", voice_profile_id=profile_id)
        self.assertEqual(
            get_preferences("person_beta").voice_profile_id,
            "gnsis-default",
        )
        deleted = delete_custom_voice_profile(
            "person_alpha",
            profile_id,
            can_upload_voice=True,
        )
        self.assertEqual(deleted.voice_profile_id, "gnsis-default")
        self.assertIsNone(get_voice_reference("person_alpha", "gnsis-default"))

    def test_custom_voice_rejects_invalid_audio(self):
        from gnsis.service.desktop_preferences import (
            DesktopPreferenceError,
            create_custom_voice_profile,
        )

        with self.assertRaises(DesktopPreferenceError):
            create_custom_voice_profile(
                "person_alpha",
                name="Invalid",
                transcript="This is not a wave file.",
                audio_wav_b64=base64.b64encode(b"not-wave").decode(),
                can_upload_voice=True,
            )


if __name__ == "__main__":
    unittest.main()
