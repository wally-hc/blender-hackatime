from . import hooks, prefs


def register():
    prefs.register()
    hooks.register()


def unregister():
    hooks.unregister()
    prefs.unregister()
