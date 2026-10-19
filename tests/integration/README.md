# Integration tests

Tests in this directory call a live model or an external service. Mark them with `@pytest.mark.integration` and skip them unless the required credentials are present. The default CI job does not run live-model tests.
