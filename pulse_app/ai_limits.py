"""Validated inference budgets; generation length is separate from context length."""
import math


DEFAULTS = {'max_tokens': 8192, 'timeout_seconds': 300, 'max_context_chars': 240000}
BOUNDS = {'max_tokens': (256, 32768), 'timeout_seconds': (15, 900),
          'max_context_chars': (16000, 500000), 'max_steps': (2, 16)}


def validate(settings):
    for key, (low, high) in BOUNDS.items():
        if key in settings and (type(settings[key]) is not int or not low <= settings[key] <= high):
            raise ValueError(f'{key}: требуется целое число от {low} до {high}')
    if 'temperature' in settings:
        value = settings['temperature']
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= 2:
            raise ValueError('temperature: требуется число от 0 до 2')
    return settings


def limits(settings, *, agent=False):
    validate(settings)
    result = {key: settings.get(key, default) for key, default in DEFAULTS.items()}
    if agent:
        result['timeout_seconds'] = settings.get('timeout_seconds', 600)
    result['temperature'] = settings.get('temperature', 0.1)
    return result


def error_code(message):
    text = str(message).lower()
    if '401' in text or '403' in text: return 'access_denied'
    if '429' in text: return 'rate_limited'
    if 'length' in text or 'token' in text and 'limit' in text: return 'output_truncated'
    if 'timeout' in text or 'бюджет времени' in text or 'budget exceeded' in text: return 'timeout'
    if 'формат' in text or 'json' in text or 'unsupported' in text: return 'invalid_response'
    if 'reasoning' in text or 'think' in text: return 'reasoning_without_answer'
    if 'network' in text or 'сети' in text or 'обрыв' in text: return 'connection_failed'
    return 'analysis_failed'
