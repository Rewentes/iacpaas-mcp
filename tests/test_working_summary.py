"""The Russian prompt must preserve full history while budgeting model context."""

import asyncio
import copy

from standalone_agent.conversation import ConversationAgent, estimate
from standalone_agent.prompts import SUMMARY_CONTEXT_NOTE, summary_prompt, system_prompt


def test_russian_instructions_compact_working_context_without_mutating_history():
    history = [
        {'id': str(index), 'role': 'user' if index % 2 == 0 else 'assistant',
         'content': f'Сообщение {index}: ' + 'обсуждение газа и сорта ' * 8,
         'state': 'complete'}
        for index in range(32)
    ]
    original = copy.deepcopy(history)
    requests = []

    async def provider(messages, tools):
        requests.append(copy.deepcopy(messages))
        assert tools == []
        assert messages[0]['content'] == summary_prompt(256)
        assert estimate(messages, tools) + 256 <= 9000
        return {'content': 'Цель: сравнение газов. Марку уточнить. Значения ещё не прочитаны.'}

    agent = ConversationAgent(
        provider=provider, context_limit=9000, output_reserve=256,
        tool_result_bytes=2048, summary_bytes=256,
    )
    question = 'Продолжим сравнение.'
    dialogue, update, compactions = asyncio.run(agent._context(question, history, None, []))
    assert requests and compactions == len(requests)
    assert history == original
    covered = next(index for index, item in enumerate(history)
                   if item['id'] == update['covered_through_message_id'])
    assert history[covered]['role'] == 'assistant'
    assert dialogue[0] == {'role': 'system', 'content': system_prompt(12)}
    assert dialogue[1]['content'] == SUMMARY_CONTEXT_NOTE + update['text']
    assert dialogue[2:-1] == [
        {'role': item['role'], 'content': item['content']} for item in history[covered + 1:]
    ]
    assert dialogue[-1] == {'role': 'user', 'content': question}
    assert estimate(dialogue, []) + 256 + 2048 <= 9000
