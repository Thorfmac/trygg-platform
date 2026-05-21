import os
from dataclasses import dataclass
from dotenv import load_dotenv

load_dotenv()

@dataclass
class Config:
    db_url: str
    anthropic_api_key: str
    model_triage: str
    model_synthesis: str
    model_deep: str
    news_api_key: str
    gnews_api_key: str
    telegram_bot_token: str
    ollama_base_url: str
    ollama_model: str
    environment: str
    log_level: str

def load_config() -> Config:
    required = [
        "DATABASE_URL",
        "ANTHROPIC_API_KEY",
        "GNEWS_API_KEY",
        "TELEGRAM_BOT_TOKEN",
    ]
    missing = [key for key in required if not os.getenv(key)]
    if missing:
        raise EnvironmentError(
            f"Missing required environment variables: {', '.join(missing)}\n"
            f"Check your .env file."
        )
    return Config(
        db_url=os.environ["DATABASE_URL"],
        anthropic_api_key=os.environ["ANTHROPIC_API_KEY"],
        model_triage=os.getenv("MODEL_TRIAGE", "claude-haiku-4-5-20251001"),
        model_synthesis=os.getenv("MODEL_SYNTHESIS", "claude-sonnet-4-6"),
        model_deep=os.getenv("MODEL_DEEP", "claude-opus-4-6"),
        news_api_key=os.getenv("NEWS_API_KEY", ""),
        gnews_api_key=os.environ["GNEWS_API_KEY"],
        telegram_bot_token=os.environ["TELEGRAM_BOT_TOKEN"],
        ollama_base_url=os.getenv("OLLAMA_BASE_URL", "http://localhost:11434"),
        ollama_model=os.getenv("OLLAMA_MODEL", "qwen2.5:32b-instruct-q4_K_M"),
        environment=os.getenv("ENVIRONMENT", "development"),
        log_level=os.getenv("LOG_LEVEL", "INFO"),
    )
