import httpx


def mock(request):
    return httpx.Response(200)


with httpx.Client(transport=httpx.MockTransport(mock)) as c:
    req = c.build_request("GET", "http://ex.com/")
    print("built request; headers encoding:", req.headers.encoding)
    try:
        req.headers["X-Note"] = "na\u00efve"
        print("  set item on built request ->", req.headers.raw[-1])
    except BaseException as exc:
        print(f"  set item on built request -> {type(exc).__module__}.{type(exc).__name__}: {exc}")

    fresh = httpx.Request("GET", "http://ex.com/", headers={})
    try:
        fresh.headers["X-Note"] = "na\u00efve"
        print("  plain Request without _prepare lookups? ->", fresh.headers.raw[-1])
    except BaseException as exc:
        print(f"  plain Request -> {type(exc).__name__}: {exc}")

print("=== the same value, three ways, one object ===")
h = httpx.Headers({"A": "b"})
h["X"] = "caf\u00e9"
print("  setitem first  ->", h.raw)
h2 = httpx.Headers({"A": "b"})
_ = h2.get("a")            # any read-only lookup caches encoding
try:
    h2["X"] = "caf\u00e9"
    print("  setitem after  ->", h2.raw)
except BaseException as exc:
    print(f"  setitem after  -> {type(exc).__module__}.{type(exc).__name__}: {exc}")
print("  h == h2 semantics:", h is not h2)
print("=== Client.post(headers=...) ctor path ===")
with httpx.Client(transport=httpx.MockTransport(mock)) as c:
    try:
        c.get("http://ex.com/", headers={"X-Note": "na\u00efve"})
        print("  ok")
    except BaseException as exc:
        print(f"  {type(exc).__module__}.{type(exc).__name__}: {exc}")
print("=== but bytes are accepted, and utf-8 is then *sent* ===")
with httpx.Client(transport=httpx.MockTransport(mock)) as c:
    r = c.get("http://ex.com/", headers=[(b"X-Note", "na\u00efve".encode())])
    print("  bytes path OK ->", r.request.headers["x-note"], "| encoding:", r.request.headers.encoding)
