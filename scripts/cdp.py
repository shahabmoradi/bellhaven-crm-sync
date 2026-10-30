"""Minimal CDP driver for the demo recording: real DOM clicks in a
visible Chrome under Xvfb, recorded with ffmpeg x11grab."""
import base64
import json
import time
import urllib.request

import websocket

DEBUG = "http://127.0.0.1:9222"


class Tab:
    def __init__(self, ws_url):
        self.ws = websocket.create_connection(ws_url, timeout=30)
        self._id = 0

    def call(self, method, **params):
        self._id += 1
        self.ws.send(json.dumps({"id": self._id, "method": method, "params": params}))
        while True:
            msg = json.loads(self.ws.recv())
            if msg.get("id") == self._id:
                if "error" in msg:
                    raise RuntimeError(f"{method}: {msg['error']}")
                return msg.get("result", {})

    def navigate(self, url, wait=2.0):
        self.call("Page.navigate", url=url)
        time.sleep(wait)

    def js(self, expr):
        r = self.call("Runtime.evaluate", expression=expr, returnByValue=True)
        return r.get("result", {}).get("value")

    def shot(self, path):
        r = self.call("Page.captureScreenshot", format="png")
        open(path, "wb").write(base64.b64decode(r["data"]))


def open_tab(url):
    req = urllib.request.Request(f"{DEBUG}/json/new?{url}", method="PUT")
    info = json.load(urllib.request.urlopen(req))
    # Make the new tab the visible one (the recording films the screen,
    # not the CDP session).
    try:
        urllib.request.urlopen(f"{DEBUG}/json/activate/{info['id']}")
    except Exception:
        pass
    tab = Tab(info["webSocketDebuggerUrl"])
    tab.call("Page.enable")
    tab.call("Runtime.enable")
    time.sleep(1.5)
    return tab


def card_js(subject, action, status="pending"):
    """JS that scrolls the proposal card for `subject` with the given
    status label into view and, for action='approve'/'reject', clicks
    the button (reject fills the reason first). The status match is
    essential: a subject can also appear on superseded/decided cards,
    and the first text match is not necessarily the live one."""
    finder = f"""
          const cards = [...document.querySelectorAll('.card')];
          const c = cards.find(x => x.textContent.includes({json.dumps(subject)})
                                 && x.textContent.includes('[{status}]'));
          if (!c) return 'not found';"""
    if action == "show":
        return f"""
        (() => {{{finder}
          c.scrollIntoView({{block: 'center'}});
          c.style.outline = '3px solid #e80';
          return 'shown';
        }})()"""
    if action == "approve":
        return f"""
        (() => {{{finder}
          c.scrollIntoView({{block: 'center'}});
          const b = c.querySelector("form[action*='approve'] button");
          if (!b) return 'no approve button';
          b.click();
          return 'clicked approve';
        }})()"""
    if action == "reject":
        return f"""
        (() => {{{finder}
          c.scrollIntoView({{block: 'center'}});
          const input = c.querySelector("form[action*='reject'] input[name='reason']");
          const b = c.querySelector("form[action*='reject'] button");
          if (!input || !b) return 'no reject form';
          input.value = {json.dumps(REJECT_REASON)};
          b.click();
          return 'clicked reject';
        }})()"""
    raise ValueError(action)


REJECT_REASON = ("Different street address (240 Market St vs 118 Union Square Dr); "
                 "cannot prove same facility.")
