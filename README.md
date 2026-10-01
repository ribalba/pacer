# Pacing energy sweep

Does application-informed pacing (Sammy, SIGCOMM '23; Yu et al., IMC '26) change
the energy needed to deliver a video stream? Both papers show that pacing
reduces loss and queueing delay without hurting QoE, but neither measures
energy. Pacing works against race to idle: the same bytes are delivered over a
longer on-period, so CPUs wake up more often and spend less time in deep idle
states.

## Design

Two containers on one machine: `server.py` (HTTP/1.1, `sendfile()`) and
`client.py` (a steady-state player that fetches one 4 s chunk of a 16 Mbit/s
stream every 4 s). The client asks for a pace rate with an `X-Pace-Rate` header
and the server applies it with `SO_MAX_PACING_RATE`, as Sammy proposes. Since
Linux 4.13 the kernel enforces this through TCP internal pacing, so neither the
fq qdisc nor `NET_ADMIN` is needed.

Every phase lasts 60 s and moves the same 120 MB. Only the on-period throughput
changes, so any power difference between phases is an energy difference for
identical work.

| Condition | Why |
| --- | --- |
| `unpaced` | Pure race to idle (about 20 Gbit/s over the docker bridge) |
| `pace 1000M` | Roughly BBR pacing at a 1 Gbit/s access port, today's "unpaced" in the wild |
| `pace 100M` | 100 Mbit/s port |
| `pace 48M` | Sammy: about 3x the top bitrate (paper uses 2.8x to 3.2x) |
| `pace 30M` | Fixed Netflix rate in the IMC '26 experiment |
| `pace 20M` | 1.25x bitrate, close to fully smooth (80 % duty cycle) |
| `idle start/end` | No traffic, same containers, reference floor |

Conditions run ascending, descending, ascending (3 repetitions per run) to
cancel thermal drift. One run takes about 25 minutes including GMT overhead.

Besides GMT's energy metrics (AC at the plug, RAPL package and DRAM,
per-container CPU), the client prints one `PACER_SUMMARY` JSON line per phase
with host-wide context switches, softirqs and C-state residency, and five GMT
custom metrics (on-period throughput, duty cycle, context switches per second,
deep C-state share, late chunks). Late chunks must stay at 0, otherwise the
pace rate starved playback.

## What this does and does not capture

Captured: the host-side cost of pacing on both ends, meaning hrtimer and softirq
wakeups, smaller TSO bursts, more receive wakeups in the application, and lost
deep C-state residency, with DVFS and Turbo either on (machine 7) or off
(machine 14).

Not captured, so a null result here does not mean pacing is energy neutral:

- No physical NIC: no interrupt coalescing, no Energy Efficient Ethernet low
  power idle between bursts.
- No radio: Wi-Fi and LTE/5G tail energy is likely the largest client-side
  effect on mobile devices and needs a phone or Wi-Fi testbed.
- No bottleneck queue, so the loss and retransmission savings that pacing
  brings in the field (and the energy they save) do not appear.
- No TLS, which adds per-byte CPU work.
- Client and server share one machine. Their split comes only from GMT's
  per-container CPU attribution.
- The Python client pays more per receive wakeup than a native player.

## Running

Locally (without GMT):

```bash
docker network create pacer
docker run -d --rm --name server --net pacer -v $PWD:/tmp/repo:ro python:3.12-slim python3 /tmp/repo/server.py
docker run --rm --net pacer -v $PWD:/tmp/repo:ro python:3.12-slim python3 /tmp/repo/client.py --label test --pace-mbps 30 --duration 12
```

On the GMT cluster the files must sit at the repository root, because GMT
mounts the repository at `/tmp/repo` and the scenario uses those paths.

```bash
/home/didi/code/green-metrics-tool/venv/bin/python \
  /home/didi/code/gmt-helpers/api/submit_software.py submit \
  --name "Pacing energy sweep - machine 7" \
  --repo-url "https://github.com/ribalba/pacer" \
  --branch main --filename usage_scenario.yml \
  --machine-id 7 --schedule-mode variance --email pacer@ribalba.de
```

Then:

```bash
PGHOST=... PGPORT=... PGUSER=... PGPASSWORD=... \
  /home/didi/code/green-metrics-tool/venv/bin/python analyze.py \
  --uri https://github.com/ribalba/pacer --name 'Pacing%'
```
