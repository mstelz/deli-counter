"""Run with: python3 -m unittest discover -s tests -v."""
import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def load(name, file):
    loader = importlib.machinery.SourceFileLoader(name, str(ROOT / file))
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


inspect = load('test_inspect', 'deli-inspect')
scheduler = load('test_scheduler', 'deli-scheduler')


def record(at=None, memory=100):
    return dict(at=int(time.time()) if at is None else at, wall=1, cpu=1, peak_cores=1, peak_mem=memory, exit=0)


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = patch.object(inspect, 'STATE_DIR', self.tmp.name)
        self.state.start()
        self.addCleanup(self.state.stop)

    def test_limits_expiry_and_interrupted_writes(self):
        path = Path(inspect.store_path())
        orphan = Path(str(path) + '.999999')
        orphan.write_text('partial')
        with inspect.Store(write=True) as st:
            for n in range(1005):
                st.profiles[str(n)] = dict(cwd='/tmp', argv=[str(n)], runs=[record()] * 8)
            st.profiles['old'] = dict(cwd='/tmp', argv=['old'], runs=[record(at=1)])
        data = json.loads(path.read_text())
        self.assertEqual(len(data['profiles']), 1000)
        self.assertNotIn('old', data['profiles'])
        self.assertTrue(all(len(p['runs']) == 5 for p in data['profiles'].values()))
        self.assertFalse(orphan.exists())

    def test_failed_replace_keeps_old_data_and_removes_temp(self):
        with inspect.Store(write=True):
            pass
        path = Path(inspect.store_path())
        before = path.read_bytes()
        with patch.object(inspect.os, 'replace', side_effect=OSError('disk full')):
            with self.assertRaises(OSError), inspect.Store(write=True) as st:
                st.profiles['new'] = dict(cwd='/tmp', argv=['new'], runs=[record()])
        self.assertEqual(path.read_bytes(), before)
        self.assertFalse(Path(str(path) + f'.{os.getpid()}').exists())
        # A failed write must release the store lock.
        with inspect.Store(write=True):
            pass

    def test_save_failure_preserves_success_and_failure_codes(self):
        for rc in (0, 7):
            with self.subTest(rc=rc), patch.object(inspect, 'measure', return_value=(rc, record())), patch.object(inspect, 'Store', side_effect=OSError('disk full')), contextlib.redirect_stderr(io.StringIO()) as output:
                self.assertEqual(inspect.cmd_run(['--quiet', '--', 'anything']), rc)
                self.assertIn('disk full', output.getvalue())

    def test_sampling_failure_waits_and_preserves_exit_code(self):
        for sig in (signal.SIGINT, signal.SIGTERM):
            self.addCleanup(signal.signal, sig, signal.getsignal(sig))
        with patch.object(inspect.dc, 'read_procs', side_effect=OSError('reading unavailable')), contextlib.redirect_stderr(io.StringIO()):
            rc, result = inspect.measure([sys.executable, '-c', 'import time; time.sleep(.1); raise SystemExit(7)'])
        self.assertEqual(rc, 7)
        self.assertIsNone(result)

    def test_failed_run_does_not_create_profile(self):
        with patch.object(inspect, 'measure', return_value=(7, record())):
            self.assertEqual(inspect.cmd_run(['--quiet', '--', 'failure']), 7)
        with inspect.Store() as st:
            self.assertEqual(st.profiles, {})

    def test_storage_summary(self):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            inspect.cmd_storage([])
        self.assertIn('0/1000 profiles', out.getvalue())
        self.assertIn('bytes', out.getvalue())


class MemoryTests(unittest.TestCase):
    def test_reservations_prevent_combined_overcommit(self):
        reason = scheduler.memory_reason(500, [dict(pid=1, memory=500)], dict(mem_total=1000, mem_avail=1000), {})
        self.assertIn('reservations', reason)

    def test_external_pressure_and_unrealized_peaks(self):
        active = [dict(pid=1, memory=400)]
        self.assertIn('available', scheduler.memory_reason(200, active, dict(mem_total=1000, mem_avail=500), {1: 100}))
        self.assertEqual(scheduler.memory_reason(200, active, dict(mem_total=1000, mem_avail=500), {1: 400}), '')


class QueueTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.queue = self.root / 'queue'
        self.state = self.root / 'state'
        self.env = dict(os.environ, DELI_QUEUE_DIR=str(self.queue), DELI_STATE_DIR=str(self.state), DELI_QUEUE_POLL_SECONDS='.03', DELI_QUEUE_SLOTS='1', DELI_AUTO_MEASURE='off', DELI_SKIP_LIGHT='off', DELI_MEMORY_CHECK='off', DELI_MEMORY_FRACTION='.9', DELI_LIGHT_SLOTS='2')
        self.children = []
        self.addCleanup(self.stop_children)

    def stop_children(self):
        for child in self.children:
            if child.poll() is None:
                child.terminate()
        for child in self.children:
            try:
                child.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.communicate()

    def start(self, name, code, env=None, command=None):
        cwd = self.root / name
        cwd.mkdir(exist_ok=True)
        child = subprocess.Popen([str(ROOT / 'deli'), '--'] + (command or [sys.executable, '-c', code]), cwd=cwd, env=env or self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.children.append(child)
        return child

    def blocked(self, name, env=None, command=None):
        return self.start(name, "import time; from pathlib import Path\nwhile not Path('release').exists(): time.sleep(.02)", env, command)

    def release(self, name):
        (self.root / name / 'release').touch()

    def wait_for(self, predicate):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(.02)
        self.fail('condition did not become true')

    def jobs(self):
        result = []
        for p in self.queue.glob('job-*.json'):
            try:
                result.append(json.loads(p.read_text()))
            except (OSError, ValueError):
                pass
        return result

    def active(self):
        return [j for j in self.jobs() if j['active']]

    def finish(self, child, rc=0):
        out, err = child.communicate(timeout=10)
        self.assertEqual(child.returncode, rc, err)
        return out, err

    def test_fifo_and_crashed_waiter_cleanup(self):
        first = self.blocked('a')
        self.wait_for(lambda: len(self.active()) == 1)
        dead = self.start('dead', 'pass')
        self.wait_for(lambda: len(self.jobs()) == 2)
        second = self.blocked('b')
        self.wait_for(lambda: len(self.jobs()) == 3)
        third = self.start('c', 'pass')
        self.wait_for(lambda: len(self.jobs()) == 4)
        dead.kill()
        dead.communicate()
        self.wait_for(lambda: not (self.queue / f'job-{dead.pid}.json').exists())
        self.release('a')
        self.finish(first)
        self.wait_for(lambda: any(j['pid'] == second.pid for j in self.active()))
        self.assertIsNone(third.poll())
        self.release('b')
        self.finish(second)
        self.finish(third)
        self.assertEqual(self.jobs(), [])

    def test_checkout_serialization_and_cancelled_waiter(self):
        first = self.blocked('same')
        self.wait_for(lambda: len(self.active()) == 1)
        second = self.start('same', 'pass')
        self.wait_for(lambda: (self.queue / f'wait-{second.pid}.reason').exists())
        self.assertEqual(len(self.active()), 1)
        second.terminate()
        self.finish(second, 143)
        self.assertFalse((self.queue / f'wait-{second.pid}.info').exists())
        self.release('same')
        self.finish(first)

    def test_live_slot_increase_and_decrease(self):
        a = self.blocked('a')
        self.wait_for(lambda: len(self.active()) == 1)
        b = self.blocked('b')
        self.wait_for(lambda: len(self.jobs()) == 2)
        (self.queue / 'slots').write_text('2')
        self.wait_for(lambda: len(self.active()) == 2)
        (self.queue / 'slots').write_text('1')
        c = self.start('c', 'pass')
        self.wait_for(lambda: len(self.jobs()) == 3)
        self.release('a')
        self.finish(a)
        time.sleep(.15)  # several admission polls with slot 2 still occupied
        self.assertIsNone(c.poll())
        self.release('b')
        self.finish(b)
        self.finish(c)

    def test_running_cancel_releases_slot_and_preserves_exit(self):
        a = self.start('a', 'import time; time.sleep(10)')
        self.wait_for(lambda: len(self.active()) == 1)
        a.terminate()
        self.finish(a, 143)
        b = self.start('b', 'raise SystemExit(7)')
        self.finish(b, 7)
        self.assertEqual(self.jobs(), [])

    def test_impossible_memory_estimate_is_rejected(self):
        env = dict(self.env, DELI_MEMORY_CHECK='on', DELI_MEMORY_FRACTION='.1')
        child = self.start('a', "from pathlib import Path; Path('ran').touch()", env)
        _, err = self.finish(child, 75)
        self.assertIn('exceeds the admission budget', err)
        self.assertFalse((self.root / 'a' / 'ran').exists())
        self.assertEqual(self.jobs(), [])

    def test_measurement_save_failure_through_cli(self):
        bad_state = self.root / 'not-a-directory'
        bad_state.write_text('occupied')
        env = dict(self.env, DELI_STATE_DIR=str(bad_state), DELI_AUTO_MEASURE='on')
        for rc in (0, 7):
            with self.subTest(rc=rc):
                child = self.start('a', f'raise SystemExit({rc})', env)
                _, err = self.finish(child, rc)
                self.assertIn('measurement could not be saved', err)

    def test_concurrent_measurements_keep_all_profiles(self):
        env = dict(self.env, DELI_AUTO_MEASURE='on', DELI_QUEUE_SLOTS='4')
        children = [self.start(str(n), 'import time; time.sleep(.2)', env) for n in range(4)]
        for child in children:
            self.finish(child)
        with patch.object(inspect, 'STATE_DIR', str(self.state)), inspect.Store() as st:
            self.assertEqual(len(st.profiles), 4)

    def test_memory_waits_even_when_slots_are_free(self):
        # Controlled machine readings exercise the full admission loop without depending
        # on the host's current RAM usage. No real memory-intensive jobs are launched.
        runner = self.root / 'memory-runner.py'
        runner.write_text(f"""
import importlib.machinery, importlib.util, sys
loader = importlib.machinery.SourceFileLoader('scheduler', {str(ROOT / 'deli-scheduler')!r})
spec = importlib.util.spec_from_loader(loader.name, loader)
m = importlib.util.module_from_spec(spec)
loader.exec_module(m)
original = m.load_inspect
def fake_machine():
    inspector = original()
    inspector.dc.machine = lambda: dict(mem_total=1000, mem_avail=1000)
    inspector.dc.read_procs = lambda: {{}}
    return inspector
m.load_inspect = fake_machine
sys.exit(m.Scheduler('run', '', 'off', sys.argv[1:]).run())
""")
        env = dict(self.env, DELI_MEMORY_CHECK='on', DELI_QUEUE_SLOTS='4')
        children = []
        for n in range(4):
            cwd = self.root / str(n)
            cwd.mkdir()
            child = subprocess.Popen([sys.executable, str(runner), sys.executable, '-c', "import time; from pathlib import Path\nwhile not Path('release').exists(): time.sleep(.02)"], cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            self.children.append(child)
            children.append(child)
            self.wait_for(lambda: len(self.jobs()) == n + 1)
            if n < 3:
                self.wait_for(lambda: len(self.active()) == n + 1)
        reason = self.queue / f'wait-{children[3].pid}.reason'
        self.wait_for(lambda: reason.exists() and 'memory reservations' in reason.read_text())
        self.assertEqual(len(self.active()), 3)
        self.release('0')
        self.finish(children[0])
        self.wait_for(lambda: any(j['pid'] == children[3].pid for j in self.active()))
        for n in range(1, 4):
            self.release(str(n))
            self.finish(children[n])

    def test_light_runs_have_shared_allowance(self):
        env = dict(self.env, DELI_SKIP_LIGHT='on', DELI_LIGHT_SLOTS='1')
        command = [sys.executable, '-c', "import time; from pathlib import Path\nwhile not Path('release').exists(): time.sleep(.02)"]
        with patch.object(inspect, 'STATE_DIR', str(self.state)), inspect.Store(write=True) as st:
            for name in ('a', 'b'):
                cwd = self.root / name
                cwd.mkdir()
                st.profiles[inspect.key_of(str(cwd), command)] = dict(cwd=str(cwd), argv=command, runs=[record()])
        a = self.start('a', '', env=env, command=command)
        self.wait_for(lambda: len(self.active()) == 1)
        b = self.start('b', '', env=env, command=command)
        self.wait_for(lambda: len(self.jobs()) == 2)
        self.assertEqual(self.active()[0]['lane'], 'light')
        self.assertFalse(list(self.queue.glob('slot-*.info')))
        self.wait_for(lambda: (self.queue / f'wait-{b.pid}.reason').exists())
        with patch.object(inspect.dc, 'QUEUE_DIR', str(self.queue)), patch.object(inspect.dc, 'SLOTS_FILE', str(self.queue / 'slots')):
            snap = inspect.dc.Sampler().sample()
            self.assertEqual(len(snap.light), 1)
            self.assertTrue(snap.waiting[0].reason)
            rendered = '\n'.join(''.join(text for text, style in row) for row in inspect.dc.top_rows(snap))
            self.assertIn('LIGHT RUNS', rendered)
            self.assertIn(snap.waiting[0].reason, rendered)
        self.release('a')
        self.finish(a)
        self.release('b')
        _, err = self.finish(b)
        self.assertIn('light allowance', err)


if __name__ == '__main__':
    unittest.main()
