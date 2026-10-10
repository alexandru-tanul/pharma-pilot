from pydantic import computed_field
from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", extra="ignore", env_ignore_empty=True
    )
    """Load .env file if it exists. Ignore fields not defined in this model."""

    # Database (PostgreSQL only)
    # --------------------
    POSTGRES_HOST: str
    POSTGRES_PORT: int = 5432
    POSTGRES_USER: str = "postgres"
    POSTGRES_PASSWORD: str
    POSTGRES_DB: str = "app_data"

    @computed_field
    @property
    def DATABASE_URL(self) -> str:
        """Build PostgreSQL database URL."""
        return f"postgres://{self.POSTGRES_USER}:{self.POSTGRES_PASSWORD}@{self.POSTGRES_HOST}:{self.POSTGRES_PORT}/{self.POSTGRES_DB}"

    @computed_field
    @property
    def TORTOISE_ORM(self) -> dict:
        """TortoiseORM configuration for app_data (chat history)."""
        return {
            "connections": {
                "default": {
                    "engine": "tortoise.backends.asyncpg",
                    "credentials": {
                        "host": self.POSTGRES_HOST,
                        "port": self.POSTGRES_PORT,
                        "user": self.POSTGRES_USER,
                        "password": self.POSTGRES_PASSWORD,
                        "database": self.POSTGRES_DB,
                        "minsize": 5,
                        "maxsize": 20,
                    }
                }
            },
            "apps": {
                "models": {
                    "models": ["app.models"],
                    "default_connection": "default",
                }
            },
        }

    # Parquet Data (DuckDB reads these at startup)
    # --------------------
    PARQUET_DIR: Path = Path(__file__).resolve().parent.parent / "data" / "parquet"

    # Directories
    # --------------------
    BASE_DIR: Path = Path(__file__).resolve().parent

    @computed_field
    @property
    def TEMPLATES_DIR(self) -> Path:
        return self.BASE_DIR / "templates"

    # LLM Configuration (using LiteLLM)
    # --------------------
    MODEL: str = "gemini/gemini-3-flash-preview"  # For tool calling and response generation
    GEMINI_API_KEY: str = ""  # Set in .env

    # Session
    # --------------------
    SECRET_KEY: str = "your-secret-key-here"  # Should be set in .env

    # App Settings
    # --------------------
    DEBUG: bool = True


settings = Settings()  # type: ignore
