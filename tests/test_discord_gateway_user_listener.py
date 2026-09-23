import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from src.services.listener.discord_gateway_user_listener import DiscordGatewayUserListener


class FakeEmbed:
    def to_dict(self):
        return {
            "title": "Signal",
            "description": "Entry now",
            "fields": [{"name": "Price", "value": "123"}],
            "image": {"url": "https://cdn.example/embed.png"},
        }


class DiscordGatewayUserListenerTest(unittest.TestCase):
    def make_listener(self):
        return DiscordGatewayUserListener(
            ["https://discord.com/channels/1/123"],
            lambda message: True,
            "token",
        )

    def test_builds_message_with_embeds_and_attachments(self):
        listener = self.make_listener()
        channel = SimpleNamespace(id=123, name="signals", guild=SimpleNamespace(name="Market"))
        message = SimpleNamespace(
            id=456,
            channel=channel,
            author=SimpleNamespace(display_name="Alice"),
            content="Hello",
            embeds=[FakeEmbed()],
            attachments=[SimpleNamespace(url="https://cdn.example/file.png")],
            stickers=[],
            created_at=datetime(2026, 9, 23, tzinfo=timezone.utc),
        )

        parsed = listener._message_from_discord(message)

        self.assertEqual("456", parsed.id)
        self.assertEqual("Alice", parsed.username)
        self.assertEqual("Market: signals", parsed.channel_name)
        self.assertIn("Signal", parsed.content)
        self.assertIn("Price: 123", parsed.content)
        self.assertEqual(
            ["https://cdn.example/file.png", "https://cdn.example/embed.png"],
            parsed.attachments,
        )

    def test_recovery_uses_recent_messages_in_chronological_order(self):
        now = datetime.now(timezone.utc)
        newest = SimpleNamespace(id=3, created_at=now)
        middle = SimpleNamespace(id=2, created_at=now - timedelta(minutes=1))
        old = SimpleNamespace(id=1, created_at=now - timedelta(days=2))

        selected = DiscordGatewayUserListener._select_recovery_messages(
            [newest, middle, old],
            now - timedelta(days=1),
        )

        self.assertEqual([2, 3], [message.id for message in selected])

    def test_recovery_keeps_latest_when_all_messages_are_old(self):
        now = datetime.now(timezone.utc)
        newest = SimpleNamespace(id=3, created_at=now - timedelta(days=2))
        older = SimpleNamespace(id=2, created_at=now - timedelta(days=3))

        selected = DiscordGatewayUserListener._select_recovery_messages(
            [newest, older],
            now - timedelta(days=1),
        )

        self.assertEqual([3], [message.id for message in selected])

    def test_seen_message_cache_is_bounded(self):
        listener = DiscordGatewayUserListener(
            ["https://discord.com/channels/1/123"],
            lambda message: True,
            "token",
            max_seen_messages=100,
        )
        for index in range(150):
            self.assertTrue(listener._remember_message(str(index)))

        self.assertEqual(100, len(listener.seen_message_keys))
        self.assertNotIn("0", listener.seen_message_keys)
        self.assertFalse(listener._remember_message("149"))


if __name__ == "__main__":
    unittest.main()
