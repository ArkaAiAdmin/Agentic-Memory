# backward compat - real implementation is in recall/search_memory
import sys
import types
import recall.search_memory as _real

def __getattr__(name):
    return getattr(_real, name)

def __dir__():
    return sorted(set(object.__dir__(_real)) | set(dir(_real)))

class _ShimModule(types.ModuleType):
    _real = None
    def __getattr__(self, name):
        return getattr(self._real, name)
    def __setattr__(self, name, value):
        if name in ('_real', '__class__'):
            super().__setattr__(name, value)
        else:
            setattr(self._real, name, value)
    def __delattr__(self, name):
        if name == '_real':
            raise AttributeError("_real is protected")
        delattr(self._real, name)
    def __dir__(self):
        return sorted(set(super().__dir__()) | set(dir(self._real)))

if __name__ in sys.modules:
    _shim = sys.modules[__name__]
    _shim.__class__ = _ShimModule
    object.__setattr__(_shim, '_real', _real)

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: search_memory.py <query> [limit] [--no-global] [db_path]")
        sys.exit(1)
    query = sys.argv[1]
    limit = 5
    include_global = True
    db_path = None
    for arg in sys.argv[2:]:
        if arg == "--no-global":
            include_global = False
        elif not arg.startswith("--"):
            if arg.isdigit():
                limit = int(arg)
            else:
                db_path = arg
    from recall.search_memory import search_memories as _search

    _search(
        query,
        limit=limit,
        custom_db_path=db_path,
        include_global=include_global,
        silent=False,
    )
