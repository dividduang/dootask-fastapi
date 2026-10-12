from datetime import date, datetime
from decimal import Decimal
import json
from .context import current_context

_translations = {}


def load_translations(path):
    with path.open(encoding="utf-8") as source:
        _translations.update({row["key"]: row for row in json.load(source)})


def translate(message):
    context = current_context.get(None)
    language = context.language if context else "zh"
    return _translations.get(message, {}).get(language) or message


class ApiError(Exception):
    def __init__(self, message: str, ret: int = 0, data=None):
        super().__init__(message)
        self.message = message
        self.ret = ret
        self.data = [] if data is None else data


def success(data=None, message="操作成功"):
    return {"ret": 1, "msg": translate(message), "data": [] if data is None else data}


def failure(error: ApiError):
    return {"ret": error.ret, "msg": translate(error.message), "data": error.data}


def json_default(value):
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    raise TypeError(f"Not JSON serializable: {type(value).__name__}")
