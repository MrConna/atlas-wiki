from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "Atlas Wiki API"
    app_git_sha: str = "unknown"
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
    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com"
    ollama_base_url: str = "http://host.docker.internal:11434"
    embedding_provider: str = "legacy"
    embedding_model: str = "embeddinggemma:300m-qat-q4_0"
    embedding_base_url: str = "http://host.docker.internal:11434"
    embedding_dimensions: int = Field(default=768, ge=1, le=2000)
    embedding_batch_size: int = Field(default=32, ge=1, le=256)
    embedding_timeout_seconds: float = Field(default=60, ge=1, le=600)
    semantic_min_score: float = Field(default=0.40, ge=0, le=1)
    semantic_expansion_min_score: float = Field(default=0.38, ge=0, le=1)
    hybrid_min_score: float = Field(default=0.423, ge=0, le=1)
    retrieval_candidate_limit: int = Field(default=100, ge=10, le=1000)

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    @property
    def allowed_origins(self) -> list[str]:
        return [item.strip() for item in self.cors_origins.split(",") if item.strip()]

    @property
    def trusted_hosts(self) -> list[str]:
        return [item.strip() for item in self.allowed_hosts.split(",") if item.strip()]


settings = Settings()
