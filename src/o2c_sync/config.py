from pathlib import Path

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration, read from environment variables / .env (prefix O2C_)."""

    model_config = SettingsConfigDict(env_file=".env", env_prefix="O2C_", extra="ignore")

    database_url: str = "sqlite:///./o2c_state.db"
    mapping_path: Path = Path("config/field_mapping.yaml")
    max_retries: int = 5

    # Salesforce
    sf_username: str | None = None
    sf_password: SecretStr | None = None
    sf_security_token: SecretStr | None = None
    sf_domain: str = "login"  # "test" for sandboxes
    # Stop the run once the org's rolling 24h API usage crosses this share,
    # leaving headroom for the humans and other integrations using the org.
    sf_api_usage_ceiling: float = 0.8

    # Odoo (ERP)
    odoo_url: str = "http://localhost:8069"
    odoo_db: str = "northwind"
    odoo_username: str = "admin"
    odoo_password: SecretStr = SecretStr("admin")
