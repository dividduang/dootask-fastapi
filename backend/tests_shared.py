"""Runnable security checks; not executed during implementation.

Run: python -m unittest backend.tests_shared backend.templates.tests_host
State checks require an isolated running MariaDB and a legally provisioned new
account identified by DOOTASK_TEST_EMAIL/PASSWORD. Never seed/bypass licensing.
"""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import SkipTest
from pgpy import PGPKey, PGPUID, PGPMessage
from pgpy.constants import PubKeyAlgorithm, KeyFlags, HashAlgorithm, SymmetricKeyAlgorithm
from sqlalchemy import select, func
from . import auth, crypto, db, security
from .config import Settings
from .context import RequestContext, current_context
from .errors import ApiError, failure, success
from .main import ALLOWLIST, token_input
from .manage import sql_statements
from .storage import confined_path, atomic_write


class SharedSecurityChecks(unittest.TestCase):
    def test_password_hashes_are_salted_and_verified(self):
        first = security.password_hash("Correct-Password7")
        second = security.password_hash("Correct-Password7")
        self.assertNotEqual(first, second)
        self.assertTrue(security.password_verify("Correct-Password7", first))
        self.assertFalse(security.password_verify("wrong", first))
        self.assertFalse(security.password_verify("wrong", "broken"))

    def test_numeric_envelope_and_context_cleanup(self):
        self.assertEqual(success(), {"ret": 1, "msg": "操作成功", "data": []})
        self.assertEqual(failure(ApiError("expired", -1))["ret"], -1)
        token = current_context.set(RequestContext("a", token="private"))
        current_context.reset(token)
        self.assertIsNone(current_context.get(None))
        self.assertNotIn("users/getmiddleware", ALLOWLIST)
        self.assertEqual(ALLOWLIST["users/key__client"], ALLOWLIST["users/key/client"])

    def test_pgp_response_and_header_wire_format(self):
        key = PGPKey.new(PubKeyAlgorithm.RSAEncryptOrSign, 2048)
        key.add_uid(PGPUID.new("test", email="test@example.org"),
                    usage={KeyFlags.EncryptCommunications, KeyFlags.EncryptStorage, KeyFlags.Sign},
                    hashes=[HashAlgorithm.SHA256], ciphers=[SymmetricKeyAlgorithm.AES256])
        packed = str(key.pubkey).replace("-----BEGIN PGP PUBLIC KEY BLOCK-----", "").replace("-----END PGP PUBLIC KEY BLOCK-----", "").strip()
        packed = packed.replace("+", "-").replace("/", "_").replace("\n", "$")
        fields = crypto.parse_header("encrypt_type=pgp;encrypt_id=client0;client_type=pgp;client_key=" + packed)
        result = crypto.encrypt_response({"ret": 1, "msg": "测试", "data": []}, fields["client_key"])
        message = PGPMessage.from_blob("-----BEGIN PGP MESSAGE-----\n\n" + result["encrypted"] + "\n-----END PGP MESSAGE-----")
        self.assertEqual(json.loads(key.decrypt(message).message)["msg"], "测试")
        with self.assertRaises(ApiError):
            crypto.parse_header("client_type=pgp;client_key=")

    def test_storage_traversal_and_atomic_replace(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for bad in ("../secret", "/absolute", "C:/secret", "dir/../../secret", "x\\y"):
                with self.assertRaises(ValueError):
                    confined_path(root, bad)
            path = atomic_write(root, "owner/blob", b"first")
            atomic_write(root, "owner/blob", b"second")
            self.assertEqual(path.read_bytes(), b"second")

    def test_sql_split_and_license_fail_closed(self):
        self.assertEqual(list(sql_statements("-- comment\nINSERT INTO t VALUES ('a;b', 'it''s'); SELECT 1;")),
                         ["INSERT INTO t VALUES ('a;b', 'it''s')", "SELECT 1"])
        with self.assertRaises(ApiError) as result:
            auth.require_registration_license()
        self.assertEqual(result.exception.data["code"], "license_unavailable")


class MariaDBStateChecks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.email, cls.password = os.environ.get("DOOTASK_TEST_EMAIL"), os.environ.get("DOOTASK_TEST_PASSWORD")
        if not cls.email or not cls.password:
            raise SkipTest("Requires legally provisioned isolated new account, not a license-bypass fixture")
        cls.engine = db.configure(Settings.from_env())

    @classmethod
    def tearDownClass(cls):
        cls.engine.dispose()

    def test_real_login_change_password_logout_and_rollback(self):
        ctx = RequestContext("state-check", ip="127.0.0.1", base_url="http://127.0.0.1:18080/")
        users = db.tables["users"]
        with db.transaction() as connection:
            before = connection.execute(select(users.c.login_num).where(users.c.email == self.email)).scalar_one()
            result = auth.login(connection, ctx, self.email, self.password)
        ctx.token = result["token"]
        with db.transaction() as connection:
            self.assertEqual(auth.info(connection, ctx)["login_num"], before + 1)
        original = ctx.token
        changed = "Changed-Password79!"
        with db.transaction() as connection:
            changed_result = auth.change_password(connection, RequestContext("change", token=original, ip=ctx.ip), self.password, changed)
        try:
            with self.assertRaises(ApiError):
                with db.transaction() as connection:
                    auth.authenticate(connection, RequestContext("revoked", token=original))
            with db.transaction() as connection:
                restored = auth.change_password(connection, RequestContext("restore", token=changed_result["token"], ip=ctx.ip), changed, self.password)
            with db.transaction() as connection:
                auth.logout(connection, RequestContext("logout", token=restored["token"]))
            with self.assertRaises(ApiError):
                with db.transaction() as connection:
                    auth.authenticate(connection, RequestContext("logged-out", token=restored["token"]))
        finally:
            # If a preceding assertion failed, recover the original test-account password.
            with db.transaction() as connection:
                credential = connection.execute(select(db.tables["auth_credentials"]).where(db.tables["auth_credentials"].c.userid == result["userid"])).mappings().one()
            if security.password_verify(changed, credential["password_hash"]):
                with db.transaction() as connection:
                    recovered = auth.change_password(connection, RequestContext("recovery", token=changed_result["token"], ip=ctx.ip), changed, self.password)
                    auth.logout(connection, RequestContext("cleanup", token=recovered["token"]))
        with self.assertRaises(RuntimeError):
            with db.transaction() as connection:
                connection.execute(users.update().where(users.c.userid == result["userid"]).values(login_num=999999))
                raise RuntimeError("force rollback")
        with db.transaction() as connection:
            self.assertEqual(connection.execute(select(users.c.login_num).where(users.c.userid == result["userid"])).scalar_one(), before + 1)
