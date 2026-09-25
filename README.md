# queue-dash

A live terminal dashboard for the `run-queued` work queue (`~/.local/bin/run-queued`).

```bash
queue-dash            # full-screen, refreshes every second
queue-dash -n 0.5     # faster refresh
queue-dash --once     # print one snapshot (also used when output is piped)
```

Keys: `q`/`Esc` quit · `r` refresh now · `+`/`-` double or halve the refresh interval.

## What it shows

- **Header**: slots busy, runs waiting, load average against CPU count, available memory,
  swap. Values turn yellow or red as the machine gets tight.
- **Slots**: each running job's label, how long it has run, how long it queued first, CPU% and
  RSS summed over its whole process tree, process count, and the command.
- **Waiting**: queued runs oldest first, and whether each waits for a machine-wide slot or for
  another run in its own checkout (and which slot holds that checkout).
- **Finished**: runs that ended while the dashboard was open, with run and queue times.
  `run-queued` records no history or exit codes, so this list starts empty each launch.

## How it works

It reads only the state files `run-queued` writes in `$AGENT_QUEUE_DIR` (default
`/tmp/agent-queue`) plus `/proc`; it never takes the queue's locks. A killed run can leave a
stale info file, so an entry counts only while its pid is alive and is still a `run-queued`
process. It respects `AGENT_QUEUE_DIR` and `AGENT_QUEUE_SLOTS`, and also detects the slot count
from the `slot-N.lock` files. Python 3 standard library only (curses), no install step.

Installed as a symlink: `ln -s ~/Development/queue-dash/queue-dash ~/.local/bin/queue-dash`.
