#!/usr/bin/env python3
"""Summarise pacing energy runs from the GMT cluster database.

Groups the flow phases of one or more runs by pacing condition (the phase name
without its " rN" repetition suffix) and reports mean and standard deviation of
average power per condition, plus the mechanism counters the client printed.
All conditions have the same duration and move the same bytes, so a power
difference is an energy difference for identical work.

Usage:
  analyze.py RUN_ID [RUN_ID ...]
  analyze.py --uri https://github.com/green-coding-solutions/<repo> [--name 'Pacing%']

Needs psycopg (the Green Metrics Tool venv has it). Connection settings come
from PGHOST/PGPORT/PGUSER/PGPASSWORD/PGDATABASE.
"""
import argparse
import json
import re
import statistics
from collections import defaultdict

import psycopg

ENERGY = {
    'psu_energy_ac_mcp_machine': 'AC_W',
    'cpu_energy_rapl_msr_component': 'PKG_W',
    'memory_energy_rapl_msr_component': 'DRAM_W',
}
CUSTOM = {
    'custom_onperiod_kbps': 'onperiod_mbps',
    'custom_duty_permille': 'duty_pct',
    'custom_deep_cstate_permille': 'deep_c_pct',
    'custom_host_ctxt_per_s': 'ctxt_per_s',
    'custom_late_chunks': 'late',
}
COLUMNS = ['AC_W', 'PKG_W', 'DRAM_W', 'client_cpu_pct', 'server_cpu_pct',
           'onperiod_mbps', 'duty_pct', 'deep_c_pct', 'ctxt_per_s', 'net_rx_per_s', 'late']


def condition(phase):
    m = re.match(r'^\d+_(.*?)(?: r\d+)?$', phase)
    return m.group(1) if m else phase


def pace_key(cond):
    if cond.startswith('idle'):
        return (0, 0)
    if cond == 'unpaced':
        return (1, 0)
    m = re.search(r'(\d+)M', cond)
    return (2, -int(m.group(1)) if m else 0)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('run_ids', nargs='*')
    ap.add_argument('--uri')
    ap.add_argument('--name', default='%')
    args = ap.parse_args()

    with psycopg.connect(dbname='green-coding') as conn:
        cur = conn.cursor()
        if args.uri:
            cur.execute("SELECT id FROM runs WHERE uri = %s AND name LIKE %s AND failed = false "
                        "AND end_measurement IS NOT NULL ORDER BY created_at", (args.uri, args.name))
            args.run_ids += [str(r[0]) for r in cur.fetchall()]
        if not args.run_ids:
            raise SystemExit('no runs found')

        samples = defaultdict(lambda: defaultdict(list))
        for run_id in args.run_ids:
            cur.execute("SELECT machine_id, phases, logs FROM runs WHERE id = %s", (run_id,))
            machine_id, phases, logs = cur.fetchone()
            durations = {f"{i:03}_{p['name']}": (p['end'] - p['start']) / 1e6
                         for i, p in enumerate(phases)}
            per_phase = defaultdict(dict)
            cur.execute("SELECT metric, detail_name, phase, value FROM phase_stats "
                        "WHERE run_id = %s AND phase NOT LIKE '%%[%%'", (run_id,))
            for metric, detail, phase, value in cur.fetchall():
                row = per_phase[phase]
                if metric in ENERGY:
                    key = ENERGY[metric]
                    row[key] = row.get(key, 0) + value / 1e6 / durations[phase]
                elif metric == 'cpu_utilization_cgroup_container' and detail.split('-')[0] in ('client', 'server'):
                    row[f"{detail.split('-')[0]}_cpu_pct"] = value / 100
                elif metric in CUSTOM:
                    scale = 1e-3 if metric == 'custom_onperiod_kbps' else (0.1 if 'permille' in metric else 1)
                    row[CUSTOM[metric]] = value * scale

            summaries = {}
            for entries in (logs or {}).values():
                for entry in entries:
                    for line in (entry.get('stdout') or '').splitlines():
                        if line.startswith('PACER_SUMMARY '):
                            s = json.loads(line.split(' ', 1)[1])
                            summaries[s['label'].replace('_', ' ')] = s

            for phase, row in per_phase.items():
                name = phase.split('_', 1)[1]
                if name in summaries:
                    row['net_rx_per_s'] = summaries[name]['host_softirq_per_s'].get('net_rx', 0)
                for key, value in row.items():
                    samples[(machine_id, condition(phase))][key].append(value)

    for machine_id in sorted({m for m, _ in samples}):
        conds = sorted((c for m, c in samples if m == machine_id), key=pace_key)
        base = samples[(machine_id, 'unpaced')]
        print(f'\nmachine {machine_id}  (n = phases per condition, mean +- sd)')
        print(f"{'condition':<12}{'n':>3}" + ''.join(f'{c:>16}' for c in COLUMNS) + f"{'dAC_W vs unpaced':>18}")
        for cond in conds:
            vals = samples[(machine_id, cond)]
            cells = []
            for col in COLUMNS:
                v = vals.get(col, [])
                if not v:
                    cells.append(f"{'-':>16}")
                elif len(v) == 1:
                    cells.append(f'{v[0]:>16.2f}')
                else:
                    cells.append(f'{statistics.mean(v):>9.2f} +-{statistics.stdev(v):>5.2f}')
            delta = ''
            if base.get('AC_W') and vals.get('AC_W'):
                d = statistics.mean(vals['AC_W']) - statistics.mean(base['AC_W'])
                delta = f'{d:+.2f} ({d / statistics.mean(base["AC_W"]) * 100:+.1f}%)'
            print(f"{cond:<12}{len(vals.get('AC_W', vals.get('PKG_W', []))):>3}" + ''.join(cells) + f'{delta:>18}')


if __name__ == '__main__':
    main()
