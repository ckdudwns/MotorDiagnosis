"""Service shutdown must leave database handles available to delivery guards."""
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
import signal
import threading
import unittest
from unittest import mock

import app
from motor_diagnosis.server import create_server


class ShutdownOperationsTest(unittest.TestCase):
    def test_term_handler_exits_accept_loop_and_restores_handler(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler)
        self.addCleanup(server.server_close)
        previous = signal.getsignal(signal.SIGTERM)
        handler_installed = threading.Event()
        original_signal = signal.signal
        installed = []

        def register(signum, callback):
            result = original_signal(signum, callback)
            if callable(callback) and callback != previous:
                installed.append(callback)
                handler_installed.set()
            return result

        def request_stop():
            if handler_installed.wait(2):
                installed[0](signal.SIGTERM, None)

        request = threading.Thread(target=request_stop, daemon=True)
        request.start()
        fallback_used = threading.Event()
        def fallback():
            fallback_used.set()
            server.shutdown()
        timer = threading.Timer(3, fallback)
        timer.daemon = True
        timer.start()
        try:
            with mock.patch.object(app, "create_server", return_value=server), \
                 mock.patch.object(app.signal, "signal", side_effect=register), \
                 mock.patch("builtins.print"), mock.patch.object(server, "server_close", wraps=server.server_close) as close:
                app.main()
                close.assert_called_once()
            self.assertTrue(installed)
            self.assertFalse(fallback_used.is_set())
            self.assertEqual(signal.getsignal(signal.SIGTERM), previous)
        finally:
            timer.cancel()
            request.join(timeout=2)
            original_signal(signal.SIGTERM, previous)

    def test_delivery_close_can_still_read_raw_database(self):
        server = create_server("127.0.0.1", 0, demo_enabled=False, auto_alerts=False)
        close = server.alerts.close
        read = []
        def drain():
            read.append(server.raw_vibration.db.execute("SELECT count(*) FROM rf66_incidents").fetchone()[0])
            close()
        with mock.patch.object(server.alerts, "close", side_effect=drain):
            server.server_close()
        self.assertEqual(read, [0])
