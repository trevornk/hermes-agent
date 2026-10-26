"""Exact carried Matrix cross-loop tests isolated from unrelated Telegram dependency."""
import asyncio
import sys
from types import SimpleNamespace
from unittest.mock import patch
from gateway.config import Platform
from tools.send_message_senders import _send_matrix_via_adapter

class TestMatrixCrossloopReuse:
    def test_live_adapter_present_but_wrong_loop_falls_back_to_ephemeral(self, tmp_path):
        """Regression test for the cron standalone-delivery-fallback crash (2026-08-02):
        when the cron scheduler's live-adapter attempt itself fails/times out, it retries
        via _send_to_platform inside a *fresh* asyncio.run() loop. If that fresh loop then
        reused the gateway's live Matrix adapter (whose aiohttp session and
        asyncio.wait_for timeout context are bound to the gateway's own loop), the send
        raised "Timeout context manager should be used inside a task".

        A runner whose _gateway_loop differs from the loop we're actually running on must
        be treated as unusable, falling through to the ephemeral (loop-agnostic) adapter
        instead of reusing the live one.
        """
        doc_path = tmp_path / "report.pdf"
        doc_path.write_bytes(b"%PDF-1.4")

        calls = []

        class LiveAdapter:
            async def send(self, chat_id, message, metadata=None):
                calls.append(("live_send", chat_id, message))
                return SimpleNamespace(success=True, message_id="$live")

        class EphemeralAdapter:
            def __init__(self, _config):
                pass

            async def connect(self):
                calls.append(("connect",))
                return True

            async def send(self, chat_id, message, metadata=None):
                calls.append(("ephemeral_send", chat_id, message))
                return SimpleNamespace(success=True, message_id="$ephemeral")

            async def send_document(self, chat_id, path, metadata=None):
                calls.append(("send_document", chat_id, path))
                return SimpleNamespace(success=True, message_id="$doc")

            async def disconnect(self):
                calls.append(("disconnect",))

        async def _run():
            # Simulate the gateway's own loop by grabbing a real loop object that is NOT
            # the one this test coroutine executes on.
            other_loop = asyncio.new_event_loop()
            try:
                live_adapter = LiveAdapter()
                fake_runner = SimpleNamespace(
                    adapters={Platform.MATRIX: live_adapter},
                    _gateway_loop=other_loop,
                )
                fake_module = SimpleNamespace(MatrixAdapter=EphemeralAdapter)
                with patch(
                    "gateway.run._gateway_runner_ref", return_value=fake_runner
                ), patch.dict(
                    sys.modules, {"plugins.platforms.matrix.adapter": fake_module}
                ):
                    return await _send_matrix_via_adapter(
                        SimpleNamespace(enabled=True, token="tok", extra={}),
                        "!room:example.com",
                        "report attached",
                        media_files=[(str(doc_path), False)],
                    )
            finally:
                other_loop.close()

        result = asyncio.run(_run())

        assert result["success"] is True
        # The live adapter must NOT be used across loops — only the loop-agnostic
        # ephemeral adapter path should have run.
        assert ("live_send", "!room:example.com", "report attached") not in calls
        assert calls == [
            ("connect",),
            ("ephemeral_send", "!room:example.com", "report attached"),
            ("send_document", "!room:example.com", str(doc_path)),
            ("disconnect",),
        ]

    def test_live_adapter_same_loop_as_gateway_is_reused(self, tmp_path):
        """POSITIVE companion: when runner._gateway_loop IS the current running loop (the
        normal in-gateway send path — no cron fresh-loop fallback involved), the live
        adapter fast path must still be taken, exactly as before this carry."""
        img_path = tmp_path / "photo.png"
        img_path.write_bytes(b"\x89PNG\r\n")

        calls = []

        class LiveAdapter:
            async def send(self, chat_id, message, metadata=None):
                calls.append(("send", chat_id, message))
                return SimpleNamespace(success=True, message_id="$text")

            async def send_image_file(self, chat_id, path, metadata=None):
                calls.append(("send_image_file", chat_id, path))
                return SimpleNamespace(success=True, message_id="$img")

        async def _run():
            current_loop = asyncio.get_running_loop()
            live_adapter = LiveAdapter()
            fake_runner = SimpleNamespace(
                adapters={Platform.MATRIX: live_adapter},
                _gateway_loop=current_loop,
            )
            with patch(
                "gateway.run._gateway_runner_ref", return_value=fake_runner,
            ), patch.dict(
                sys.modules, {"plugins.platforms.matrix.adapter": SimpleNamespace()}
            ):
                return await _send_matrix_via_adapter(
                    SimpleNamespace(enabled=True, token="tok", extra={}),
                    "!room:example.com",
                    "here is an image",
                    media_files=[(str(img_path), False)],
                )

        result = asyncio.run(_run())

        assert result["success"] is True
        assert result["message_id"] == "$img"
        assert calls == [
            ("send", "!room:example.com", "here is an image"),
            ("send_image_file", "!room:example.com", str(img_path)),
        ]
