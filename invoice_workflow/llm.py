"""Model calls with record and replay. Each request is keyed by a hash of exactly what is sent, so a
run with no API key replays the recorded answers, and a run with a key records new ones."""

import hashlib
import json
import logging
import os
import time
import uuid
from pathlib import Path

from openai import APIConnectionError, APIStatusError

from .prompts import PROMPT, TASK_PROMPTS, VP_TOOL


DEEPSEEK_URL = 'https://api.deepseek.com'
XAI_URL = 'https://api.x.ai/v1'


MODEL_ENV = {'vp': 'VP_MODEL', 'critic': 'CRITIC_MODEL'}


RECORDINGS = Path(__file__).resolve().parents[1] / 'evals' / 'recordings'


log = logging.getLogger(__name__)


def llm_model(*, offline=False, recordings=None, client=None, task='extract', usage=None):
    """Use Grok first, with DeepSeek for temporary provider failures when its key is configured.

    Explicit LLM_* settings select a provider. With no keys, offline replay uses the recorded DeepSeek
    answers. A model's hold, invalid answer or grounding failure never triggers fallback.
    """
    key = os.environ.get('LLM_API_KEY')
    base_url = os.environ.get('LLM_BASE_URL')
    if key and not base_url and not offline and client is None:
        raise ValueError('LLM_API_KEY requires LLM_BASE_URL; choose the endpoint issued for that key')
    if not base_url:
        base_url = XAI_URL if os.environ.get('XAI_API_KEY') else DEEPSEEK_URL
    is_grok = base_url.rstrip('/') == XAI_URL
    if not key:
        if is_grok:
            key = os.environ.get('XAI_API_KEY')
        elif base_url.rstrip('/') == DEEPSEEK_URL:
            key = os.environ.get('DEEPSEEK_API_KEY')
    default_model = 'grok-4.7' if is_grok else os.environ.get('DEEPSEEK_MODEL', 'deepseek-flash')
    model = os.environ.get(MODEL_ENV.get(task, 'LLM_MODEL'), os.environ.get('LLM_MODEL', default_model))
    primary = provider_model(
        model=model,
        base_url=base_url,
        api_key=key,
        offline=offline,
        recordings=recordings,
        client=client,
        task=task,
        usage=usage,
    )
    backup_key = os.environ.get('DEEPSEEK_API_KEY')
    if offline or not is_grok or not backup_key or client is not None:
        return primary
    backup = None

    def ask(text, issue):
        nonlocal backup
        try:
            return primary(text, issue)
        except (APIConnectionError, APIStatusError) as exc:
            if isinstance(exc, APIStatusError) and exc.status_code != 429 and exc.status_code < 500:
                raise
            log.warning('provider_fallback primary=%s backup=deepseek reason=%s', model, type(exc).__name__)
            if backup is None:
                backup = provider_model(
                    model=os.environ.get('DEEPSEEK_MODEL', 'deepseek-flash'),
                    base_url=DEEPSEEK_URL,
                    api_key=backup_key,
                    recordings=recordings,
                    task=task,
                    usage=usage,
                )
            return backup(text, issue)

    return ask


def provider_model(
    *, model, base_url, api_key, offline=False, recordings=None, client=None, task='extract', usage=None
):
    """Return the model for one task as a function: ask(text, issue) -> the model's JSON answer.

    Each request is keyed by a hash of exactly what is sent. Offline, the answer is replayed from
    `<recordings>/<key>.json`; a missing recording raises FileNotFoundError, so the invoice holds.
    Live, the answer is fetched and saved under the same key, so the next run can replay it."""
    directory = Path(recordings or RECORDINGS)
    if not offline and client is None:
        from openai import OpenAI

        if not api_key:
            raise ValueError('Set XAI_API_KEY, DEEPSEEK_API_KEY, or LLM_API_KEY with LLM_BASE_URL for live calls')
        is_grok = base_url.rstrip('/') == XAI_URL
        client = OpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=180 if is_grok else 60,
            max_retries=0 if is_grok else 1,
        )

    def ask(text, issue):
        payload = json.loads(text) if task == 'vp' else None
        if task in TASK_PROMPTS:
            prompt = TASK_PROMPTS[task]
        else:
            prompt = PROMPT + (' Correct the previous formatting problem: ' + issue if issue else '')
        user_text = json.dumps({'critique': payload.get('critique')}) if task == 'vp' else text
        messages = [{'role': 'system', 'content': prompt}, {'role': 'user', 'content': user_text}]
        settings = {'response_format': {'type': 'json_object'}, 'max_tokens': 4096, 'temperature': 0}
        if base_url.rstrip('/') == XAI_URL and model == 'grok-4.7':
            settings['reasoning_effort'] = 'low'
        if task in ('extract', 'vp') and base_url.rstrip('/') == DEEPSEEK_URL:
            settings['extra_body'] = {'thinking': {'type': 'disabled'}}
        identity = {'model': model, 'base_url': base_url, 'messages': messages, **settings}
        if task == 'vp':
            identity.update(tool=VP_TOOL, facts=payload['facts'])
        key = hashlib.sha256(json.dumps(identity, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        if offline:
            return replay(directory / f'{key}.json', task, usage)
        return call_live(client, task, model, messages, settings, payload, directory, key)

    return ask


def replay(path, task, usage):
    if not path.exists():
        raise FileNotFoundError('No recorded model response for this prompt')
    recording = json.loads(path.read_text())
    log.debug('model_replay task=%s recording=%s', task, path.name)
    if usage is not None:
        usage.append(recording)
    return recording['content']


def call_live(client, task, model, messages, settings, payload, directory, key):
    """One request, or two for the VP: a forced read-only fact lookup, then its answer. Saves the answer
    under `key`; VP and critic calls also keep an audit copy, including failures."""
    audit_path = directory / f'{key}-{uuid.uuid4().hex}.json'
    options = {'model': model, 'messages': messages, **settings}
    start, first_usage = time.monotonic(), None
    try:
        if task == 'vp':
            tool_choice = {'type': 'function', 'function': {'name': 'get_invoice_facts'}}
            first = client.chat.completions.create(**options, tools=[VP_TOOL], tool_choice=tool_choice)
            first_usage = first.usage
            messages = messages + fact_lookup(first.choices[0].message, payload['facts'])
        result = client.chat.completions.create(**{**options, 'messages': messages})
    except Exception as exc:
        log.warning(
            'model_call_failed task=%s model=%s error=%s latency_seconds=%.3f',
            task,
            model,
            type(exc).__name__,
            time.monotonic() - start,
        )
        directory.mkdir(parents=True, exist_ok=True)
        failure = {'messages': messages, 'model': model, 'task': task, 'error': type(exc).__name__}
        audit_path.write_text(json.dumps(failure, ensure_ascii=False))
        raise
    tokens = [u for u in (result.usage, first_usage) if u]
    recording = {
        'content': result.choices[0].message.content or '',
        'model': result.model,
        'reasoning_effort': settings.get('reasoning_effort'),
        'latency_seconds': time.monotonic() - start,
        'prompt_tokens': sum(u.prompt_tokens for u in tokens) if result.usage else None,
        'completion_tokens': sum(u.completion_tokens for u in tokens) if result.usage else None,
        'provider_requests': 2 if task == 'vp' else 1,
    }
    log.debug('model_call %s', json.dumps({k: v for k, v in recording.items() if k != 'content'}))
    directory.mkdir(parents=True, exist_ok=True)
    if task in ('vp', 'critic'):
        recording['messages'] = messages
        recording['request_settings'] = {
            'temperature': 0,
            'max_tokens': 4096,
            'extra_body': settings.get('extra_body'),
            'reasoning_effort': settings.get('reasoning_effort'),
            'tools': [VP_TOOL] if task == 'vp' else [],
        }
        audit_path.write_text(json.dumps(recording, ensure_ascii=False))
    (directory / f'{key}.json').write_text(json.dumps(recording, ensure_ascii=False))
    return recording['content']


def fact_lookup(message, facts):
    """The VP's one tool call, checked, and the facts code answers it with."""
    calls = message.tool_calls or []
    if (
        len(calls) != 1
        or calls[0].function.name != 'get_invoice_facts'
        or json.loads(calls[0].function.arguments) != {}
    ):
        raise ValueError('VP must request only validated facts')
    call = {
        'id': calls[0].id,
        'type': 'function',
        'function': {'name': 'get_invoice_facts', 'arguments': calls[0].function.arguments},
    }
    return [
        {'role': 'assistant', 'content': message.content, 'tool_calls': [call]},
        {'role': 'tool', 'tool_call_id': calls[0].id, 'content': json.dumps(facts, sort_keys=True)},
    ]
