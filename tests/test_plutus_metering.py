import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from agent.plutus_metering import build_usage_event, meter_normalized_usage


class PlutusMeteringTests(unittest.TestCase):
    def test_build_usage_event_maps_canonical_usage_and_baseline(self):
        usage = SimpleNamespace(
            input_tokens=120,
            output_tokens=40,
            cache_read_tokens=10,
            reasoning_tokens=3,
        )
        event = build_usage_event(
            usage,
            provider="openai",
            model="gpt-test",
            baseline={
                "baseline_input_tokens": 300,
                "baseline_output_tokens": 40,
                "source": "estimate-exact",
            },
        )
        self.assertEqual(event, {
            "provider": "openai",
            "model": "gpt-test",
            "input_tokens": 120,
            "output_tokens": 40,
            "cache_read_tokens": 10,
            "reasoning_tokens": 3,
            "baseline_input_tokens": 300,
            "baseline_output_tokens": 40,
            "source": "hermes-provider-response",
            "baseline_source": "estimate-exact",
        })

    def test_meter_normalized_usage_consumes_baseline_once(self):
        usage = SimpleNamespace(input_tokens=120, output_tokens=40,
                                cache_read_tokens=0, reasoning_tokens=0)
        meter = Mock(return_value="recorded")
        consume = Mock(return_value={"baseline_input_tokens": 300,
                                     "baseline_output_tokens": 40,
                                     "source": "estimate-exact"})
        result = meter_normalized_usage(
            usage, provider="openai", model="gpt-test", cfg={"plutus": {"enabled": True}},
            meter_fn=meter, consume_baseline_fn=consume,
        )
        self.assertEqual(result, "recorded")
        consume.assert_called_once_with()
        meter.assert_called_once()
        self.assertEqual(meter.call_args.kwargs["baseline_input_tokens"], 300)
        self.assertEqual(meter.call_args.kwargs["baseline_output_tokens"], 40)


if __name__ == "__main__":
    unittest.main()
