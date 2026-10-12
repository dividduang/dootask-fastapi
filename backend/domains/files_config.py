from dataclasses import dataclass
from functools import lru_cache
import os


@dataclass(frozen=True)
class FileSettings:
    office_url: str
    office_secret: str
    preview_url: str
    manticore_url: str
    publish_key: str
    embedding_url: str
    embedding_key: str
    embedding_model: str
    crontab_key: str
    libreoffice: str
    recycle_days: int
    temporary_days: int

    @classmethod
    def from_env(cls):
        return cls(office_url=os.environ.get('OFFICE_INTERNAL_URL', ''), office_secret=os.environ.get('OFFICE_JWT_SECRET', ''),
            preview_url=os.environ.get('FILEVIEW_URL', ''), manticore_url=os.environ.get('MANTICORE_URL', ''),
            publish_key=os.environ.get('DESKTOP_PUBLISH_KEY', ''), embedding_url=os.environ.get('SEARCH_EMBEDDING_URL', ''),
            embedding_key=os.environ.get('SEARCH_EMBEDDING_KEY', ''), embedding_model=os.environ.get('SEARCH_EMBEDDING_MODEL', ''),
            crontab_key=os.environ.get('CRONTAB_KEY', ''), libreoffice=os.environ.get('LIBREOFFICE_EXECUTABLE', ''),
            recycle_days=int(os.environ.get('AUTO_EMPTY_FILE_RECYCLE', '365')), temporary_days=int(os.environ.get('AUTO_EMPTY_TEMP_FILE', '30')))


@lru_cache(maxsize=1)
def external_settings():
    return FileSettings.from_env()
