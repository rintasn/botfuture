"""Authenticated Binance user-data WebSocket with a thread-safe event queue."""

import asyncio
import queue
import threading

import ccxt.pro as ccxtpro

import config
from logger_setup import logger


class UserDataStream:
    """Watch order/position updates; REST remains the reconciliation authority."""

    def __init__(self, api_key, secret, trading_mode="live"):
        self.api_key = api_key
        self.secret = secret
        self.trading_mode = trading_mode
        self.events = queue.Queue(maxsize=500)
        self._stop = threading.Event()
        self._thread = None
        self._loop = None
        self._exchange = None

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(
            target=self._thread_main,
            name="binance-user-stream",
            daemon=True,
        )
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._loop and self._exchange:
            try:
                asyncio.run_coroutine_threadsafe(self._exchange.close(), self._loop)
            except Exception:
                pass
        if self._thread:
            self._thread.join(timeout=5)

    def drain(self):
        items = []
        while True:
            try:
                items.append(self.events.get_nowait())
            except queue.Empty:
                return items

    def _publish(self, event):
        try:
            self.events.put_nowait(event)
        except queue.Full:
            try:
                self.events.get_nowait()
                self.events.put_nowait(event)
            except queue.Empty:
                pass

    def _thread_main(self):
        try:
            asyncio.run(self._run())
        except Exception as e:
            self._publish({"type": "health", "status": "error", "error": str(e)})

    async def _watch(self, method_name, event_type):
        delay = 1
        while not self._stop.is_set():
            try:
                payload = await getattr(self._exchange, method_name)()
                self._publish({"type": event_type, "payload": payload})
                self._publish({"type": "health", "status": "connected"})
                delay = 1
            except asyncio.CancelledError:
                return
            except Exception as e:
                self._publish({"type": "health", "status": "degraded", "error": str(e)})
                logger.warning(f"⚠️ User WebSocket {event_type} reconnect: {e}")
                await asyncio.sleep(delay)
                delay = min(delay * 2, config.USER_STREAM_RECONNECT_MAX_SECONDS)

    async def _run(self):
        self._loop = asyncio.get_running_loop()
        self._exchange = ccxtpro.binance({
            "apiKey": self.api_key,
            "secret": self.secret,
            "enableRateLimit": True,
            "options": {
                "defaultType": "future",
                "fetchCurrencies": False,
            },
        })
        if self.trading_mode == "testnet":
            self._exchange.set_sandbox_mode(True)

        tasks = [
            asyncio.create_task(self._watch("watch_orders", "orders")),
            asyncio.create_task(self._watch("watch_positions", "positions")),
        ]
        try:
            while not self._stop.is_set():
                await asyncio.sleep(0.5)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await self._exchange.close()

