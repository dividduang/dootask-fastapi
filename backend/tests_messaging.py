"""Consumer-boundary checks. Not run in the implementation batch.

Pure checks: python -m backend.tests_messaging
Real MariaDB rollback/ACL checks: python -m backend.tests_messaging --users ID ID ID
IDs must be three existing locally-created active accounts in the isolated new database.
"""
import argparse
import hashlib
from sqlalchemy import select, func
from .config import Settings
from . import db, jobs
from .context import RequestContext
from .errors import ApiError
from .domains.assistant import cosine
from .domains.messaging_push import signature
from .domains.dialog_core import create_group, send_message, get_message, sync_members


def pure_checks():
    assert cosine([1, 0], [1, 0]) == 1
    assert cosine([1, 0], [0, 1]) == 0
    assert cosine([0, 0], [0, 0]) == 0
    raw = b'{"appkey":"example","timestamp":"1"}'
    url = "https://msgapi.umeng.com/api/send"
    assert signature(url, raw, "secret") == hashlib.md5(b"POST" + url.encode() + raw + b"secret").hexdigest()
    from .domains.messaging_markup import normalize_html
    markup = normalize_html(None, None, '<p onclick="bad()">Hello<a href="javascript:bad()">link</a><span class="mention user" data-id="12">@User</span></p>', None)
    assert "onclick" not in markup and "javascript:" not in markup
    assert 'class="mention user" data-id="12"' in markup
    try:
        cosine([1], [1, 2])
    except ApiError:
        pass
    else:
        raise AssertionError("Mismatched vectors must fail")


def database_checks(settings, users):
    engine = db.configure(settings)
    with engine.connect() as connection:
        with connection.begin() as tx:
            owner, recipient, outsider = users
            dialog_id = create_group(connection, owner_id=owner, userids=[owner, recipient], group_type="user", name="transactional boundary check")
            message = send_message(connection, dialog_id=dialog_id, sender_id=owner, msg_type="text", msg={"text": "real persistence"},
                                   context=RequestContext(request_id="check", fd="9001"))
            reads, outbox = db.tables["web_socket_dialog_msg_reads"], db.tables["backend_outbox"]
            assert connection.execute(select(func.count()).select_from(reads).where(reads.c.msg_id == message["id"], reads.c.userid == recipient)).scalar_one() == 1
            before = connection.execute(select(func.count()).select_from(outbox)).scalar_one()
            try:
                send_message(connection, dialog_id=dialog_id, sender_id=outsider, msg_type="text", msg={"text": "forbidden"})
            except ApiError:
                pass
            else:
                raise AssertionError("Outsider sent to dialog")
            assert connection.execute(select(func.count()).select_from(outbox)).scalar_one() == before
            assert get_message(connection, message["id"], recipient)["msg"]["text"] == "real persistence"
            sync_members(connection, dialog_id, [owner], owner_id=owner)
            assert connection.execute(select(func.count()).select_from(outbox).where(outbox.c.kind == 'search.dialog_members')).scalar_one() >= 1
            try:
                get_message(connection, message["id"], recipient)
            except ApiError:
                pass
            else:
                raise AssertionError("Revoked member read message")
            tx.rollback()
        with connection.begin():
            assert connection.execute(select(db.tables["web_socket_dialogs"].c.id).where(db.tables["web_socket_dialogs"].c.id == dialog_id)).first() is None
            assert connection.execute(select(reads.c.id).where(reads.c.msg_id == message["id"])).first() is None


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--users", type=int, nargs=3)
    args = parser.parse_args()
    pure_checks()
    if args.users:
        database_checks(Settings.from_env(), args.users)
