import unittest

from platforms.chatgpt.oauth_resume_cache import OAuthResumeContextCache


class OAuthResumeContextCacheTests(unittest.TestCase):
    def test_take_is_case_insensitive_and_one_time(self):
        now = [100.0]
        cache = OAuthResumeContextCache(ttl_seconds=60, clock=lambda: now[0])
        session = object()

        cache.remember(
            "Existing@Example.com",
            session=session,
            device_id="device-1",
            user_agent="UA",
            sec_ch_ua='"Chromium";v="136"',
            accept_language="en-US",
            impersonate="chrome136",
        )

        context = cache.take("existing@example.COM")
        self.assertIs(context.session, session)
        self.assertEqual(context.device_id, "device-1")
        self.assertEqual(context.impersonate, "chrome136")
        self.assertIsNone(cache.take("existing@example.com"))

    def test_expired_context_is_not_returned(self):
        now = [100.0]
        cache = OAuthResumeContextCache(ttl_seconds=30, clock=lambda: now[0])
        cache.remember("existing@example.com", session=object())

        now[0] = 131.0

        self.assertIsNone(cache.take("existing@example.com"))


if __name__ == "__main__":
    unittest.main()
