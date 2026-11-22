"""Install the base image's SQLite extension on demand in the standalone interpreter."""

import sys
from importlib.machinery import ExtensionFileLoader, ModuleSpec

from _umbermill_sqlite_path import EXTENSION


class ImageSQLite:
    @staticmethod
    def find_spec(fullname, path=None, target=None):
        if fullname == "_sqlite3":
            spec = ModuleSpec(fullname, ExtensionFileLoader(fullname, EXTENSION), origin=EXTENSION)
            spec.has_location = True
            return spec
        return None


sys.meta_path.insert(0, ImageSQLite)
