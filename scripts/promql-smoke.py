#!/usr/bin/env python3
"""Evaluate Pulse's actual PromQL with promtool, without a live cluster.

Usage: python scripts/promql-smoke.py --promtool /path/to/promtool
Or:    python scripts/promql-smoke.py --docker-image prom/prometheus:<pinned-version>
The Docker image must already be present. No network access is used by the test.
"""
import argparse
from pathlib import Path
import subprocess
import sys
import tempfile

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pulse_app.connectors import utilization_expressions


def cases():
    tests = []
    for scoped in (True, False):
        settings = {'labels': {'cluster': 'one'}} if scoped else {'mode': 'dedicated'}
        series = []

        def add(metric, instance, values, **labels):
            # Two scrape jobs for the same exporter must not double its weight.
            for job in ('exporter', 'duplicate'):
                tags = {'cluster': 'one', 'instance': instance, 'job': job, **labels}
                label_text = ','.join(f'{k}="{v}"' for k, v in tags.items())
                series.append({'series': metric + '{' + label_text + '}', 'values': values})

        for instance, cores, idle, total, available, disk, disk_available in (
            ('node-a:9100', 1, 48, 100, 50, 100, 40),
            ('node-b:9100', 2, 12, 300, 60, 900, 270),
        ):
            for cpu in range(cores):
                add('node_cpu_seconds_total', instance, f'0+{idle}x6', cpu=str(cpu), mode='idle')
                add('node_cpu_seconds_total', instance, f'0+{60-idle}x6', cpu=str(cpu), mode='user')
            add('node_memory_MemTotal_bytes', instance, f'{total}+0x6')
            add('node_memory_MemAvailable_bytes', instance, f'{available}+0x6')
            add('node_filesystem_size_bytes', instance, f'{disk}+0x6', mountpoint='/', fstype='ext4')
            add('node_filesystem_avail_bytes', instance, f'{disk_available}+0x6', mountpoint='/', fstype='ext4')
        if scoped:
            # Foreign cluster data must never affect the selected cluster.
            for row in list(series):
                series.append({**row, 'series': row['series'].replace('cluster="one"', 'cluster="other"').replace('node-', 'foreign-')})
        expressions = []
        expected = {'cpu': (60, 20, 80), 'ram': (72.5, 50, 80), 'disk': (69, 60, 70)}
        for per_node in (False, True):
            for key, expr in utilization_expressions(settings, per_node).items():
                values = expected[key]
                samples = ([{'labels': '{instance="node-a:9100"}', 'value': values[1]},
                            {'labels': '{instance="node-b:9100"}', 'value': values[2]}]
                           if per_node else [{'labels': '{}', 'value': values[0]}])
                # promtool 3.1 compares floats exactly; tolerate only rounding
                # below the UI's 0.01% precision, without changing the queries.
                expressions.append({'expr': f'round(({expr}), 0.000001)', 'eval_time': '6m', 'exp_samples': samples})
                # Raw samples stopped 3m ago, but Prometheus still retains them
                # in its 5m lookback and rate windows. None may appear as current.
                expressions.append({'expr': expr, 'eval_time': '9m', 'exp_samples': []})
        tests.append({'name': 'shared' if scoped else 'dedicated', 'interval': '1m',
                      'input_series': series, 'promql_expr_test': expressions})
    return {'evaluation_interval': '1m', 'tests': tests}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--promtool')
    group.add_argument('--docker-image')
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='pulse-promql-') as directory:
        root = Path(directory)
        root.chmod(0o755)
        path = root / 'cases.yaml'
        path.write_text(yaml.safe_dump(cases(), sort_keys=False))
        path.chmod(0o644)
        if args.docker_image:
            command = ['docker', 'run', '--rm', '--pull=never', '--network=none',
                       '--read-only', '--tmpfs', '/tmp:rw,noexec,nosuid,size=128m',
                       '--cap-drop=ALL', '--security-opt=no-new-privileges',
                       '--mount', f'type=bind,src={root},dst=/tests,readonly',
                       '--entrypoint', '/bin/promtool', args.docker_image,
                       'test', 'rules', '/tests/cases.yaml']
        else:
            command = [args.promtool, 'test', 'rules', str(path)]
        subprocess.run(command, check=True, timeout=60)
    print('24 PromQL evaluations passed: weighting, duplicate scrapes, cluster scope, raw sample freshness.')


if __name__ == '__main__':
    main()
