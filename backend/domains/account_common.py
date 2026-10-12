"""Account-domain primitives; callers own the SQLAlchemy transaction."""
import json
import os
import re
from datetime import datetime
from urllib.parse import urljoin
from sqlalchemy import select, func
from .. import db
from ..auth import authenticate, require_admin, user_data
from ..context import utcnow
from ..errors import ApiError


def integer(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def text(data, key, default=""):
    value = data.get(key, default)
    return str(value).strip() if value is not None and not isinstance(value, (dict, list)) else default


def decoded(value, default=None):
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value) if value else ({} if default is None else default)
    except (ValueError, TypeError):
        return {} if default is None else default


def ids(value):
    if isinstance(value, str):
        value = decoded(value, value.strip(',').split(','))
    if not isinstance(value, (list, tuple, set)):
        value = [value]
    return list(dict.fromkeys(integer(x) for x in value if integer(x) > 0))


def packed(value):
    return ',' + ','.join(str(x) for x in dict.fromkeys(value)) + ','


def row(c, table, **where):
    t = db.tables[table]
    q = select(t).where(*(t.c[k] == v for k, v in where.items()))
    if 'deleted_at' in t.c:
        q = q.where(t.c.deleted_at.is_(None))
    result = c.execute(q).mappings().first()
    return dict(result) if result else None


def rows(c, query):
    return [dict(x) for x in c.execute(query).mappings()]


def update(c, table, where, values):
    t = db.tables[table]
    values = dict(values)
    if 'updated_at' in t.c:
        values['updated_at'] = utcnow()
    c.execute(t.update().where(*(t.c[k] == v for k, v in where.items())).values(**values))


def upsert(c, table, where, values):
    found = row(c, table, **where)
    if found:
        update(c, table, where, values)
        return found.get('id')
    return db.insert_row(c, table, **where, **values)


def actor(c, context, admin=False):
    a = authenticate(c, context)
    if admin:
        require_admin(a)
    return a


def safe_user(c, user, context, basic=False):
    result = user_data(c, user, '', context)
    result.pop('token', None)
    if basic:
        names = ('userid','email','nickname','profession','department','userimg','bot','az','pinyin','line_at','disable_at')
        result = {k: result.get(k) for k in names}
    return result


def account_policy(user, only_userid=None):
    if only_userid and only_userid != user['userid']:
        return
    if (user['userid'] == 1 and os.environ.get('PASSWORD_ADMIN') == 'disabled') or os.environ.get('PASSWORD_OWNER') == 'disabled':
        raise ApiError('当前环境禁止此操作')


def email_valid(email):
    return bool(re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', email))


def named(value, label, required=False):
    if (required or value) and len(value) < 2:
        raise ApiError(label + '不可以少于2个字')
    if len(value) > 20:
        raise ApiError(label + '最多只能设置20个字')
    return value


def paginate(c, query, data, maximum=100, default=20, key='pagesize'):
    page = max(1, integer(data.get('page'), 1))
    size = max(1, min(maximum, integer(data.get(key), default)))
    total = c.execute(select(func.count()).select_from(query.order_by(None).subquery())).scalar_one()
    items = rows(c, query.limit(size).offset((page-1)*size))
    return {'current_page':page, 'data':items, 'from':(page-1)*size+1 if items else None,
            'last_page':max(1,(total+size-1)//size), 'per_page':size,
            'to':(page-1)*size+len(items) if items else None, 'total':total}


def stored_setting(c, name, value):
    t = db.tables['settings']
    found = c.execute(select(t.c.id).where(t.c.name == name).with_for_update()).first()
    payload = json.dumps(value, ensure_ascii=False)
    if found:
        update(c, 'settings', {'id':found.id}, {'setting':payload})
    else:
        db.insert_row(c, 'settings', name=name, setting=payload)
    return value


def url(context, value):
    return urljoin(context.base_url, str(value or '')) if value else ''
