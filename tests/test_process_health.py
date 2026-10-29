import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from services import process_health


class ProcessHealthTests(unittest.TestCase):
    def test_heartbeat_survives_replace_permission_error(self):
        async def check():
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                heartbeat = root / 'health.json'
                dump = root / 'hang.log'
                with patch.dict(os.environ, {
                    'NYABOT_HEARTBEAT_FILE': str(heartbeat),
                    'NYABOT_THREAD_DUMP_FILE': str(dump),
                }), patch.object(process_health.os, 'replace', side_effect=PermissionError()):
                    task = asyncio.create_task(process_health.heartbeat_loop())
                    await asyncio.sleep(2.2)
                    self.assertTrue(heartbeat.exists())
                    data = json.loads(heartbeat.read_text())
                    self.assertEqual(data['pid'], os.getpid())
                    task.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await task
        asyncio.run(check())


if __name__ == '__main__':
    unittest.main()
