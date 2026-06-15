"""Notifier — deliver messages by shelling out to the OpenClaw binary.

roomieorder's worker is a long-running daemon, so unlike commutecompass's
oneshot timers (which pipe delimited stdout through openclaw-send.sh) it calls
``openclaw message send`` directly, once per message. Notifications are
best-effort: a delivery failure is logged but never crashes the worker or
fails an order — the order already happened.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import Optional, Protocol

from roomieorder.config import Config

_logger = logging.getLogger(__name__)

# Hard cap so a wedged openclaw binary can't hang the worker forever.
_SEND_TIMEOUT_SECONDS = 20.0


class Notifier(Protocol):
    def send(self, text: str, photo: Optional[Path] = None) -> bool: ...


class OpenClawNotifier:
    """Send messages via ``openclaw message send``.

    Args:
        binary: path to (or name on PATH of) the openclaw executable.
        target: delivery target — for Telegram, the numeric chat id.
        channel: openclaw channel name (default ``telegram``).
    """

    def __init__(self, binary: str, target: str, channel: str = "telegram") -> None:
        self.binary = binary
        self.target = target
        self.channel = channel

    def send(self, text: str, photo: Optional[Path] = None) -> bool:
        """Deliver one message, optionally with a screenshot attached.

        Returns True on a clean exit, False on any failure (non-zero exit,
        timeout, missing binary). Never raises — callers treat notification
        as fire-and-forget.
        """
        cmd = [
            self.binary,
            "message",
            "send",
            "--channel",
            self.channel,
            "--target",
            self.target,
            "--message",
            text,
        ]
        if photo is not None:
            # openclaw's attachment flag is --media <path-or-url> (handles
            # image/audio/video/document); the older --photo was removed.
            # openclaw runs a separate gateway process and resolves a relative
            # path against *its* cwd, so always hand it an absolute path or the
            # screenshot is silently undeliverable. (The path must also live
            # under one of openclaw's allowed media roots — the deployment
            # points the shots dir there.)
            cmd += ["--media", str(photo.resolve())]
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=_SEND_TIMEOUT_SECONDS,
                check=False,
            )
        except FileNotFoundError:
            _logger.error("openclaw binary not found: %s", self.binary)
            return False
        except subprocess.TimeoutExpired:
            _logger.error("openclaw send timed out after %ss", _SEND_TIMEOUT_SECONDS)
            return False
        if result.returncode != 0:
            _logger.warning(
                "openclaw send failed (exit %d): %s",
                result.returncode,
                (result.stderr or "").strip()[:200],
            )
            return False
        return True


class NullNotifier:
    """No-op notifier used when no OpenClaw target is configured.

    Logs at INFO so a misconfiguration (forgot OPENCLAW_TARGET) is visible in
    the journal without taking the service down.
    """

    def send(self, text: str, photo: Optional[Path] = None) -> bool:
        _logger.info("notify (no target configured): %s", text.replace("\n", " ")[:200])
        return True


def build_notifier(config: Config) -> Notifier:
    if config.notify_enabled:
        return OpenClawNotifier(
            binary=config.openclaw_bin,
            target=config.openclaw_target,
            channel=config.openclaw_channel,
        )
    return NullNotifier()
