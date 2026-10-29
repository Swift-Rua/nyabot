"""Deadlock regression and real child-process recovery checks."""
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from services import supervisor


class RecoveryTests(unittest.TestCase):
    def test_stop_request_stops_worker_without_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            timer = threading.Timer(1, lambda: (root / 'supervisor.stop').touch())
            with patch.object(supervisor, 'DATA_DIR', root):
                timer.start()
                try:
                    result = supervisor.run([sys.executable, '-c', 'import time; time.sleep(30)'],
                                            startup_seconds=5, restart_delay=0, max_restarts=0)
                finally:
                    timer.join()
            self.assertEqual(result, 0)
            self.assertNotIn('restarting:', (root / 'supervisor.log').read_text())

    def test_streak_and_milestones_do_not_deadlock_or_lose_events(self):
        # A subprocess timeout fails cleanly even if a threading.Lock regresses.
        code = '''
import tempfile
from pathlib import Path
from services import event_system as e
with tempfile.TemporaryDirectory() as directory:
    e.EVENT_STATE_FILE = str(Path(directory) / 'state.json')
    for i in range(3):
        e.on_group_message('g', 'u', 'name', message_count=100 if i == 2 else 1)
    state = e._load_state()
    assert state['streaks']['g']['count'] == 3
    assert state['milestones']['g']['u'] == [100]
    assert [x['type'] for x in state['pending_events']['g']] == ['streak', 'milestone']
    e.on_group_message('g', 'u', 'name', message_count=1000)
    state = e._load_state()
    assert state['milestones']['g']['u'] == [100, 1000]
    assert len(state['pending_events']['g']) == 3
    e.on_group_message('g', 'u', 'name', message_count=1000)
    assert len(e.take_events('g', 5)) == 3
'''
        subprocess.run([sys.executable, '-c', code], check=True, timeout=8)

    def test_stalled_child_is_killed_and_restarted(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            marker = root / 'restarted'
            code = '''
import json,os,time,sys
from pathlib import Path
marker = Path(sys.argv[1])
if marker.exists():
    marker.write_text('recovered')
    sys.exit(0)
marker.write_text('stalled')
Path(os.environ['NYABOT_HEARTBEAT_FILE']).write_text(json.dumps({'monotonic':time.monotonic(),'pid':os.getpid()}))
time.sleep(30)
'''
            with patch.object(supervisor, 'DATA_DIR', root):
                result = supervisor.run([sys.executable, '-c', code, str(marker)],
                                        stall_seconds=0.5, startup_seconds=3,
                                        restart_delay=0, max_restarts=1)
            self.assertEqual(result, 0)
            self.assertEqual(marker.read_text(), 'recovered')
            self.assertIn('heartbeat stalled', (root / 'supervisor.log').read_text())

    def test_startup_hang_is_bounded(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(supervisor, 'DATA_DIR', Path(directory)):
                result = supervisor.run([sys.executable, '-c', 'import time; time.sleep(30)'],
                                        startup_seconds=0.5, restart_delay=0, max_restarts=0)
            self.assertEqual(result, 1)

    def test_crashed_child_is_restarted(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            marker = root / 'attempt'
            code = "from pathlib import Path; import sys; p=Path(sys.argv[1]); exists=p.exists(); p.write_text('ok'); sys.exit(0 if exists else 2)"
            with patch.object(supervisor, 'DATA_DIR', root):
                result = supervisor.run([sys.executable, '-c', code, str(marker)],
                                        restart_delay=0, max_restarts=1)
            self.assertEqual(result, 0)
            self.assertIn('worker exited code=2', (root / 'supervisor.log').read_text())


if __name__ == '__main__':
    unittest.main()
