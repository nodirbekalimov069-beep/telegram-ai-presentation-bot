"""Provider-neutral AI failure contracts."""


class ProviderCallBudgetExhausted(RuntimeError):
    """A durable logical-generation outbound-call budget is exhausted."""
