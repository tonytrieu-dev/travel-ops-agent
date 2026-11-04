import pytest

from tests.conftest import _validate_test_database_url

pytestmark = pytest.mark.no_database


def test_test_database_url_rejects_ambiguous_database_names() -> None:
    for database_name in ("contest", "latest", "travel_agent", "test"):
        with pytest.raises(RuntimeError, match="must end with _test or _testing"):
            _validate_test_database_url(f"postgresql+asyncpg://localhost/{database_name}")
