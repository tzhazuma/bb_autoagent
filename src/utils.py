import logging
import os
from pathlib import Path
from typing import Optional

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel
from rich.console import Console
from rich.logging import RichHandler

_ENV_TO_CONFIG_PATH = {
    "BB_URL": ("blackboard", "base_url"),
    "BB_USERNAME": ("blackboard", "username"),
    "BB_PASSWORD": ("blackboard", "password"),
    "SSO_URL": ("blackboard", "sso_url"),
    "OPENAI_API_KEY": ("grading", "llm", "api_key"),
    "OPENAI_MODEL": ("grading", "llm", "model"),
    "OPENAI_BASE_URL": ("grading", "llm", "base_url"),
    "HEADLESS": ("playwright", "headless"),
    "SLOW_MO": ("playwright", "slow_mo"),
    "BROWSER_TIMEOUT": ("playwright", "browser_timeout"),
    "SESSION_FILE": ("session", "file"),
    "LOG_LEVEL": ("logging", "level"),
    "LOG_FILE": ("logging", "file"),
}

_LOG_FORMAT = "%(message)s"
_LOG_DATE_FORMAT = "[%X]"
_DEFAULT_CONFIG_PATH = Path("config.yaml")


class LLMConfig(BaseModel):
    provider: str = "openai"
    model: str = "gpt-4o"
    temperature: float = 0.1
    max_tokens: int = 2000
    api_key: str = ""
    base_url: str = "https://api.openai.com/v1"


class GradingConfig(BaseModel):
    default_rubric: str = "prompts/grading_default.yaml"
    llm: LLMConfig = LLMConfig()
    batch_size: int = 5
    retry_attempts: int = 3


class BlackboardConfig(BaseModel):
    base_url: str = "https://elearning.shanghaitech.edu.cn:8443"
    sso_url: str = "https://ids.shanghaitech.edu.cn/authserver/login"
    login_timeout: int = 30000
    username: str = ""
    password: str = ""


class SubmissionConfig(BaseModel):
    download_dir: str = "./downloads"
    supported_formats: list[str] = [
        ".pdf", ".docx", ".doc", ".txt", ".py", ".java", ".cpp", ".c", ".ipynb"
    ]


class GradebookConfig(BaseModel):
    upload_timeout: int = 60000
    cell_delay: int = 500
    verify_after_upload: bool = True


class SessionConfig(BaseModel):
    file: str = "session.json"
    max_age_hours: int = 24


class PlaywrightConfig(BaseModel):
    headless: bool = False
    slow_mo: int = 0
    browser_timeout: int = 30000


class LoggingConfig(BaseModel):
    level: str = "INFO"
    file: str = "bb_autoagent.log"


class AppConfig(BaseModel):
    blackboard: BlackboardConfig = BlackboardConfig()
    courses: dict[str, str] = {}
    grading: GradingConfig = GradingConfig()
    submissions: SubmissionConfig = SubmissionConfig()
    gradebook: GradebookConfig = GradebookConfig()
    session: SessionConfig = SessionConfig()
    playwright: PlaywrightConfig = PlaywrightConfig()
    logging: LoggingConfig = LoggingConfig()


def _set_nested(d, keys, value):
    target = d
    for key in keys[:-1]:
        if key not in target:
            target[key] = {}
        target = target[key]
    target[keys[-1]] = value


def _apply_env_overrides(config_dict):
    for env_key, path in _ENV_TO_CONFIG_PATH.items():
        env_val = os.getenv(env_key)
        if env_val is not None:
            _set_nested(config_dict, path, env_val)
    return config_dict


def _coerce_env_types(config_dict):
    if "blackboard" in config_dict:
        bb = config_dict["blackboard"]
        if "login_timeout" in bb:
            bb["login_timeout"] = int(bb["login_timeout"])
    if "grading" in config_dict:
        g = config_dict["grading"]
        if "batch_size" in g:
            g["batch_size"] = int(g["batch_size"])
        if "retry_attempts" in g:
            g["retry_attempts"] = int(g["retry_attempts"])
        if "llm" in g:
            llm = g["llm"]
            if "temperature" in llm:
                llm["temperature"] = float(llm["temperature"])
            if "max_tokens" in llm:
                llm["max_tokens"] = int(llm["max_tokens"])
    if "gradebook" in config_dict:
        gb = config_dict["gradebook"]
        if "upload_timeout" in gb:
            gb["upload_timeout"] = int(gb["upload_timeout"])
        if "cell_delay" in gb:
            gb["cell_delay"] = int(gb["cell_delay"])
    if "session" in config_dict:
        s = config_dict["session"]
        if "max_age_hours" in s:
            s["max_age_hours"] = int(s["max_age_hours"])
    if "playwright" in config_dict:
        pw = config_dict["playwright"]
        if "slow_mo" in pw:
            pw["slow_mo"] = int(pw["slow_mo"])
        if "browser_timeout" in pw:
            pw["browser_timeout"] = int(pw["browser_timeout"])
    return config_dict


def load_config(config_path: Optional[Path] = None) -> AppConfig:
    path = config_path or _DEFAULT_CONFIG_PATH
    with open(path, "r") as f:
        config_dict = yaml.safe_load(f) or {}
    config_dict.setdefault("courses", {})
    if config_dict.get("courses") is None:
        config_dict["courses"] = {}
    _apply_env_overrides(config_dict)
    _coerce_env_types(config_dict)
    return AppConfig(**config_dict)


def setup_logging(level: Optional[str] = None, log_file: Optional[str] = None):
    log_level = (level or os.getenv("LOG_LEVEL", "INFO")).upper()
    log_path = log_file or os.getenv("LOG_FILE", "bb_autoagent.log")

    console = Console()
    rich_handler = RichHandler(
        console=console,
        show_time=True,
        show_level=True,
        show_path=False,
        rich_tracebacks=True,
    )

    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    )

    root_logger = logging.getLogger()
    root_logger.setLevel(getattr(logging, log_level, logging.INFO))
    root_logger.handlers.clear()
    root_logger.addHandler(rich_handler)
    root_logger.addHandler(file_handler)

    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("playwright").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)

    return root_logger


def load_env(env_path: Optional[Path] = None):
    path = env_path or Path(".env")
    if path.exists():
        load_dotenv(dotenv_path=path, override=False)


def ensure_dir(path: Path):
    path.mkdir(parents=True, exist_ok=True)
