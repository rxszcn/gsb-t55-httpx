# Headers 非 ASCII 取值：编码取决于读写顺序

本文只记录现状与成因，不改实现。复现脚本：`repro/headers_encoding_order.py`
（运行前清掉代理变量：`env -u http_proxy -u https_proxy -u HTTP_PROXY -u HTTPS_PROXY PYTHONPATH=. python3 repro/headers_encoding_order.py`）。

## 复现脚本的实际输出（逐行抄录）

```
built request; headers encoding: ascii
  set item on built request -> builtins.UnicodeEncodeError: 'ascii' codec can't encode character '\xef' in position 2: ordinal not in range(128)
  plain Request without _prepare lookups? -> (b'X-Note', b'na\xc3\xafve')
=== the same value, three ways, one object ===
  setitem first  -> [(b'A', b'b'), (b'X', b'caf\xc3\xa9')]
  setitem after  -> builtins.UnicodeEncodeError: 'ascii' codec can't encode character '\xe9' in position 3: ordinal not in range(128)
  h == h2 semantics: True
=== Client.post(headers=...) ctor path ===
  builtins.UnicodeEncodeError: 'ascii' codec can't encode character '\xef' in position 2: ordinal not in range(128)
=== but bytes are accepted, and utf-8 is then *sent* ===
  bytes path OK -> naïve | encoding: utf-8
```

读法：

- 同一个值 `"naïve"` / `"café"`，先写后读 → 成功，按 utf-8 存成 `b'na\xc3\xafve'`、`b'caf\xc3\xa9'`；
  先读后写 → 抛 `UnicodeEncodeError`（ascii 编码失败）。
- 构造时直接传 str 的非 ASCII 值（`Client.get(..., headers={"X-Note": "naïve"})`）→ 抛 `UnicodeEncodeError`。
- 构造时传已编码字节（`headers=[(b"X-Note", "naïve".encode())]`）→ 成功，字节原样保留，随后 `headers.encoding` 探测为 `utf-8`，读回 `naïve`。

## 为什么先读一次再写和上来就写结果不同

两处代码用的是两套默认编码：

- 写：`Headers.__setitem__`（`httpx/_models.py:309-310`）用 `self._encoding or "utf-8"`。
  注意它读的是私有属性 `_encoding`，不是 `encoding` property。`_encoding is None` 时回退 **utf-8**。
- 读：`Headers.encoding` property（`httpx/_models.py:167-189`）在 `_encoding is None` 时做探测——
  先试 ascii、再试 utf-8、最后兜底 iso-8859-1——并把结果**缓存**进 `self._encoding`。
  当时已有的 header 全是 ASCII 时，探测结果必然是 `"ascii"`。

于是顺序决定命运：

1. 上来就 `h["X"] = "café"`：`_encoding` 还是 `None`，setitem 走 utf-8 分支，值按 utf-8 存进 `_list`。
   事后再读 `encoding`，探测时这些字节能按 utf-8 解码，缓存为 `"utf-8"`，自洽。
2. 先做任何一次只读访问再写：只读访问几乎都要过 `self.encoding`——
   `__getitem__`（`httpx/_models.py:284`）、`get`（`:242`）、`get_list`、`keys`（`:202`）、
   `__contains__`（`:346`，即 `in` 判定）都会触发探测并把 `_encoding` 缓存成 `"ascii"`。
   之后 setitem 命中 `self._encoding or "utf-8"` 的 `"ascii"` 分支，`"café".encode("ascii")` 抛 `UnicodeEncodeError`。

也就是说，把编码值缓存下来的不是某一次特殊的调用，而是 `encoding` property 本身：
第一次读取即定案，之后写入路径跟着这份缓存走。`h2.get("a")` 那一行（脚本里的注释也写了）
只是触发缓存的最小例子，换成 `"a" in h2`、`h2["a"]`、`h2.keys()` 效果相同（已实测 `in` 判定同样致错）。

### 构造路径是第三套行为

`Headers(...)` 构造时经 `_normalize_header_value`（`httpx/_models.py:74-82`）归一化，
用的是 `encoding or "ascii"`——构造期默认 **ascii**，所以 str 的非 ASCII 值在构造时就抛
`UnicodeEncodeError`（脚本 `Client.get(headers={"X-Note": "naïve"})` 的报错来源）。
而 bytes 值原样保留、不做任何编码，之后 `encoding` 探测落到 utf-8——这就是脚本最后一段
"bytes path OK -> naïve | encoding: utf-8" 的由来。

### 为什么"built request"失败、"plain Request"成功

`Request.__init__` 里 `self.headers.get("content-type")`（`httpx/_models.py:407`）确实会触发一次
encoding 缓存，但随后的 `_prepare`（`httpx/_models.py:441`）用
`Headers(auto_headers + self.headers.raw)` **重建**了一个全新的 `Headers`（`_encoding=None`），
缓存随之作废。所以 `httpx.Request("GET", url, headers={})` 之后 setitem 仍按 utf-8 成功。
脚本第一段里 `c.build_request(...)` 出来的 request 之所以失败，是因为
`print(..., req.headers.encoding)` 这一行自己先读了 `encoding`、把 `"ascii"` 缓存了；
实测去掉这次读取，`build_request` 之后 setitem 同样成功、存为 `b'na\xc3\xafve'`。

## 同一份 headers，WSGI 为什么崩、ASGI 为什么不崩

前提：headers 里已经按 utf-8 存了非 ASCII 字节（比如先写后读那条路，或构造时传字节）。

- `WSGITransport.handle_request`（`httpx/_transports/wsgi.py:113-117`）遍历
  `request.headers.raw`，对每个值做 `header_value.decode("ascii")` 硬解成 str 塞进 WSGI environ
  （PEP 3333 要求 environ 值是 str）。utf-8 字节 `b'na\xc3\xafve'` 按 ascii 解不动，
  抛 `UnicodeDecodeError: 'ascii' codec can't decode byte 0xc3 ...`（已实测）。
- `ASGITransport`（`httpx/_transports/asgi.py:111`）把 `request.headers.raw` 的字节对原样放进
  `scope["headers"]`。ASGI 规范的 headers 本来就是 `[(bytes, bytes)]`，全程透传字节、不解码，
  所以同一份请求走 ASGI 正常返回（已实测 200）。

## 应当统一的规则

非 ASCII 取值应当在**进入 `Headers` 的那一刻**（构造/归一化，即 `_normalize_header_value`）
统一按 **utf-8** 编码定死，让构造、setitem、读取探测三条路收敛到同一编码，
而不是现在"构造默认 ascii、setitem 默认 utf-8、读取靠探测缓存"三套并存、结果取决于读写顺序。
构造时传已编码字节这条路（`headers=[(b"X-Note", "naïve".encode("utf-8"))]`）目前可用、
字节原样保留、随后探测为 utf-8，是动不了的兼容底线，统一规则时必须保留。

## 测试基线

仅新增本文档，未改实现。`tests/models tests/test_wsgi.py tests/test_asgi.py`
在改前/改后结果一致：861 passed, 6 failed。6 个失败均为环境缺可选依赖
（`brotli`、`zstandard`、`charset_normalizer`）所致，与 headers 编码无关；
`tests/models/test_headers.py` 全绿。`tests/client` 需本地 echo server，本环境跑不动，属环境性红，未追。
