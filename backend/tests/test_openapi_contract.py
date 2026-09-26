"""SDD drift guard: the runtime FastAPI schema must conform to the authored specs/openapi.yaml.

A route that diverges from the contract — a missing path, a dropped error status, a renamed
error code — turns this red before it can surprise a client.
"""

from pathlib import Path

import pytest
import yaml

from app.main import app

_SPEC_PATH = Path(__file__).resolve().parents[1] / "specs" / "openapi.yaml"
pytestmark = pytest.mark.no_database


def _contract() -> dict:
    return yaml.safe_load(_SPEC_PATH.read_text())


def test_runtime_implements_every_contracted_route_and_status() -> None:
    contract = _contract()
    runtime = app.openapi()
    for path, operations in contract["paths"].items():
        assert path in runtime["paths"], f"contract path {path} is not implemented at runtime"
        for method, operation in operations.items():
            runtime_operation = runtime["paths"][path].get(method)
            assert runtime_operation is not None, (
                f"{method.upper()} {path} is in the contract but not implemented"
            )
            contracted_statuses = set(operation["responses"])
            runtime_statuses = set(runtime_operation["responses"])
            missing = contracted_statuses - runtime_statuses
            assert not missing, (
                f"{method.upper()} {path} does not declare contracted status codes {missing}"
            )


def test_runtime_component_schemas_match_contract_properties() -> None:
    """Beyond paths/statuses, every authored component schema must exist at runtime with the same
    property set — the drift guard that would have caught FlightOfferOut.legs and the agent_run →
    agent_runs rename shipping in the routes while the spec still described the old shape.

    Property NAMES (not `required`) are compared: Pydantic omits fields that carry a default from
    JSON-Schema `required` (e.g. the discriminator `status` literals), so an exact required-set
    comparison would flag those as false drift while telling a client nothing useful.
    """
    contract = _contract()
    runtime = app.openapi()
    runtime_schemas = runtime["components"]["schemas"]
    for schema_name, contract_schema in contract["components"]["schemas"].items():
        assert schema_name in runtime_schemas, (
            f"contract schema {schema_name} is not generated at runtime"
        )
        contract_properties = set((contract_schema.get("properties") or {}).keys())
        runtime_properties = set((runtime_schemas[schema_name].get("properties") or {}).keys())
        assert contract_properties == runtime_properties, (
            f"{schema_name} property drift between contract and runtime: "
            f"only-in-contract={contract_properties - runtime_properties}, "
            f"only-in-runtime={runtime_properties - contract_properties}"
        )


def test_runtime_error_code_enum_matches_contract() -> None:
    contract = _contract()
    runtime = app.openapi()
    contract_codes = set(contract["components"]["schemas"]["ErrorCode"]["enum"])
    runtime_codes = set(runtime["components"]["schemas"]["ErrorCode"]["enum"])
    assert contract_codes == runtime_codes, (
        f"error-code enum drift between contract and runtime: "
        f"only-in-contract={contract_codes - runtime_codes}, "
        f"only-in-runtime={runtime_codes - contract_codes}"
    )
