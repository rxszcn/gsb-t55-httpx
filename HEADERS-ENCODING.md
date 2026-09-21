# `httpx.Headers` 非 ASCII 取值：编码取决于读取顺序

本文只描述现状，不改任何实现或用例。所有数字都来自
`repro/headers_encoding_order.py` 的实际运行输出，环境为 Python 3.12.13、
httpx 0.28.1。

## 运行方式

复现脚本只用进程内的 `MockTransport`，不发真实网络请求。运行前先清掉代理变量，
避免代理设置干扰传输栈：

```bash
unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY all_proxy ALL_PROXY \
      no_proxy NO_PROXY
.venv/bin/python repro/headers_encoding_order.py
```

需要起本地 echo server 的客户端用例（真实 socket）在当前环境本来就起不来，
属于环境性红，与本问题无关，不去追。

## 逐行现状（脚本实际输出）

先把完整 stdout 原样抄在下面（异常信息里 Python 用的是 `'\xef'` 这种转义形态，
不是渲染后的 `ï`；字节用 `\x..` repr；最后一行回读出的才是渲染字符 `naïve`）：

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

逐行标注成不成、抛什么错、存成什么字节：

- `built request; headers encoding: ascii` —— `build_request()` 出来的请求，
  headers 里此刻只有 ASCII 内容（`Host` 等）。脚本紧接着读了一次 `.encoding`，
  编码于是被解析并缓存为 `ascii`。
- `set item on built request -> builtins.UnicodeEncodeError ... '\xef' ...` ——
  失败。编码已缓存为 `ascii`，`naïve`（`U+00EF`）无法按 ascii 编码，抛
  `UnicodeEncodeError`，值没有写入。这里之所以是 ascii，唯一原因是上一行先读了
  `.encoding`；若不读，`build_request()` 结束时内部 `_encoding` 仍是 `None`，
  同一个 setitem 会按 utf-8 成功（见下节）。
- `plain Request ... -> (b'X-Note', b'na\xc3\xafve')` —— 成功。
  `httpx.Request(..., headers={})` 构造完后 `_encoding` 仍是 `None`（构造中的
  `get("content-type")` 读到的缓存，被 `_prepare()` 里重建的新 `Headers` 替换
  掉），没有任何读取把编码定成 ascii，setitem 走 utf-8 兜底，值存为 UTF-8 字节
  `b'na\xc3\xafve'`。
- `setitem first -> [(b'A', b'b'), (b'X', b'caf\xc3\xa9')]` —— 成功。全新对象、
  此前无读取，`_encoding is None`，setitem 按 utf-8 兜底，`café`（`U+00E9`）存为
  `b'caf\xc3\xa9'`。
- `setitem after -> builtins.UnicodeEncodeError ... '\xe9' ...` —— 失败。
  `h2.get("a")` 是一次只读查询，它访问了 `encoding` 属性；此刻对象里只有 ASCII 的
  `A: b`，编码被解析并缓存为 `ascii`。之后 setitem 同样的 `café` 就按 ascii 编码，
  抛 `UnicodeEncodeError`，值没有写入。
- `h == h2 semantics: True` —— 这行只打印布尔表达式 `h is not h2` 的结果，说明
  `h`、`h2` 是两个不同对象，与内容是否相等无关；两者命运不同仅因为 setitem 前
  有没有发生过读取。
- ctor 路径 `builtins.UnicodeEncodeError ... '\xef' ...` —— 失败，但路径不同。
  `c.get(..., headers={"X-Note": "naïve"})` 在**构造** `Headers` 时就把字符串入参
  转字节；构造器没拿到显式 encoding，`_normalize_header_value` 直接按 ascii 编码
  （`value.encode(encoding or "ascii")`），当场抛 `UnicodeEncodeError`，对象都没
  建出来。此路径不经过 setitem，与读取顺序无关。
- `bytes path OK -> naïve | encoding: utf-8` —— 成功。传入的是已编码字节
  `(b"X-Note", "naïve".encode())`，`_normalize_header_value` 对 bytes 原样保留，
  构造时不做编码。之后读取（如 `__getitem__`）解析编码时 ascii 失败、utf-8 成功，
  缓存为 `utf-8`，回读出 `naïve`；底层存的是 UTF-8 字节 `b'na\xc3\xafve'`，并会
  以这串字节发出去。

## 为什么“先读一次再写”和“上来就写”结果不同

`Headers` 内部一律按字节存（`self._list` 里的 `(key, lower_key, value)` 都是
`bytes`），字符串与字节之间的编码由一个**懒解析、只算一次并缓存**的属性决定：

- 构造时 `self._encoding` 只是入参 `encoding`，未传则为 `None`
  （`httpx/_models.py` 中 `Headers.__init__`）。
- `encoding` 是个 property：当 `_encoding is None` 时，按 `ascii` → `utf-8` →
  `iso-8859-1` 的顺序，尝试用每种编码解码 `self.raw` 里的全部键值，第一个能全过
  的就**写回并缓存**到 `self._encoding`，之后不再重算
  （`httpx/_models.py` 中 `Headers.encoding`）。
- 几乎所有只读路径都会触发它：`__getitem__`/`get`、`__contains__`（`in`）、
  `keys()`/`values()`/`items()`、`get_list`、`__delitem__`，以及直接读
  `.encoding`。
- 写入 `__setitem__` 用的是 `key.encode(self._encoding or "utf-8")` 与
  `value.encode(self._encoding or "utf-8")`：**不触发解析，只看缓存**。缓存为
  `None` 时兜底 utf-8；缓存已经是 ascii 时就硬按 ascii 编码。

于是同一个值、同一个对象：

- 上来就写：还没人读过，`_encoding is None`，setitem 走 utf-8 兜底，非 ASCII 写得
  进去，存成 UTF-8 字节。
- 先读一次再写：读取触发 `encoding` 解析。只要此刻对象里现存内容全是 ASCII（这是
  常见初始状态），ascii 尝试成功，编码被**缓存成 `ascii`**；之后 setitem 看到缓存
  非 `None`，按 ascii 编码非 ASCII 值，抛 `UnicodeEncodeError`。

把缓存钉死成 ascii 的“那一处读取”，就是任何一次访问 `encoding` property 的只读
操作——脚本里分别是 `h2.get("a")`（简化用例）和脚本开头那次
`print(... req.headers.encoding)`（built request 用例）。`in` 判定、取一个键同样
会触发，效果一样。

构造路径则是另一回事：`Headers({"X-Note": "naïve"})` 在 `__init__` 里立即用
`_normalize_header_value` 把字符串编成 ascii，没等到懒解析就抛
`UnicodeEncodeError`。这解释了为什么“setitem 一个空/ASCII 对象”有时能成，而“构造
时直接传非 ASCII 字符串”永远不成。

补充：`build_request()` 结束时 headers 的 `_encoding` 其实也是 `None`
（`_prepare()` 用 `Headers(auto_headers + self.headers.raw)` 重建了对象），脚本里
它之所以变成 ascii，纯粹因为紧接着打印时读了一次 `.encoding`；不读那一下，同一个
setitem 也会按 utf-8 成功。空的 `Request(..., headers={})` 之所以保持 `None`，同理
是因为构造中的读取缓存被 `_prepare()` 重建丢弃了。

## 同一份 headers：WSGI 崩、ASGI 不崩

先 setitem（未读取）得到的 headers 里，非 ASCII 值以 UTF-8 字节存放，例如
`b'na\xc3\xafve'`。两个传输对这串字节的处理不同：

- WSGI（`httpx/_transports/wsgi.py`）在组装 `environ` 时对每个原始值硬解码：
  `environ[key] = header_value.decode("ascii")`（键也是
  `header_key.decode("ascii")`）。这串字节不是合法 ascii，抛
  `UnicodeDecodeError: 'ascii' codec can't decode byte 0xc3 in position 2:
  ordinal not in range(128)`。这也与 WSGI PEP 3333 的要求相关：environ 里的
  `HTTP_*` 变量是 `str`，需按 latin-1 承载字节；httpx 这里直接用 ascii，于是
  UTF-8 字节在这一步炸掉。
- ASGI（`httpx/_transports/asgi.py`）的 scope 按 ASGI 规范本来就把 headers 定义
  为 `list[tuple[bytes, bytes]]`，httpx 直接
  `"headers": [(k.lower(), v) for (k, v) in request.headers.raw]` 透传字节，
  不解码、不校验，UTF-8 字节原样到达应用，因此不崩。

（WSGI 回程同理：`start_response` 返回的字符串响应头也被
`(key.encode("ascii"), value.encode("ascii"))` 硬编码，非 ASCII 同样过不去。）

已用进程内 app 验证该差异：同一份先 setitem 存下的 headers，WSGITransport 抛
`builtins.UnicodeDecodeError: 'ascii' codec can't decode byte 0xc3 ...`，
ASGITransport 返回 200，应用侧 scope 收到
`[(b'host', b'ex.com'), (b'x-note', b'na\xc3\xafve')]`。

## 应当统一的规则

非 ASCII 取值的编码必须在**值进入 `Headers` 的那一步（构造/写入入口）一次性定死
为一个显式、固定的编码（事实标准是 utf-8）**，不能让 `encoding` 依赖“第一次读取
时现存内容碰巧能不能过 ascii”这种顺序相关的懒推断；读取路径也不应再去回写这个
缓存。这样“先读后写”和“上来就写”、构造路径和 setitem 路径才会一致。

在实现未统一前，唯一稳定可用的路是**构造时就传已按 utf-8 编码好的字节**（bytes
原样保留，不触发 ascii 编码）；必要时配合显式
`Headers(..., encoding="utf-8")` 或在任何读取前 `h.encoding = "utf-8"`，让后续
setitem/回读都按 utf-8。要注意这条逃生路只在接受字节的传输上有效——ASGI 与直接
取 `raw` 字节的栈能正常透传，而 WSGITransport 仍会在 `decode("ascii")` 处崩，
传字节绕不过 WSGI 这一关。
