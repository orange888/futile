import os
import threading
from datetime import datetime
import MySQLdb as mysql
from queue import LifoQueue, Full, Empty
from futile.strings import ensure_str


class ConnectionError(Exception):
    pass


def _quote(s):
    return "'" + str(s).replace("'", r"\'") + "'"


def _dict2str(dictin, joiner=", "):
    # in sql, where key='value' or key in (value), dicts are the values to update
    sql = []
    for k, v in dictin.items():
        if isinstance(v, (list, tuple)):
            part = f"{k} in ({','.join(map(_quote, v))})"
        else:
            part = f"{k}={_quote(v)}"
        sql.append(part)
    return joiner.join(sql)


class Connection:
    def execute(self, query):
        self.conn.ping(True)
        self.cursor.execute(query)
        return self.cursor.fetchall()


class ConnectionPool:
    def __init__(
        self,
        max_connections=50,
        timeout=20,
        connection_factory=None,
        queue_class=LifoQueue,
        **connection_kwargs,
    ):
        self.max_connections = max_connections
        self.connection_factory = connection_factory
        self.queue_class = queue_class
        self.timeout = timeout

        self.reset()

    def _checkpid(self):
        if self.pid != os.getpid():
            with self._check_lock:
                if self.pid == os.getpid():
                    # another thread already did the work while we waited on the lock.
                    return
                self.disconnect()
                self.reset()

    def reset(self):
        self.pid = os.getpid()
        self._check_lock = threading.Lock()

        # Create and fill up a thread safe queue with ``None`` values.
        self.pool = self.queue_class(self.max_connections)
        while True:
            try:
                self.pool.put_nowait(None)
            except Full:
                break

        # Keep a list of actual connection instances so that we can
        # disconnect them later.
        self._connections = []

    def make_connection(self):
        """Make a fresh connection."""
        connection = self.connection_factory(**self.connection_kwargs)
        self._connections.append(connection)
        return connection

    def get_connection(self, command_name, *keys, **options):
        """
        Get a connection, blocking for ``self.timeout`` until a connection
        is available from the pool.
        If the connection returned is ``None`` then creates a new connection.
        Because we use a last-in first-out queue, the existing connections
        (having been returned to the pool after the initial ``None`` values
        were added) will be returned before ``None`` values. This means we only
        create new connections when we need to, i.e.: the actual number of
        connections will only increase in response to demand.
        """
        # Make sure we haven't changed process.
        self._checkpid()

        # Try and get a connection from the pool. If one isn't available within
        # self.timeout then raise a ``ConnectionError``.
        connection = None
        try:
            connection = self.pool.get(block=True, timeout=self.timeout)
        except Empty:
            # Note that this is not caught by the redis client and will be
            # raised unless handled by application code. If you want never to
            raise ConnectionError("No connection available.")

        # If the ``connection`` is actually ``None`` then that's a cue to make
        # a new connection to add to the pool.
        if connection is None:
            connection = self.make_connection()

        return connection

    def release(self, connection):
        """
        Releases the connection back to the pool.
        """
        # Make sure we haven't changed process.
        self._checkpid()
        if connection.pid != self.pid:
            return

        # Put the connection back into the pool.
        try:
            self.pool.put_nowait(connection)
        except Full:
            # perhaps the pool has been reset() after a fork? regardless,
            # we don't want this connection
            pass

    def disconnect(self):
        "Disconnects all connections in the pool."
        for connection in self._connections:
            connection.disconnect()


class MysqlDatabase:
    def __init__(self, client, dry_run=False, autocommit=True):
        self._client = client
        self._client.autocommit(autocommit)
        self._dry_run = dry_run

    def query(self, stmt):
        try:
            cursor = self._client.cursor(mysql.cursors.DictCursor)
            cursor.execute(stmt)
        except mysql.OperationalError:
            self._client.ping(True)
            cursor = self._client.cursor(mysql.cursors.DictCursor)
            cursor.execute(stmt)
        return cursor

    def create_table(self, table, fields, indexes):
        sql = [
            "create table if not exists ",
            table,
            "(id bigint unsigned not null primary key auto_increment,",
        ]
        for field_name, field_type in fields:
            sql.append(field_name)
            sql.append(field_type)
            sql.append(",")
        for index in indexes:
            sql.append("index")
            sql.append("idx_%s(%s)" % (index, index))
            sql.append(",")
        sql.pop()
        sql.append(
            ") Engine=InnoDB default charset=utf8mb4 collate utf8mb4_general_ci;"
        )
        stmt = " ".join(sql)
        if self._dry_run:
            print(stmt)
        else:
            return self.query(stmt)

    def insert_or_update(self, table, defaults, **where):
        """
        insert into table (key_list) values (value_list) on duplicate key update (value_list)
        """
        insertion = {**defaults, **where}
        fields = ",".join(insertion.keys())
        values = ",".join( [_quote(v) for v in insertion.values()])
        updates = _dict2str(defaults)
        tmpl = "insert into %s (%s) values (%s) on duplicate key update %s"
        stmt = tmpl % (table, fields, values, updates)
        if self._dry_run:
            print(stmt)
        else:
            return self.query(stmt)

    def insert(self, table, defaults):
        fields = ",".join(defaults.keys())
        values = ",".join(defaults.values())
        tmpl = "insert into %s (%s) values (%s)"
        stmt = tmpl % (table, fields, values)
        if self._dry_run:
            print(stmt)
        else:
            return self.query(stmt)

    def update(self, table, defaults, **where):
        tmpl = "update %s set %s where %s"
        stmt = tmpl % (table, _dict2str(defaults), _dict2str(where, " and "))
        if self._dry_run:
            print(stmt)
        else:
            return self.query(stmt)

    def select(self, table, keys="*", where=None, limit=None, offset=None):
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

        stmt = " ".join(sql)

        if self._dry_run:
            print(stmt)
        else:
            return self.query(stmt)


def main():
    conn = mysql.connect(
        host=os.getenv("VS_DB_HOST"),
        user=os.getenv("VS_DB_USER"),
        passwd=os.getenv("VS_DB_PASSWORD"),
        db=os.getenv("VS_DB_NAME"),
        charset="utf8mb4",
    )
    db = MysqlDatabase(conn, dry_run=True)
    db.create_table(
        "alibaba_deal_info", [("product_id", "varchar(128)")], ["product_id"]
    )
    db.insert("alibaba_deal_info", {"foo": "bar", "a": "b"})
    db.select("alibaba_product", where={"product_id": 1}, limit=5, offset=5)


if __name__ == "__main__":
    main()