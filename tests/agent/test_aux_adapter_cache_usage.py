"""Regression: auxiliary adapter usage objects preserve the cache-token
breakdown through ``agent.usage_pricing.normalize_usage`` as called by
``agent.aux_accounting.record_aux_usage`` — i.e. WITHOUT an explicit
``api_mode`` (aux accounting always resolves via the default chat shape).

Prior to this fix, ``_CodexCompletionsAdapter`` and ``_AnthropicCompletionsAdapter``
rebuilt usage as bare prompt/completion/total, silently dropping the cache
read/write breakdown. Every adapter-path aux call (compression, vision, title
generation, ...) was then recorded — and priced — as fully uncached, even
when the underlying provider reported a cache hit. These tests assert the
actual accounting *behavior* (cache tokens surviving to the canonical usage
object used for accounting/pricing), not mere presence of a field name.
"""

from types import SimpleNamespace

from agent.auxiliary_client import (
    _AnthropicCompletionsAdapter,
    _extract_cached_token_details,
)
from agent.auxiliary_codex_response import _parse_codex_final_response
from agent.usage_pricing import normalize_usage


class _ExplodingUsage:
    """Simulates a malformed/exotic SDK usage object (proxy, pydantic model
    with a validator that raises on access, ...) whose attribute access
    raises something other than AttributeError."""

    def __getattr__(self, name):
        raise RuntimeError(f"boom accessing {name}")


def test_extract_cached_token_details_never_raises_on_malformed_usage():
    # _extract_cached_token_details's own docstring context (see the module
    # comment above _aux_usage_field) promises this helper "never raises --
    # it sits in the response-construction path, where an exception would
    # break the aux call itself, not just its accounting". A nested-detail
    # lookup that raises defeats that contract.
    assert _extract_cached_token_details(_ExplodingUsage()) == (0, 0)


class _SecretValue:
    """A value whose repr/str would leak a fake credential if ever logged."""

    def __getattr__(self, name):
        raise RuntimeError("leaked-marker-9f3a2b7c-do-not-log")

    def __repr__(self):
        return "SECRET_REPR_leaked-marker-9f3a2b7c-do-not-log"


def test_extract_cached_token_details_emits_safe_diagnostic_on_genuine_exception(caplog):
    """A genuine exception during the nested-detail read must not be silent:
    it should emit a debug diagnostic naming only the known, literal field
    ('input_tokens_details' / 'prompt_tokens_details'), never the usage
    object, its value, the exception text, or a traceback (any of which
    could carry secrets from a malformed/exotic SDK response object)."""
    import logging

    with caplog.at_level(logging.DEBUG, logger="agent.auxiliary_client"):
        result = _extract_cached_token_details(_SecretValue())

    assert result == (0, 0)
    messages = [r.getMessage() for r in caplog.records]
    assert any("input_tokens_details" in m or "prompt_tokens_details" in m for m in messages), messages
    full_text = "\n".join(messages)
    assert "leaked-marker" not in full_text
    assert "SECRET_REPR" not in full_text
    # No traceback text either (exc_info was not requested for this path).
    assert "Traceback" not in full_text


def test_extract_cached_token_details_stays_quiet_on_benign_missing_fields(caplog):
    """A usage shape that simply lacks the nested-detail attributes/keys is
    the ordinary, expected case (e.g. a bare OpenAI chat usage object with no
    cache breakdown at all) and must NOT emit any diagnostic — only a
    genuine exception during the read should log."""
    import logging

    plain_usage = SimpleNamespace(input_tokens=10, output_tokens=5, total_tokens=15)

    with caplog.at_level(logging.DEBUG, logger="agent.auxiliary_client"):
        result = _extract_cached_token_details(plain_usage)

    assert result == (0, 0)
    assert caplog.records == []


def test_extract_cached_token_details_return_type_is_int_pair():
    """Documents the tuple[int, int] contract callers rely on for unpacking
    (cache_read, cache_write) = _extract_cached_token_details(...)."""
    read, write = _extract_cached_token_details(
        SimpleNamespace(input_tokens_details=SimpleNamespace(cached_tokens=3, cache_write_tokens=7))
    )
    assert (read, write) == (3, 7)
    assert isinstance(read, int) and isinstance(write, int)


class _BoolRaisingDetail:
    """A nested-detail object that is successfully fetched via getattr but
    explodes on the subsequent truthiness check (`if det:`) -- a distinct
    failure mode from the attribute-access guard covered above."""

    def __bool__(self):
        raise RuntimeError("boom on bool()")


def test_extract_cached_token_details_never_raises_when_detail_bool_raises():
    """The `if det:` truthiness check for a successfully-fetched nested
    detail is a second, separate trust boundary from the getattr/dict.get
    call above it -- both must be covered by the same never-raises guard."""
    usage = SimpleNamespace(input_tokens_details=_BoolRaisingDetail())
    assert _extract_cached_token_details(usage) == (0, 0)


def test_malformed_usage_does_not_break_the_aux_call():
    """End-to-end guard: a fully malformed usage object anywhere in the
    Anthropic adapter's usage-construction path must not raise -- accounting
    degrades to zeros/absence, but the aux call's response construction
    itself must succeed (this is the documented boundary this helper and its
    sibling _aux_usage_field exist to protect)."""
    response = SimpleNamespace(usage=_ExplodingUsage())
    # _AnthropicCompletionsAdapter's usage-construction reads go through
    # _aux_usage_field / _extract_cached_token_details exactly like
    # _parse_codex_final_response does; exercise the documented boundary
    # helper directly rather than standing up the full adapter/SSE stack.
    from agent.auxiliary_client import _aux_usage_field

    assert _aux_usage_field(response.usage, "input_tokens") == 0
    assert _extract_cached_token_details(response.usage) == (0, 0)


def _codex_final_response(input_tokens, output_tokens, cached_tokens, cache_write_tokens=0):
    return SimpleNamespace(
        output=[],
        usage=SimpleNamespace(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=input_tokens + output_tokens,
            input_tokens_details=SimpleNamespace(
                cached_tokens=cached_tokens, cache_write_tokens=cache_write_tokens,
            ),
        ),
    )


class TestCodexAdapterCachePreservedThroughDefaultNormalization:
    """_parse_codex_final_response's usage object must carry cache info that
    survives normalize_usage() called WITHOUT api_mode="codex_responses" —
    exactly how agent.aux_accounting.record_aux_usage() calls it."""

    def test_cache_read_survives_default_chat_shape_normalization(self):
        final = _codex_final_response(input_tokens=4000, output_tokens=100, cached_tokens=3920)
        _, _, usage, _ = _parse_codex_final_response(final)

        # This is the actual accounting entrypoint: no api_mode kwarg.
        canonical = normalize_usage(usage)

        assert canonical.cache_read_tokens == 3920
        # Chat shape treats prompt_tokens as INCLUSIVE of cache; input_tokens
        # is derived by subtracting the cache buckets back out.
        assert canonical.input_tokens == 4000 - 3920
        assert canonical.output_tokens == 100

    def test_cache_write_survives_default_chat_shape_normalization(self):
        final = _codex_final_response(
            input_tokens=4000, output_tokens=100, cached_tokens=0, cache_write_tokens=4000,
        )
        _, _, usage, _ = _parse_codex_final_response(final)

        canonical = normalize_usage(usage)

        assert canonical.cache_write_tokens == 4000
        assert canonical.cache_read_tokens == 0

    def test_zero_cache_reports_zero_not_missing(self):
        final = _codex_final_response(input_tokens=500, output_tokens=20, cached_tokens=0)
        _, _, usage, _ = _parse_codex_final_response(final)

        canonical = normalize_usage(usage)

        assert canonical.cache_read_tokens == 0
        assert canonical.input_tokens == 500
        assert canonical.output_tokens == 20


class TestAnthropicAdapterCachePreservedThroughDefaultNormalization:
    """_AnthropicCompletionsAdapter's rebuilt usage must carry cache info
    that survives the SAME api_mode-less normalize_usage() call."""

    def _adapter_usage(self, input_tokens, output_tokens, cache_read=0, cache_write=0):
        # Exercise the exact code path: build a fake response.usage as the
        # Anthropic SDK would shape it (input_tokens EXCLUDES cache) and run
        # it through the adapter's usage-construction block by calling the
        # module-level helpers the adapter itself uses.
        from agent.auxiliary_client import _aux_usage_field, _extract_cached_token_details

        raw = SimpleNamespace(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_input_tokens=cache_read,
            cache_creation_input_tokens=cache_write,
        )
        in_tok = _aux_usage_field(raw, "input_tokens")
        out_tok = _aux_usage_field(raw, "output_tokens")
        read, write = _extract_cached_token_details(raw)
        prompt_tokens = in_tok + read + write
        return SimpleNamespace(
            prompt_tokens=prompt_tokens, completion_tokens=out_tok,
            total_tokens=prompt_tokens + out_tok,
            input_tokens=in_tok, output_tokens=out_tok,
            cache_read_input_tokens=read, cache_creation_input_tokens=write,
        )

    def test_cache_read_survives_default_chat_shape_normalization(self):
        usage = self._adapter_usage(input_tokens=80, output_tokens=50, cache_read=3920)

        canonical = normalize_usage(usage)

        assert canonical.cache_read_tokens == 3920
        # prompt_tokens is inclusive (80 uncached + 3920 cached = 4000);
        # normalize_usage derives input_tokens by subtracting cache back out.
        assert canonical.input_tokens == 80
        assert canonical.output_tokens == 50

    def test_cache_write_survives_default_chat_shape_normalization(self):
        usage = self._adapter_usage(input_tokens=4000, output_tokens=50, cache_write=1000)

        canonical = normalize_usage(usage)

        assert canonical.cache_write_tokens == 1000
        assert canonical.input_tokens == 4000

    def test_no_cache_reports_zero_and_matches_prior_behavior(self):
        usage = self._adapter_usage(input_tokens=10, output_tokens=5)

        canonical = normalize_usage(usage)

        assert canonical.cache_read_tokens == 0
        assert canonical.cache_write_tokens == 0
        assert canonical.input_tokens == 10
        assert canonical.output_tokens == 5


class TestAnthropicCompletionsAdapterUsageConstruction:
    """End-to-end through the adapter class itself (not just the helpers),
    using the same fake client shape as the existing
    test_anthropic_auxiliary_client_aggregates_stream_response test."""

    def test_adapter_emits_inclusive_prompt_tokens_with_cache_fields(self):
        class _FakeStream:
            def __init__(self, final_message):
                self._final = final_message

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def __iter__(self):
                return iter(())

            def get_final_message(self):
                return self._final

        final_message = SimpleNamespace(
            content=[SimpleNamespace(type="text", text="hi")],
            stop_reason="end_turn",
            usage=SimpleNamespace(
                input_tokens=80, output_tokens=10,
                cache_read_input_tokens=3920, cache_creation_input_tokens=0,
            ),
        )
        messages_api = SimpleNamespace(stream=lambda **kw: _FakeStream(final_message))
        real_client = SimpleNamespace(messages=messages_api)
        adapter = _AnthropicCompletionsAdapter(real_client, "claude-sonnet-4-20250514")

        response = adapter.create(messages=[{"role": "user", "content": "hi"}], max_tokens=16)

        # Inclusive prompt_tokens (OpenAI convention): uncached + cache_read.
        assert response.usage.prompt_tokens == 80 + 3920
        assert response.usage.completion_tokens == 10
        assert response.usage.cache_read_input_tokens == 3920

        canonical = normalize_usage(response.usage)
        assert canonical.cache_read_tokens == 3920
        assert canonical.input_tokens == 80
