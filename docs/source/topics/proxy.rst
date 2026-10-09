Proxy Settings
==============

wzgram supports proxies with and without authentication. This feature allows wzgram to exchange data with Telegram
through an intermediate SOCKS 4/5 or HTTP (CONNECT) proxy server, through a classic MTProxy, or through a WEB proxy
relay.

-----

Usage
-----

To use wzgram with a proxy, use the *proxy* parameter in the Client class. If your proxy doesn't require authorization
you can omit ``username`` and ``password``.

.. code-block:: python

    from wzgram import Client

    proxy = {
        "scheme": "socks5",  # "socks4", "socks5", "http", "mtproxy" and "web" are supported
        "hostname": "11.22.33.44",
        "port": 1234,
        "username": "username",
        "password": "password"
    }

    app = Client("my_account", proxy=proxy)

    app.run()

-----

Proxy links
-----------

*proxy* also takes a string, so a proxy you were given as a link needs no translating. A credential inside a URL is
percent-decoded, which is how a password holding ``@`` or ``:`` is written there.

.. code-block:: python

    app = Client("my_account", proxy="socks5://user:pass@11.22.33.44:1234")
    app = Client("my_account", proxy="http://11.22.33.44:8080")
    app = Client("my_account", proxy="tg://socks?server=11.22.33.44&port=1234")
    app = Client("my_account", proxy="tg://proxy?server=11.22.33.44&port=443&secret=ee...")

-----

MTProxy
-------

A classic MTProxy speaks Telegram's own obfuscated2 protocol straight to the proxy host. It takes a *secret*, read as
hex, base64url or base64, and no username or password.

.. code-block:: python

    proxy = {
        "scheme": "mtproxy",
        "hostname": "11.22.33.44",
        "port": 443,
        "secret": "0123456789abcdef0123456789abcdef"
    }

    app = Client("my_account", proxy=proxy)

The secret decides the framing, so *proxy* is the only argument the scheme needs:

- A bare 16-byte secret is a plain obfuscated2 stream.
- A ``dd``-prefixed secret asks for random padding on every packet.
- An ``ee``-prefixed secret asks for the same padding and appends the domain the connection then imitates a TLS session
  with, which is what makes the stream look like ordinary HTTPS to whatever is watching it.

-----

WEB proxy
---------

A WEB proxy reaches a hosted relay over HTTPS and carries the same obfuscated2 stream inside it. It is addressed by
hostname only, because the relay always listens on port 443.

.. code-block:: python

    proxy = {
        "scheme": "web",
        "hostname": "relay.example.com",
        "secret": "0123456789abcdef0123456789abcdef"
    }

    app = Client("my_account", proxy=proxy)

The web scheme takes a plain or ``dd``-prefixed secret, but not an ``ee``-prefixed one: the relay speaks obfuscated2 to
its own MTProxy and never adds the TLS record layer an ``ee`` secret asks for.
