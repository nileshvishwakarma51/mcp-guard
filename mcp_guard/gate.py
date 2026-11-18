"""The policy point shared by both proxies: "may this request proceed?"."""

from __future__ import annotations

import asyncio
import logging

from .broker_client import BrokerClient, BrokerUnavailable
from .protocol import PendingCall

log = logging.getLogger(__name__)


class Gate:
    """Ask the broker; translate every failure into a safe decision.

    Fail-closed is the default: if the user cannot be asked (broker crashed,
    refused to start, returned garbage) the request is *blocked*.  An
    approval system that silently allows on error provides no protection.
    """

    def __init__(self, broker: BrokerClient, *, fail_open: bool = False) -> None:
        self.broker = broker
        self.fail_open = fail_open

    async def check(self, call: PendingCall) -> tuple[bool, str]:
        try:
            allowed, reason = await self.broker.request_decision(call)
        except asyncio.CancelledError:
            raise
        except BrokerUnavailable as exc:
            log.error("broker unavailable for %s/%s: %s", call.server, call.tool, exc)
            if self.fail_open:
                return True, f"broker unavailable ({exc}); allowed because --fail-open is set"
            return False, f"approval service unavailable ({exc}); request blocked for safety"
        except Exception as exc:  # pragma: no cover - defensive
            log.exception("unexpected error while asking for a decision")
            return False, f"internal error in MCP Guard ({type(exc).__name__}); request blocked for safety"
        log.info("%s %s/%s (id=%r): %s", "ALLOW" if allowed else "BLOCK", call.server,
                 call.tool or call.method, call.request_id, reason)
        return allowed, reason

    async def close(self) -> None:
        await self.broker.close()
