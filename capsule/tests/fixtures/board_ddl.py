"""The board DDL for fixtures; a missing `db.BOARD_DDL` is an assertion failure, not an error."""
from capsule import db


def board_ddl():
    ddl = getattr(db, "BOARD_DDL", None)
    assert ddl is not None, "capsule.db.BOARD_DDL is missing"
    return ddl
