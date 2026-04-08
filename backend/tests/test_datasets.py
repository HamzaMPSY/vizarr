from app.core.datasets import build_registry


def test_synthetic_registry_contains_expected_dataset() -> None:
    registry = build_registry()
    assert registry.meta.id == "demo-global"
    variable_ids = {item.id for item in registry.meta.variables}
    assert variable_ids == {"temperature", "precipitation"}

