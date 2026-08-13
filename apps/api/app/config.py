from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "Atlas Wiki API"
    database_url: str = "sqlite:///./atlas.db"
    cors_origins: str = "http://localhost:3000"
    allowed_hosts: str = "localhost,127.0.0.1,api,testserver"
    upload_dir: str = "uploads"
    max_upload_bytes: int = Field(default=10 * 1024 * 1024, ge=1024, le=100 * 1024 * 1024)
    max_storage_bytes: int = Field(default=1024 * 1024 * 1024, ge=1024 * 1024, le=100 * 1024 * 1024 * 1024)
    model_provider: str = "none"
    model_name: str = ""
    openai_api_key: str = ""
    openai_base_url: str = "https://api.openai.com/v1"
    ollama_base_url: str = "http://host.docker.internal:11434"

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    @property
    def allowed_origins(self) -> list[str]:
        return [item.strip() for item in self.cors_origins.split(",") if item.strip()]

    @property
    def trusted_hosts(self) -> list[str]:
        return [item.strip() for item in self.allowed_hosts.split(",") if item.strip()]


settings = Settings()
