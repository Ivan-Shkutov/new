from pathlib import Path
import yaml
from pulse_app.connectors import RESOURCE_PATHS


def test_reader_role_covers_collector_without_write_or_secret_permissions():
    manifests = list(yaml.safe_load_all((Path(__file__).resolve().parents[1] / 'deploy/rbac-readonly.yaml').read_text()))
    rules = next(m['rules'] for m in manifests if m['kind'] == 'ClusterRole')
    for path in RESOURCE_PATHS.values():
        parts = path.strip('/').split('/')
        group = '' if parts[0] == 'api' else parts[1]
        resource = parts[-1]
        assert any(group in rule['apiGroups'] and resource in rule['resources'] and 'list' in rule['verbs'] for rule in rules), path
    for rule in rules:
        assert set(rule['verbs']) <= {'get', 'list', 'watch'}
        assert not set(rule['resources']) & {'secrets', 'pods/exec', '*'}
