"""utility for reading and writing files"""


__all__ = [
    "read_list_from_file",
    "write_list_to_file",
    "read_list_from_csv",
    "write_list_to_csv",
    "mkdirp",
]

import os
import csv
import threading
from .strings import ensure_bytes, ensure_str


class ThreadSafeWriter:
    """
    >>> from StringIO import StringIO
    >>> f = StringIO()
    >>> wtr = ThreadSafeWriter(f)
    >>> wtr.writerow(['a', 'b'])
    >>> f.getvalue() == "a,b\\r\\n"
    True
    """

    def __init__(self, f, *args, **kwargs):
        if isinstance(f, str):
            self._file = open(f, kwargs.get("filemode") or "w")
        else:
            self._file = f
        self._file.write(u"\ufeff")  # write BOM at the beginning
        self._writer = csv.writer(self._file, *args, **kwargs)
        self._lock = threading.Lock()

    def __enter__(self):
        return self

    def __exit__(self, exception_type, exception_val, trace_back):
        self._file.close()
        return True

    def _encode(self, row):
        return [ensure_str(cell) for cell in row]

    def writerow(self, row):
        row = self._encode(row)
        with self._lock:
            return self._writer.writerow(row)

    def writerows(self, rows):
        rows = (self._encode(row) for row in rows)
        with self._lock:
            return self._writer.writerows(rows)

    def close(self):
        self._file.close()


class ThreadSafeDictWriter(csv.DictWriter):
    def __init__(self, f, *args, **kwargs):
        if isinstance(f, str):
            self._file = open(f, kwargs.get("filemode") or "w")
        else:
            self._file = f
        self._file.write(u"\ufeff")  # write BOM at the beginning
        self._writer = csv.DictWriter(self._file, *args, **kwargs)
        self._lock = threading.Lock()

    def __enter__(self):
        return self

    def __exit__(self, exception_type, exception_val, trace_back):
        self._file.close()
        return True

    def _encode(self, row):
        return {ensure_str(k): ensure_str(v) for k, v in row.items()}

    def writerow(self, row):
        row = self._encode(row)
        with self._lock:
            return super(ThreadSafeDictWriter, self).writerow(row)

    def writerows(self, rows):
        rows = (self._encode(row) for row in rows)
        with self._lock:
            return super(ThreadSafeDictWriter, self).writerows(rows)

    def close(self):
        self._file.close()


def read_list_from_file(filename, type_=str, comment=None):
    """read a list from file"""
    with open(filename, encoding="utf-8") as f:
        return [
            type_(line.strip())
            for line in f
            if not comment or not line.startswith(comment)
        ]


def write_list_to_file(filename, lst):
    """write a list to file"""
    with open(filename, "w", encoding="utf-8") as f:
        for line in lst:
            f.write(u"{}\n".format(line))


def read_list_from_csv(filename, delimiter=","):
    """read a list of row from csv file"""
    with open(filename) as f:
        csv_reader = csv.reader(f, delimiter=delimiter)
        return list(csv_reader)


def write_list_to_csv(filename, lst, delimiter=","):
    """write a list of row to csv file"""
    with open(filename, "w") as f:
        csv_writer = csv.writer(f, delimiter=delimiter)
        for line in lst:
            csv_writer.writerow(line)


def mkdirp(path):
    """mkdir -p"""
    if not os.path.exists(path):
        os.mkdir(path)


def local_file(basefile, filename):
    dir_path = os.path.dirname(os.path.realpath(basefile))
    return os.path.join(dir_path, filename)
