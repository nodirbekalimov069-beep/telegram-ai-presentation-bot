import builtins
import importlib
import sys
from pathlib import Path


def test_essay_pipeline_has_no_concrete_openai_provider_dependency() -> None:
    source = Path("modules/service_orders/essay_pipeline.py").read_text(encoding="utf-8")

    assert "providers.openai_compatible" not in source
    assert "QuizProviderBudgetExhausted" not in source


def test_essay_pipeline_imports_without_openai_adapter(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    module_name = "modules.service_orders.essay_pipeline"
    original_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):  # type: ignore[no-untyped-def]
        if name == "modules.ai.providers.openai_compatible":
            raise ModuleNotFoundError(name)
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    sys.modules.pop(module_name, None)

    assert importlib.import_module(module_name) is not None
