"""Scenario contracts: grounded facts, bounded model output and conservative recovery."""
from copy import deepcopy
import json
import re

import pytest

from pulse_app import workflows as w


def incident():
    return {
        'started_at': '2026-11-02T13:00:00Z', 'finished_at': '2026-11-02T13:00:02Z',
        'target': {'namespace': 'am-agent', 'name': 'agent-old', 'uid': 'old-uid'},
        'pod': {'phase': 'Failed', 'reason': 'Evicted', 'uid': 'old-uid', 'source_type': 'live',
                'evidence_id': 'E01', 'message': 'Usage of EmptyDir volume "data" exceeds the limit "1Gi".'},
        'records': [{'id': 'E01', 'group': 'pod', 'status': 'ok', 'complete': True}],
        'findings': [{'source': 'E01', 'fact': 'Pod phase=Failed; reason=Evicted'}],
        'owners': [], 'owners_complete': False,
    }


def recovered():
    report = incident()
    report['records'] += [dict(id=eid, status='ok', complete=True)
                          for eid in ('E02', 'E03', 'E04', 'E05', 'E06')]
    report['owners'] = [
        {'kind': 'ReplicaSet', 'name': 'agent-rs', 'uid': 'rs-uid', 'spec': {'replicas': 0},
         'status': {'readyReplicas': 0}, 'generation': 1, 'observedGeneration': 1,
         'source_type': 'live', 'evidence_id': 'E02'},
        {'kind': 'Deployment', 'name': 'agent', 'uid': 'deployment-uid', 'spec': {'replicas': 1},
         'status': {'readyReplicas': 1, 'availableReplicas': 1, 'updatedReplicas': 1},
         'generation': 2, 'observedGeneration': 2, 'source_type': 'live', 'evidence_id': 'E03'},
    ]
    report['owners_complete'] = True
    report['replacements'] = {
        'complete': True, 'truncated': False, 'evidence_ids': ['E04'],
        'owner_uids': ['rs-uid', 'new-rs-uid', 'deployment-uid'],
        'items': [{'name': 'agent-new', 'uid': 'new-uid', 'phase': 'Running',
                   'conditions': [{'type': 'Ready', 'status': 'True'}], 'ready': True, 'deleting': False,
                   'owner_uids': ['new-rs-uid'], 'source_type': 'live', 'ip': '10.0.0.10',
                   'container_statuses': [{'name': 'agent', 'ready': True}]}],
    }
    report['services'] = {'items': [{'name': 'agent', 'uid': 'service-uid', 'evidence_id': 'E05'}],
                          'complete': True, 'evidence_id': 'E05'}
    report['endpoints'] = [{'service': 'agent', 'complete': True, 'evidence_id': 'E06',
                           'addresses': [{'ip': '10.0.0.10', 'target_uid': 'new-uid', 'ready': True}]}]
    return report


def model(hypotheses=None, checks=None):
    return json.dumps({'hypotheses': hypotheses or [], 'checks': checks or []}, ensure_ascii=False)


def suggestion():
    return {'title': 'Проверьте очистку', 'detail': 'Сопоставьте назначение файлов с правилами ротации.',
            'expected': 'Установлен источник роста временных файлов.', 'evidence_ids': ['E01']}


def main_words(result):
    text = result['summary'] + ' ' + ' '.join(row['label'] + ' ' + row['value'] for row in result['rows'])
    return len(text.split())


def test_catalog_has_six_trusted_scenarios_and_returns_copies():
    items = w.catalog()['items']
    assert [item['id'] for item in items] == ['brief', 'deep', 'causes', 'impact', 'plan', 'recovery']
    assert [item['title'] for item in items] == [
        'Краткий разбор', 'Подробный разбор', 'Причины сбоя', 'Последствия сбоя',
        'План устранения', 'Проверка восстановления']
    assert all(item['mode'] and item['question'] and item['description'] for item in items)
    items[0]['question'] = 'untrusted replacement'
    assert 'untrusted' not in w.scenario('brief')['question']
    with pytest.raises(ValueError):
        w.scenario('free-chat')
    assert 'ТОЛЬКО JSON' in w.model_prompt('brief')


def test_brief_is_small_and_exact_without_severity_or_node_disk_guess():
    result = w.build_result('brief', incident())
    assert result['status'] == 'complete' and result['origin'] == 'pulse'
    assert len(result['rows']) == 4 and main_words(result) <= 120
    assert 'EmptyDir data' in result['rows'][0]['value'] and '1Gi' in result['rows'][0]['value']
    assert result['rows'][0]['evidence_ids'] == ['E01']
    assert 'Не определена' in result['rows'][2]['value']
    assert 'заполнен диск' not in str(result)
    assert result['observed_at'] == incident()['finished_at']


def test_plan_previous_logs_targets_the_container_with_the_observed_failure():
    report = incident()
    report['pod'].update(phase='Running',reason='',message='',container_statuses=[
        {'name':'healthy-sidecar','state':{'running':{}}},
        {'name':'failing-app','state':{'waiting':{'reason':'CrashLoopBackOff'}}}])
    result = w.build_result('plan',report)
    commands = [command['command'] for step in result['steps'] for command in step['commands']]
    assert any('-c failing-app --previous' in command for command in commands)
    assert not any('-c healthy-sidecar' in command for command in commands)


@pytest.mark.parametrize('previous', [False, True])
def test_oom_plan_reads_the_terminated_container_run_without_claiming_a_limit_breach(previous):
    report = incident()
    report['pod'].update(phase='Running' if previous else 'Failed', reason='', message='',
                        container_statuses=[{'name': 'sidecar', 'state': {'running': {}}},
                            {'name': 'api', 'lastState' if previous else 'state':
                                {'terminated': {'reason': 'OOMKilled', 'exitCode': 137}},
                             'restartCount': 3 if previous else 0}])
    result = w.build_result('plan', report)
    logs = [c for step in result['steps'] for c in step['commands'] if ' logs ' in c['command']]
    assert len(logs) == 1
    assert '-c api' in logs[0]['command']
    assert (' --previous' in logs[0]['command']) is previous
    assert 'не установлены' in result['rows'][0]['value']
    assert 'превышен лимит памяти' not in result['rows'][0]['value']


@pytest.mark.parametrize('detail', [
    'Проверьте доступ, временно добавив правило для UDP/53.',
    'Временно измените NetworkPolicy и сопоставьте результат.',
    'Необходимо перезапустить Pod для проверки гипотезы.',
    'Увеличить лимит памяти до 1Gi.',
    'Disable TLS and restart the service.',
    'Disable TLS verification temporarily.',
    'Временно разрешить DNS в политике.',
    'helm upgrade deployment',
    'python3 -c "print(1)"',
])
def test_model_readonly_checks_reject_mutation_in_plain_language(detail):
    answer = model(checks=[{**suggestion(), 'detail': detail}])
    result = w.build_result('plan', incident(), answer)
    assert result['model']['status'] == 'invalid'
    assert result['status'] == 'partial'
    assert detail not in str(result)
    assert 'EmptyDir' in result['rows'][0]['value']


def test_legitimate_establishing_a_cause_and_reviewing_changes_is_not_installation():
    answer = model(checks=[{**suggestion(),
        'detail': 'Установить источник роста по истории изменений и правилам ротации.'}])
    assert w.build_result('causes', incident(), answer)['model']['status'] == 'complete'


def test_plan_does_not_invent_service_commands_without_observed_service_evidence():
    report = recovered()
    report['services']['items'].append({'name': 'guessed-service'})
    result = w.build_result('plan', report)
    commands = [c['command'] for step in result['steps'] for c in step['commands']]
    assert any(' endpoints agent ' in command for command in commands)
    assert not any('guessed-service' in command for command in commands)


@pytest.mark.parametrize('answer', [
    'Произошла нехватка памяти на узле.',
    '{"hypotheses":[],"checks":[],"summary":"invented"}',
    '{"hypotheses":[],"checks":[],"checks":[]}',
    model([{'text': 'Возможно, нарушена очистка.', 'evidence_ids': ['E99']}]),
    model([{'text': 'Возможно, нарушена очистка.', 'evidence_ids': []}]),
    model([{'text': 'x' * 501, 'evidence_ids': ['E01']}]),
    model([{'text': 'kubectl delete pod agent-old', 'evidence_ids': ['E01']}]),
    model(checks=[{**suggestion(), 'command': 'delete pod'}]),
    model([{'text': 'Причина сбоя — заполнение диска.', 'evidence_ids': ['E01']}]),
    model([{'text': 'Возможно, восстановление подтверждено.', 'evidence_ids': ['E01']}]),
])
def test_invalid_model_never_displaces_canonical_facts(answer):
    result = w.build_result('causes', incident(), answer)
    assert result['model']['status'] == 'invalid'
    assert result['status'] == 'partial'
    assert result['origin'] == 'pulse'
    assert 'EmptyDir data' in result['rows'][0]['value']
    assert 'invented' not in str(result)


def test_valid_json_is_only_hypotheses_and_proposed_checks():
    answer = model([{'text': 'Возможно, приложение не очищает временные файлы.', 'evidence_ids': ['E01']}],
                   [suggestion()])
    result = w.build_result('causes', incident(), '```json\n' + answer + '\n```')
    assert result['model']['status'] == 'complete' and result['origin'] == 'model'
    assert 'EmptyDir data' in result['rows'][0]['value']
    assert result['sections'][-1]['title'] == 'Гипотезы модели — требуют проверки'
    assert result['sections'][-1]['items'][0].startswith('Гипотеза: Возможно')
    assert result['steps'][0]['commands'] == []
    assert result['steps'][0]['evidence_ids'] == ['E01']
    brief = w.build_result('brief', incident(), answer)
    assert main_words(brief) <= 120 and len(brief['rows']) == 4


def test_unavailable_evidence_cannot_support_hypothesis_but_can_be_next_check():
    report = incident()
    report['records'].append({'id': 'E02', 'status': 'unavailable'})
    with pytest.raises(ValueError):
        w.parse_model_answer(model([{'text': 'Возможно, логи указывают на ошибку.', 'evidence_ids': ['E02']}]), report)
    check = {**suggestion(), 'evidence_ids': ['E02']}
    assert w.parse_model_answer(model(checks=[check]), report)['checks'][0]['evidence_ids'] == ['E02']


def test_cached_observation_and_model_error_stay_explicit_and_redacted():
    report = incident()
    report['pod']['source_type'] = 'cached'
    result = w.build_result('brief', report, model_error='HTTP 403 Authorization: Bearer super-private')
    assert result['status'] == 'partial' and 'сохранённом' in result['summary']
    assert 'super-private' not in str(result)
    assert result['model']['status'] == 'unavailable'
    assert any('сохранённому' in item for item in result['limitations'])


def test_recovery_uses_fresh_replacement_not_historical_failed_pod():
    report = recovered()
    result = w.build_result('recovery', report, model_answer='I guarantee success')
    assert report['pod']['phase'] == 'Failed'
    assert result['recovery']['state'] == 'verified'
    assert all(check['state'] == 'verified' and check['evidence_ids'] for check in result['recovery']['checks'])
    assert result['model']['status'] == 'not_used'
    assert 'пользовательских запросов' in ' '.join(result['limitations'])


@pytest.mark.parametrize('mutation', [
    lambda r: r.update(owners_complete=False),
    lambda r: r['owners'][-1].update(source_type='cached'),
    lambda r: r['owners'][-1].update(observedGeneration=1),
    lambda r: r['owners'][-1].update(generation=None),
    lambda r: r['owners'][-1]['spec'].update(replicas=0),
    lambda r: r['replacements'].update(complete=False),
    lambda r: r['replacements'].update(truncated=True),
    lambda r: r['replacements']['items'][0].update(owner_uids=['foreign-uid']),
    lambda r: r['replacements']['items'][0].update(source_type='cached'),
    lambda r: r['replacements']['items'].append(deepcopy(r['replacements']['items'][0])),
    lambda r: r['records'][3].update(status='partial', complete=False),
    lambda r: r['services'].update(complete=False),
    lambda r: r['services'].update(items=[]),
    lambda r: r['services']['items'][0].update(publish_not_ready=True),
    lambda r: r['endpoints'][0].update(complete=False),
    lambda r: r['endpoints'][0]['addresses'][0].update(target_uid='foreign-pod'),
    lambda r: r.update(identity_changed=True),
])
def test_incomplete_or_foreign_recovery_evidence_never_turns_green(mutation):
    report = recovered()
    mutation(report)
    assert w.recovery_assessment(report)['state'] != 'verified'


@pytest.mark.parametrize('mutation', [
    lambda r: r['owners'][-1]['status'].update(readyReplicas=0, availableReplicas=0),
    lambda r: r['owners'][-1]['status'].update(updatedReplicas=0),
    lambda r: r['replacements']['items'][0].update(ready=False),
    lambda r: r['replacements']['items'][0].update(container_statuses=[]),
    lambda r: r['replacements']['items'][0].update(deleting=True),
    lambda r: r['endpoints'][0].update(addresses=[]),
])
def test_fresh_negative_readiness_is_not_recovered(mutation):
    report = recovered()
    mutation(report)
    assert w.recovery_assessment(report)['state'] == 'not_recovered'


def test_plan_uses_actual_names_and_only_read_commands_with_conditional_fix():
    result = w.build_result('plan', recovered())
    commands = [command['command'] for step in result['steps'] for command in step['commands']]
    assert commands == ['kubectl -n am-agent get pod agent-old -o json',
                        'kubectl -n am-agent get deployment agent -o json',
                        'kubectl -n am-agent get endpoints agent -o json']
    assert all(' get ' in command for command in commands)
    assert 'Если подтверждено' in result['steps'][1]['detail']
    assert 'верните согласованную конфигурацию' in result['steps'][2]['detail']
    report = recovered()
    report['target']['namespace'] = 'default; delete all'
    assert not any(step['commands'] for step in w.build_result('plan', report)['steps'])


def test_no_pod_report_cannot_confirm_cause_or_recovery_even_with_model_prose():
    result = w.build_result('deep', {}, model([{'text': 'Возможно, нужна проверка истории ошибок.', 'evidence_ids': []}]))
    assert result['status'] == 'unconfirmed' and result['recovery']['state'] == 'unconfirmed'
    assert any('причина не установлена' in row['value'] for row in result['rows'])
    assert result['model']['status'] == 'complete'
    assert all(check['state'] == 'unconfirmed' for check in result['recovery']['checks'])


def test_two_services_require_each_relevant_endpoint_to_be_ready():
    report = recovered()
    report['services']['items'].append({'name': 'agent-extra'})
    assert w.recovery_assessment(report)['state'] == 'unconfirmed'
    report['endpoints'].append({'service': 'agent-extra', 'complete': True,
                                'evidence_id': 'E06', 'addresses': []})
    assert w.recovery_assessment(report)['state'] == 'not_recovered'


def test_endpoint_ip_without_uid_does_not_prove_current_pod_identity():
    report = recovered()
    report['endpoints'][0]['addresses'][0]['target_uid'] = None
    assert w.recovery_assessment(report)['state'] == 'unconfirmed'


def test_technical_urgency_only_uses_confirmed_readiness_facts():
    report = recovered()
    ready = w.build_result('brief', report)
    assert ready['rows'][2]['value'].startswith('Низкая по текущей готовности')
    assert main_words(ready) <= 120
    report['endpoints'][0]['addresses'] = []
    unavailable = w.build_result('brief', report)
    assert unavailable['rows'][2]['value'].startswith('Высокая для готовности сервиса')
    assert unavailable['rows'][2]['evidence_ids'] == ['E05', 'E06']
    report['endpoints'][0]['complete'] = False
    report['owners'][-1]['status'].update(readyReplicas=0, availableReplicas=0)
    assert w.build_result('brief', report)['rows'][2]['value'].startswith('Средняя по готовности')


@pytest.mark.parametrize(('container_status', 'code', 'phrase'), [
    ({'state': {'terminated': {'reason': 'OOMKilled'}}}, 'oom', 'OOMKilled'),
    ({'state': {'running': {}}, 'lastState': {'terminated': {'reason': 'OOMKilled'}}}, 'oom', 'Последнее завершение'),
    ({'state': {'waiting': {'reason': 'ImagePullBackOff'}}}, 'image', 'ImagePullBackOff'),
    ({'state': {'waiting': {'reason': 'CrashLoopBackOff'}}}, 'crash', 'CrashLoopBackOff'),
])
def test_direct_container_signals_get_specific_checks_without_invented_root_cause(container_status, code, phrase):
    report = incident()
    report['pod'].update(reason=None, message='', phase='Pending', container_statuses=[{'name': 'agent', **container_status}])
    signal = w._cause_signal(report, ['E01'])
    assert signal['code'] == code and phrase in signal['detail']
    result = w.build_result('brief', report)
    assert phrase in result['rows'][0]['value']
    assert main_words(result) <= 120
    plan = w.build_result('plan', report)
    assert plan['steps'][1]['detail'] == signal['fix']
    assert all(re.search(r'\b(get|describe|logs)\b', command['command'])
               for step in plan['steps'] for command in step['commands'])


def test_only_event_from_selected_uid_can_support_scheduling_or_probe_signal():
    report = incident()
    report['pod'].update(reason=None, message='', phase='Pending')
    event = {'involvedObject': {'uid': 'foreign'}, 'reason': 'FailedScheduling', 'message': 'Insufficient cpu'}
    report['records'].append({'id': 'E02', 'group': 'events', 'status': 'ok', 'data': {'items': [event]}})
    assert w._cause_signal(report, ['E01']) is None
    event['involvedObject']['uid'] = 'old-uid'
    assert w._cause_signal(report, ['E01'])['code'] == 'scheduling'
    event.update(reason='Unhealthy', message='Readiness probe failed: HTTP 503')
    assert w._cause_signal(report, ['E01'])['code'] == 'probe'
    report['records'][-1]['status'] = 'unavailable'
    assert w._cause_signal(report, ['E01']) is None


def test_model_changes_brief_first_check_but_not_cause_or_severity():
    result = w.build_result('brief', incident(), model(checks=[suggestion()]))
    assert result['origin'] == 'model' and result['model']['status'] == 'complete'
    assert result['rows'][-1]['value'].startswith('Предлагается: Проверьте очистку')
    assert 'EmptyDir data' in result['rows'][0]['value']
    assert main_words(result) <= 120
    ignored = w.build_result('brief', incident(), model([{'text': 'Возможно, нужна очистка.', 'evidence_ids': ['E01']}]))
    assert ignored['origin'] == 'pulse'  # No model suggestion was displayed.


def test_live_resource_and_metrics_are_useful_but_never_claim_application_recovery():
    report = {'records': [{'id': 'E01', 'status': 'ok', 'complete': True}],
              'resource': {'kind': 'nodes', 'name': 'worker-2', 'uid': 'node-uid',
                           'source_type': 'live', 'evidence_id': 'E01', 'state': 'not_ready',
                           'conditions': [{'type': 'Ready', 'status': 'False'}, {'type': 'DiskPressure', 'status': 'True'}]}}
    result = w.build_result('brief', report)
    assert result['status'] == 'complete' and 'nodes/worker-2' in result['summary']
    assert 'DiskPressure=True' in result['rows'][0]['value']
    assert 'Pod' not in result['summary']
    assert w.build_result('recovery', report)['recovery']['state'] == 'unconfirmed'
    plan = w.build_result('plan', report)
    assert plan['steps'][0]['commands'][0]['command'] == 'kubectl get nodes worker-2 -o json'
    metric_report = {'records': [{'id': 'E01', 'status': 'ok', 'complete': True}],
                     'metrics': {'metric': 'cpu', 'percent': 14.5, 'source_type': 'live',
                                 'complete': True, 'evidence_ids': ['E01']}}
    measured = w.build_result('brief', metric_report)
    assert measured['status'] == 'complete' and '14.5%' in measured['summary']
    assert 'перегрузка' not in measured['summary']
    assert measured['recovery']['state'] == 'unconfirmed'


def test_snapshot_context_remains_explicitly_unconfirmed_and_does_not_use_pod_fallback():
    context = {'kind': 'nodes', 'name': 'worker-2', 'title': 'Узел не готов', 'detail': 'Ready=False в снимке'}
    report = {'snapshot': {'observed_at': '2026-11-02T12:00:00Z'}, 'target': context}
    result = w.build_result('brief', report, context=context)
    assert result['status'] == 'unconfirmed' and 'nodes/worker-2' in result['summary']
    assert 'снимка' in result['summary']
    assert 'выбранного Pod' not in str(result)
    assert main_words(result) <= 120
