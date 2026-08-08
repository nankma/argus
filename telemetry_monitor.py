"""
Periodically checks whether Phoenix's OTLP endpoint is reachable, and
alerts the admin (via admin_bot.py's token) when it goes down or recovers.

Why a separate active check instead of hooking OpenTelemetry's own export
failures: the OTel SDK's exporter logs failures internally but doesn't
raise into application code (by design -- telemetry failures shouldn't
crash the app), so there's nothing to catch there. A simple periodic TCP
check against the same host/port used for real trace export is more
direct and easier to reason about.

Edge-triggered, not level-triggered: only sends a message when the state
actually changes (up->down or down->up), not on every check, so an
ongoing outage doesn't spam the admin every interval.
"""

import asyncio
from telegram import Bot


async def check_phoenix_reachable(host: str, port: int, timeout: float = 5.0) -> bool:
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=timeout)
    except (OSError, asyncio.TimeoutError):
        return False
    writer.close()
    await writer.wait_closed()
    return True


async def check_and_alert(
    admin_bot_token: str, admin_chat_id: int, host: str, port: int, was_healthy: bool
) -> bool:
    """Runs one health-check iteration. Returns the new health state so the
    caller can pass it back in as `was_healthy` on the next call."""
    healthy = await check_phoenix_reachable(host, port)
    if healthy != was_healthy:
        text = (
            f"Phoenix telemetry is reachable again at {host}:{port}."
            if healthy
            else f"Phoenix telemetry is unreachable at {host}:{port} — traces are not being recorded."
        )
        await Bot(token=admin_bot_token).send_message(chat_id=admin_chat_id, text=text)
    return healthy


async def monitor_telemetry_health(
    admin_bot_token: str, admin_chat_id: int, host: str, port: int, interval_seconds: float = 300
) -> None:
    was_healthy = True
    while True:
        was_healthy = await check_and_alert(admin_bot_token, admin_chat_id, host, port, was_healthy)
        await asyncio.sleep(interval_seconds)
