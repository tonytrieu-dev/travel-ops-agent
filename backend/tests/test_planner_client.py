import subprocess
import sys
import textwrap

import pytest


@pytest.mark.no_database
def test_runtime_client_follows_generated_baml():
    # A fresh interpreter exercises import-time settings without changing the shared agent.
    script = textwrap.dedent(r'''
        import asyncio
        import os
        import re

        import baml_sdk

        sources = baml_sdk.get_baml_source_files()
        source = sources["travel_planner.baml"]
        source = re.sub(r'model = "[^"]+"', 'model = "regression-model"', source, count=1)
        source = re.sub(r'base_url = "[^"]+"', 'base_url = "https://planner.example.test/v1"', source, count=1)
        source = re.sub(r'api_key = env\.[A-Z_][A-Z_0-9]*', 'api_key = env.REGRESSION_PLANNER_KEY', source, count=1)
        baml_sdk.get_baml_source_files = lambda: {**sources, "travel_planner.baml": source}
        os.environ["REGRESSION_PLANNER_KEY"] = "dummy-planner-key"
        os.environ["CEREBRAS_API_KEY"] = "wrong-legacy-key"
        os.environ["SEARCHAPI_API_KEY"] = "dummy-search-key"
        os.environ["TAVILY_API_KEY"] = "dummy-activity-key"
        os.environ["DATABASE_URL"] = "postgresql+asyncpg://localhost/unused_test"

        from pydantic_ai.models.cerebras import CerebrasModel
        from app.agent.planner import agent

        assert isinstance(agent.model, CerebrasModel)
        try:
            assert agent.model.model_name == "regression-model"
            assert str(agent.model.client.base_url) == "https://planner.example.test/v1/"
            assert agent.model.client.api_key == "dummy-planner-key"
        finally:
            asyncio.run(agent.model.client.close())
    ''')
    subprocess.run([sys.executable, "-c", script], check=True, timeout=30)
