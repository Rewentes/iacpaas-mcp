"""Strict report schema for the independent answer checker (not main generation)."""

REVIEW_SCHEMA = {
    'type': 'object', 'additionalProperties': False,
    'properties': {
        'answers_question': {'type': 'boolean'}, 'reason': {'type': 'string'},
        'blocks': {'type': 'array', 'items': {
            'type': 'object', 'additionalProperties': False,
            'properties': {'id': {'type': 'string'},
                           'status': {'type': 'string', 'enum': ['supported', 'unsupported', 'contradicted', 'not_factual']},
                           'source_ids': {'type': 'array', 'items': {'type': 'string'}},
                           'reason': {'type': 'string'}},
            'required': ['id', 'status', 'source_ids', 'reason'],
        }},
    },
    'required': ['answers_question', 'reason', 'blocks'],
}
