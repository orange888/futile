import os
import threading
import logging
from functools import wraps
import MySQLdb as mysql
from contextlib import contextmanager

from futile.strings import ensure_str
from futile.log import get_logger
from futile.connection_pool import ConnectionPool


class MysqlConnection:
    """
    not thread safe, use connection pool to maintain thread-safety
    """

    def __init__(self, host, port, user, passwd, db):
        self._host = host
        self._port = port
        self._user = user
        self._passwd = passwd
        self._db = db
        self._logger = get_logger("mysql_connection")
        self.pid = os.getpid()

        self._connection = None

    def connect(self):
        if self._connection:
            return
        connection = mysql.connect(
            self._host,
            port=self._port,
            user=self._user,
            passwd=self._password,
            charset="utf8mb4",
        )
        self._connection = connection

    def disconnect(self):
        if self._connection:
            try:
                self._connection.close()
            except Exception:
                pass
            self._connection = None

    def __getattr__(self, attr):
        def wrapped(*args, **kwargs):
            if self._connection is None:
                self.connect()
            return getattr(self._connection, attr)(*args, **kwargs)

        return wrapped


class MysqlClient:
    """
    虽然还是没有保证每次执行都能够成功, 但是至少每次拿出的都是一个可以使用的链接,
    不会产生一直都出错的情况
    """

    def __init__(self, host, port, user, passwd, db):
        self._host = host
        self._port = port
        self._user = user
        self._passwd = passwd
        self._db = db
        self._logger = get_logger("mysql_client")
        self._connection_pool = ConnectionPool(
            connection_class=MysqlConnection,
            host=host,
            port=port,
            user=user,
            passwd=passwd,
            db=db,
        )

    def query(self, stmt):
        with self.transaction() as conn:
            cursor = conn.cursor(mysql.cursors.DictCursor)
            cursor.execute(stmt)
            return cursor

    @contextmanager
    def transaction(self):
        connection = self._connection_pool.get_connection()
        try:
            try:
                # begin 的时候如果断开了会引起 Error
                connection.begin()
            except mysql.OperationalError:
                connection.disconnect()
                connection.begin()
            yield connection
            connection.commit()
        finally:
            self._connection_pool.release(connection)


def _quote(s):
    return "'" + mysql.escape_string(str(s)).decode("utf-8") + "'"


def _quote_key(s):
    return "`" + s + "`"


OP_REF = dict(eq="=", gt=">", ge=">=", lt="<", le="<=", ne="!=")


def _dict2str(dictin, joiner=", "):
    # in sql, where key='value' or key in (value), dicts are the values to update
    sql = []
    for k, v in dictin.items():
        if isinstance(v, (list, tuple)):
            part = f"`{k}` in ({','.join(map(_quote, v))})"
        else:
            if "__" in k:
                k, op = k.split("__")
            else:
                op = "eq"
            real_op = OP_REF[op]
            if v is None:
                part = f"{_quote_key(k)} is null"
            else:
                part = f"{_quote_key(k)}{real_op}{_quote(v)}"
        sql.append(part)
    return joiner.join(sql)


def insert_or_update(table, defaults, **where):
    """
    insert into table (keys) values (value_list) on duplicate key update (value_list)
    """
    insertion = {**defaults, **where}
    fields = ",".join(map(_quote_key, insertion.keys()))
    values = ",".join([_quote(v) for v in insertion.values()])
    updates = _dict2str(defaults)
    tmpl = "insert into %s (%s) values (%s) on duplicate key update %s"
    stmt = tmpl % (table, fields, values, updates)
    return stmt


def insert_many(table, fields, values_list, ignore=True):
    """
    insert ignore into table (keys) values (values), (values)...)

    >>> insert_many("foo", ["bar", "baz"], [[1, 2], [3, 4], [5, 6]])
    "insert ignore into foo (`bar`,`baz`) values ('1','2'),('3','4'),('5','6')"
    """
    fields = ",".join(map(_quote_key, fields))
    sql_values = []
    for values in values_list:
        values = ",".join(map(_quote, values))
        sql_values.append("(" + values + ")")
    tmpl = "insert ignore into %s (%s) values %s"
    stmt = tmpl % (table, fields, ",".join(sql_values))
    return stmt


def select(table, keys="*", where=None, limit=None, offset=None, order_by=None):
    """
    >>> select("alibaba_product", where={"product_id": 1}, limit=5, offset=5)
    "select * from alibaba_product where `product_id`='1' limit 5 offset 5"
    >>> select("alibaba_product", where=dict(product_id__gt=10))
    "select * from alibaba_product where `product_id`>'10'"
    >>> select("alibaba_product", where=dict(product_id__gt=10), order_by="id")
    "select * from alibaba_product where `product_id`>'10' order by id"
    """
    if isinstance(keys, (tuple, list)):
        keys = ",".join(keys)
    tmpl = "select %s from %s"
    sql = [tmpl % (keys, table)]
    if where:
        sql.append("where")
        sql.append(_dict2str(where, " and "))
    if limit:
        sql.append("limit")
        sql.append(str(limit))
    if offset:
        sql.append("offset")
        sql.append(str(offset))
    if order_by:
        sql.append("order by")
        sql.append(str(order_by))

    return " ".join(sql)


def create_table(table, fields, indexes=None, unique=None):
    sql = [
        "create table if not exists ",
        table,
        "(id bigint unsigned not null primary key auto_increment,",
    ]
    for field_name, field_type in fields:
        sql.append(_quote_key(field_name))
        sql.append(field_type)
        sql.append(",")
    if indexes:
        for index in indexes:
            sql.append("index")
            if isinstance(index, str):
                index = [index]
            sql.append(
                "idx_%s(%s)" % ("_".join(index), ",".join(map(_quote_key, index)))
            )
            sql.append(",")
    if unique:
        for uniq in unique:
            sql.append("unique")
            if isinstance(uniq, str):
                uniq = [uniq]
            sql.append(
                "uniq_%s(%s)" % ("_".join(uniq), ",".join(map(_quote_key, uniq)))
            )
            sql.append(",")
    sql.pop()
    sql.append(") Engine=InnoDB default charset=utf8mb4 collate utf8mb4_general_ci;")
    stmt = " ".join(sql)

    return stmt


def insert(table, defaults):
    fields = ",".join(map(_quote_key, defaults.keys()))
    values = ",".join(map(_quote, defaults.values()))
    tmpl = "insert into %s (%s) values (%s)"
    stmt = tmpl % (table, fields, values)
    return stmt


def update(table, defaults, **where):
    tmpl = "update %s set %s where %s"
    stmt = tmpl % (table, _dict2str(defaults), _dict2str(where, " and "))
    return stmt


class MysqlDatabase:
    def __init__(self, client, dry_run=False, log_id=""):
        self._client = client
        self._dry_run = dry_run
        self._log_id = f"/* {log_id} */"

    def query(self, stmt, commit=True):
        conn = self._client.connection()
        cursor = conn.cursor(mysql.cursors.DictCursor)
        cursor.execute(stmt)
        if commit:
            conn.commit()
        return cursor

    def _add_log_id(self, stmt):
        return stmt + self._log_id

    def create_table(self, table, fields, indexes=None, unique=None):
        stmt = create_table(table, fields, indexes, unique)
        stmt = self._add_log_id(stmt)
        if self._dry_run:
            print(stmt)
        else:
            return self.query(stmt)

    def insert_or_update(self, table, defaults, **where):
        stmt = insert_or_update(table, defaults, **where)
        stmt = self._add_log_id(stmt)
        if self._dry_run:
            print(stmt)
        else:
            return self.query(stmt)

    def insert(self, table, defaults):
        stmt = insert(table, defaults)
        stmt = self._add_log_id(stmt)
        if self._dry_run:
            print(stmt)
        else:
            return self.query(stmt)

    def update(self, table, defaults, **where):
        stmt = update(table, defaults, **where)
        stmt = self._add_log_id(stmt)
        if self._dry_run:
            print(stmt)
        else:
            return self.query(stmt)

    def select(
        self, table, keys="*", where=None, limit=None, offset=None, order_by=None
    ):
        stmt = select(table, keys, where, limit, offset)
        stmt = self._add_log_id(stmt)
        if self._dry_run:
            print(stmt)
        else:
            return self.query(stmt)

    def iter_select(self, table, keys="*", where=None, chunk_size=20):
        """
        迭代读取所有元素
        """
        while True:
            cursor = self.select(
                table, keys=keys, where=where, limit=chunk_size, order_by="id"
            )
            rows = cursor.fetchall()
            if not rows:
                break
            yield from rows
            where["id__gt"] = rows[-1]["id"]


def main():
    db = MysqlDatabase(None, dry_run=True, log_id="foo")
    db.create_table(
        "alibaba_deal_info",
        [("product_id", "varchar(128)")],
        [["product_id", "city"], ["id"]],
    )
    db.insert("alibaba_deal_info", {"foo": "bar", "a": "b"})


if __name__ == "__main__":
    import doctest

    doctest.testmod()
    main()
