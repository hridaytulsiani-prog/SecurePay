from payments.adapters.mock_pa import MOCK_CAPABILITIES, MockPaymentAggregatorAdapter


def list_adapter_capabilities():
    return [capability.to_model_defaults() for capability in MOCK_CAPABILITIES.values()]


def get_pa_adapter(provider):
    if provider in MOCK_CAPABILITIES:
        return MockPaymentAggregatorAdapter(provider)
    raise ValueError(f"No PA adapter registered for provider: {provider}")
