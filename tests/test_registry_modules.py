"""The driver modules a compiled binary imports by name are the ones in
the folders: one added without the list would be missing from the .deb."""
import pkgutil

import localhome.drivers
from localhome.core.registry import BUILTIN_DRIVER_MODULES


def test_the_fixed_list_is_every_driver_module():
    found = sorted(m.name for m in pkgutil.walk_packages(localhome.drivers.__path__, prefix="localhome.drivers.")
                   if not m.ispkg)
    assert list(BUILTIN_DRIVER_MODULES) == found
