import pytest

from app.llm.json_tools import ParseError, parse


def test_reply():
    assert parse('{"reply": "Hello!"}').reply == "Hello!"


def test_tool_calls_with_fence_and_string_args():
    t = parse('```json\n{"tool_calls": [{"name": "lookup_appointment", "arguments": "{\\"email\\": \\"a@b.co\\"}"}]}\n```')
    assert t.reply is None and t.tool_calls == [("lookup_appointment", {"email": "a@b.co"})]


def test_single_call_shapes():
    assert parse('{"tool_call": {"name": "list", "arguments": {}}}').tool_calls == [("list", {})]
    assert parse('Sure. {"name": "list", "arguments": {"a": 1}}').tool_calls == [("list", {"a": 1})]


def test_plain_prose_is_a_reply():
    assert parse("We open at 9am.").reply == "We open at 9am."


@pytest.mark.parametrize("bad", ["", '{"tool_calls": [{"name": "x", "arguments": ', '{"foo": 1}',
                                 '{"tool_calls": [{"arguments": {}}]}', '{"tool_calls": [{"name": "x", "arguments": [1]}]}'])
def test_invalid(bad):
    with pytest.raises(ParseError):
        parse(bad)
