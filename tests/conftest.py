pytest_plugins = [
    "tests.fixtures.db",
    "tests.fixtures.config",
]

def pytest_sessionfinish(session, exitstatus):
    if exitstatus == 5:  # ExitCode.NO_TESTS_COLLECTED
        session.exitstatus = 0