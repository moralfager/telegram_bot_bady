import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from supplement_shop.telegram_api import TelegramApiError, run_polling


class PollingTest(unittest.TestCase):
    def test_temporary_get_me_failure_retries(self):
        stop = threading.Event()
        client = Mock()
        client.get_me.side_effect = [TelegramApiError('getMe', 'timeout'), {'username':'test'}]
        def updates(**kwargs):
            stop.set()
            return []
        client.get_updates.side_effect = updates
        with patch.object(stop, 'wait', return_value=False):
            run_polling(name='test', client=client, timeout=1, handler=Mock(), stop_event=stop)
        self.assertEqual(client.get_me.call_count, 2)
        self.assertEqual(client.get_updates.call_count, 1)

    def test_invalid_token_stops_other_workers(self):
        stop = threading.Event()
        client = Mock()
        client.get_me.side_effect = TelegramApiError('getMe', 'Unauthorized', 401)
        run_polling(name='test', client=client, timeout=1, handler=Mock(), stop_event=stop)
        self.assertTrue(stop.is_set())
        client.get_updates.assert_not_called()
