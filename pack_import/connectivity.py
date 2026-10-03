from .errors import PackFormatError


def verify_tips(tips, incoming, store):
    for oid in tips:
        if oid not in incoming and not store.has(oid):
            raise PackFormatError("tip is missing")
    return list(tips)
