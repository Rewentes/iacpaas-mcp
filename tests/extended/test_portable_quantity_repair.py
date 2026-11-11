"""Old history quantities cannot bypass current evidence during answer correction."""

import asyncio
import json
from decimal import Decimal

import pytest
from test_portable_authored_conversation import accepted_review, evidence, gas_session

from standalone_agent.answers import authored_answer, numbers, parse_answer
from standalone_agent.conversation import ConversationAgent
from standalone_agent.review_options import sampling_options


@pytest.mark.parametrize('repair_succeeds', [True, False])
def test_quantity_failure_is_corrected_once_then_checked_again(repair_succeeds):
    calls = []
    reviews = []
    source_id = None

    async def provider(messages, tools):
        nonlocal source_id
        calls.append((messages, tools))
        if len(calls) == 1:
            return {'content': None, 'tool_calls': [{
                'id': 'read', 'type': 'function',
                'function': {'name': 'get_gas_specification', 'arguments': '{}'},
            }]}
        if len(calls) == 2:
            source_id = next(s['id'] for s in json.loads(messages[-1]['content'])['evidence']
                             if s.get('value_kind') == 'normative_limit')
            return {'content': f'Аргон: ≥ 99.993 % [{source_id}]. Старая марка: 95.123 %*.'}
        assert tools == [] and len(messages) == 2
        payload = json.loads(messages[-1]['content'])
        assert payload['documents'] and not payload.get('history')
        assert payload['numeric_check']['unmatched_quantities'] == [{'value': '95.123', 'unit': '%'}]
        if repair_succeeds:
            return {'content': f'Аргон: ≥ 99.993 % [{source_id}]. Для второй марки нет текущего чтения.'}
        return {'content': f'Аргон: ≥ 99.993 % [{source_id}]. Старая марка: 95.123 %*.'}

    async def reviewer(messages, tools):
        reviews.append(messages)
        return await accepted_review(messages, tools)

    history = [{'id': 'old', 'role': 'assistant', 'content': 'Старая марка: 95.123 %.'}]
    result = asyncio.run(ConversationAgent(
        provider=provider, reviewer=reviewer, session_factory=gas_session,
    ).respond('Сравни марки', history))
    assert len(calls) == 3 and len(result['events']) == 1
    assert '95.123 %' not in result['answer']
    assert result['verification']['status'] == ('reviewed' if repair_succeeds else 'rejected')
    assert len(reviews) == (1 if repair_succeeds else 0)


def test_scientific_values_equal_decimal_values():
    assert numbers('5e-7 %') == numbers('0,0000005 %') == {Decimal('0.0000005')}
    registry, provenance = evidence()
    registry['e1']['value'] = 5e-7
    result = authored_answer(parse_answer('Доля ≤ 0,0000005 % [e1]'), registry, provenance)
    assert result['verification']['numeric_check']['status'] == 'passed'


def test_source_warnings_do_not_duplicate_answer_but_are_kept_in_report():
    registry, provenance = evidence()
    provenance['e1']['warnings'] = ['Норматив, а не измерение.']
    result = authored_answer(parse_answer('Доля ≥ 99.993 % [e1]'), registry, provenance)
    assert 'Норматив, а не измерение.' not in result['answer']
    assert result['verification']['warnings'] == ['Норматив, а не измерение.']


@pytest.mark.parametrize('supported,expected', [
    ([], {}), (['seed'], {'seed': 42, 'provider': {'require_parameters': True}}),
    (['temperature'], {'temperature': 0, 'provider': {'require_parameters': True}}),
])
def test_sampling_controls_only_use_advertised_parameters(supported, expected):
    assert sampling_options({'supported_parameters': supported}) == expected
