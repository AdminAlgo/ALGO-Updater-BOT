import contextlib
import dataclasses
import os
import tempfile
import unittest
from pathlib import Path

from src.config import Company, ConfigError, REQUIRED_ENV_VARS, Secrets, load_config

_ENV_KEYS = (
    "FACTOR_API_KEY", "LEADER_API_KEY", "ACME_KEY",
    "FACTOR_API_BASE_URL", "LEADER_API_BASE_URL", "TELEGRAM_BOT_TOKEN",
    "ADMIN_TELEGRAM_USER_IDS",
)

MINIMAL_CONFIG_YAML = """
poll_interval_seconds: 60
team_group_chat_id: "-100123"
disconnect_realert_minutes: 30
disconnect_stale_minutes: 15
shift_limit_hours: 14
connection_required_statuses: ["Driving"]
low_hours_thresholds_minutes:
  driver_group: [60]
  team_group: []
companies:
  - name: "ACME"
    provider: "factor"
    driver_group_chat_id: "-100456"
    monitor_all_drivers: true
    company_key_env: "ACME_KEY"
"""

MINIMAL_ENV = """
FACTOR_API_BASE_URL=https://api.drivehos.app
LEADER_API_BASE_URL=https://api.drivehos.app
TELEGRAM_BOT_TOKEN=123456789:test-token
FACTOR_API_KEY=test-provider-key
ACME_KEY=test-company-key
"""


@contextlib.contextmanager
def _clean_env():
    """Isolate the relevant env vars for one test (load_dotenv never overrides
    an already-set process var, so leftover state between tests must be
    scrubbed for load_config to be deterministic)."""
    saved = {k: os.environ.pop(k, None) for k in _ENV_KEYS}
    try:
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


class SchemaShapeTests(unittest.TestCase):
    def test_secrets_has_provider_key_fields(self):
        names = {f.name for f in dataclasses.fields(Secrets)}
        self.assertIn("factor_api_key", names)
        self.assertIn("leader_api_key", names)

    def test_company_has_key_fields(self):
        names = {f.name for f in dataclasses.fields(Company)}
        self.assertIn("company_key", names)
        self.assertIn("company_key_env", names)

    def test_required_env_vars_excludes_conditional_keys(self):
        # Provider/company keys are validated conditionally (only when a
        # provider/company is actually enabled and in use), not unconditionally.
        self.assertNotIn("FACTOR_API_KEY", REQUIRED_ENV_VARS)
        self.assertNotIn("LEADER_API_KEY", REQUIRED_ENV_VARS)
        self.assertIn("FACTOR_API_BASE_URL", REQUIRED_ENV_VARS)
        self.assertIn("TELEGRAM_BOT_TOKEN", REQUIRED_ENV_VARS)


class LoadConfigTests(unittest.TestCase):
    def _write(self, d, env_body=MINIMAL_ENV, yaml_body=MINIMAL_CONFIG_YAML):
        env_path = Path(d) / ".env"
        config_path = Path(d) / "config.yaml"
        env_path.write_text(env_body, "utf-8")
        config_path.write_text(yaml_body, "utf-8")
        return config_path, env_path

    def test_loads_with_provider_and_company_key(self):
        with tempfile.TemporaryDirectory() as d, _clean_env():
            config_path, env_path = self._write(d)
            config = load_config(config_path, env_path)
            self.assertEqual(len(config.companies), 1)
            company = config.companies[0]
            self.assertEqual(company.provider, "factor")
            self.assertEqual(company.company_key, "test-company-key")
            self.assertEqual(config.secrets.factor_api_key, "test-provider-key")

    def test_missing_required_var_raises_config_error(self):
        env_body = "TELEGRAM_BOT_TOKEN=123456789:test-token\n"  # missing base URLs
        with tempfile.TemporaryDirectory() as d, _clean_env():
            config_path, env_path = self._write(d, env_body=env_body)
            with self.assertRaises(ConfigError) as ctx:
                load_config(config_path, env_path)
            self.assertTrue(any("FACTOR_API_BASE_URL" in p for p in ctx.exception.problems))

    def test_company_missing_provider_still_reported(self):
        bad_yaml = MINIMAL_CONFIG_YAML.replace('provider: "factor"', 'provider: "unknown"')
        with tempfile.TemporaryDirectory() as d, _clean_env():
            config_path, env_path = self._write(d, yaml_body=bad_yaml)
            with self.assertRaises(ConfigError) as ctx:
                load_config(config_path, env_path)
            self.assertTrue(any("provider" in p for p in ctx.exception.problems))

    def test_disabled_company_needs_no_key_at_all(self):
        # A disabled company doesn't poll or authenticate, so a missing/absent
        # company_key_env shouldn't block startup — same isolation principle
        # that already applied before the bearer-token detour.
        yaml_body = MINIMAL_CONFIG_YAML.replace(
            'monitor_all_drivers: true\n    company_key_env: "ACME_KEY"',
            'monitor_all_drivers: true\n    enabled: false',
        )
        env_body = MINIMAL_ENV.replace("ACME_KEY=test-company-key\n", "")
        with tempfile.TemporaryDirectory() as d, _clean_env():
            config_path, env_path = self._write(d, env_body=env_body, yaml_body=yaml_body)
            config = load_config(config_path, env_path)
            self.assertFalse(config.companies[0].enabled)
            self.assertIsNone(config.companies[0].company_key)

    def test_enabled_company_missing_company_key_env_is_reported(self):
        yaml_body = MINIMAL_CONFIG_YAML.replace('\n    company_key_env: "ACME_KEY"', "")
        with tempfile.TemporaryDirectory() as d, _clean_env():
            config_path, env_path = self._write(d, yaml_body=yaml_body)
            with self.assertRaises(ConfigError) as ctx:
                load_config(config_path, env_path)
            self.assertTrue(any("company_key_env" in p for p in ctx.exception.problems))

    def test_enabled_company_with_unset_env_var_is_reported(self):
        env_body = MINIMAL_ENV.replace("ACME_KEY=test-company-key\n", "")
        with tempfile.TemporaryDirectory() as d, _clean_env():
            config_path, env_path = self._write(d, env_body=env_body)
            with self.assertRaises(ConfigError) as ctx:
                load_config(config_path, env_path)
            self.assertTrue(any("ACME_KEY" in p for p in ctx.exception.problems))

    def test_missing_provider_key_blocks_when_provider_in_use(self):
        env_body = MINIMAL_ENV.replace("FACTOR_API_KEY=test-provider-key\n", "")
        with tempfile.TemporaryDirectory() as d, _clean_env():
            config_path, env_path = self._write(d, env_body=env_body)
            with self.assertRaises(ConfigError) as ctx:
                load_config(config_path, env_path)
            self.assertTrue(any("FACTOR_API_KEY" in p for p in ctx.exception.problems))

    def test_provider_key_not_required_when_provider_unused(self):
        # No company uses "leader" here, so LEADER_API_KEY should never be
        # demanded even though it's absent from MINIMAL_ENV.
        with tempfile.TemporaryDirectory() as d, _clean_env():
            config_path, env_path = self._write(d)
            config = load_config(config_path, env_path)
            self.assertIsNone(config.secrets.leader_api_key)


if __name__ == "__main__":
    unittest.main()
