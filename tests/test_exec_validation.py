from __future__ import annotations

import pytest
from fastapi import HTTPException

from server.routers.exec import _validate_create_request, _validate_verify_request
from server.schemas_exec import CreateStatesRequest, VerifyRequest
from server.settings import Settings


def _capped_settings() -> Settings:
    return Settings(
        _env_file=None,
        max_acquire_timeout_ms=1000,
        max_step_timeout_ms=1000,
    )


def test_create_request_rejects_excess_step_timeout() -> None:
    # acquire is within cap so the step-timeout check is the one that must fire.
    request = CreateStatesRequest(
        env_profile="lean4.29.1_mathlib_x",
        items=[
            {
                "item_id": "theorem_42:a0",
                "code": "theorem t : True := by sorry",
                "acquire_timeout_ms": 500,
                "step_timeout_ms": 2000,
            }
        ],
    )

    with pytest.raises(HTTPException) as excinfo:
        _validate_create_request(request, _capped_settings())

    assert excinfo.value.status_code == 422
    assert "create_states step timeout" in str(excinfo.value.detail)


def test_verify_request_rejects_excess_step_timeout() -> None:
    request = VerifyRequest(
        env_profile="lean4.29.1_mathlib_x",
        items=[
            {
                "item_id": "theorem_42:a0",
                "code": "theorem t : True := trivial",
                "theorem_name": "t",
                "acquire_timeout_ms": 500,
                "step_timeout_ms": 2000,
            }
        ],
    )

    with pytest.raises(HTTPException) as excinfo:
        _validate_verify_request(request, _capped_settings())

    assert excinfo.value.status_code == 422
    assert "verify step timeout" in str(excinfo.value.detail)
