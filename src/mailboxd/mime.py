"""Message analysis: headers, authentication results, parts, HTML safety.

Everything here works on an already-fetched `email.message.Message` and
never talks to a server.
"""

from __future__ import annotations

import hashlib
import ipaddress
import re
from email.header import decode_header, make_header
from email.message import Message
from email.utils import getaddresses, parseaddr
from html.parser import HTMLParser
from typing import Any, Iterator
from urllib.parse import urlsplit


def decode(s: Any) -> str:
    if s is None:
        return ""
    if isinstance(s, bytes):
        try:
            s = s.decode("utf-8")
        except UnicodeDecodeError:
            s = s.decode("latin-1", errors="replace")
    try:
        return str(make_header(decode_header(str(s))))
    except Exception:
        return str(s)


def all_headers(msg: Message) -> list[list[str]]:
    """Every header, in wire order, decoded. Duplicates (Received, ...) are kept."""
    return [[k, decode(v)] for k, v in msg.items()]


def domain_of(addr: str) -> str:
    _, a = parseaddr(addr or "")
    return a.rsplit("@", 1)[-1].strip().lower().rstrip(">") if "@" in a else ""


def org_domain(domain: str) -> str:
    """Rough organizational domain (last two labels, three for short ccTLD SLDs)."""
    labels = [x for x in domain.lower().strip(".").split(".") if x]
    if len(labels) <= 2:
        return ".".join(labels)
    if len(labels[-2]) <= 3 and len(labels[-1]) == 2:  # co.uk, com.au, gouv.fr ...
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def message_ids(value: str | None) -> list[str]:
    return re.findall(r"<[^<>\s]+>", value or "")


# ── Authentication-Results (RFC 8601) ────────────────────────────────────


def _strip_comments(value: str) -> str:
    out, depth = [], 0
    for ch in value:
        if ch == "(":
            depth += 1
        elif ch == ")" and depth:
            depth -= 1
        elif not depth:
            out.append(ch)
    return "".join(out)


def _parse_one_ar(value: str) -> tuple[str, list[dict[str, str]]]:
    parts = [p.strip() for p in _strip_comments(value).replace("\r\n", " ").split(";")]
    authserv = parts[0].split()[0].lower() if parts and parts[0] else ""
    results: list[dict[str, str]] = []
    for p in parts[1:]:
        tokens = p.split()
        if not tokens or "=" not in tokens[0]:
            continue
        method, _, result = tokens[0].partition("=")
        entry = {"method": method.lower(), "result": result.lower()}
        for t in tokens[1:]:
            k, sep, v = t.partition("=")
            if sep:
                entry[k.lower()] = v.strip('"')
        results.append(entry)
    return authserv, results


def parse_authentication_results(msg: Message, trusted_authserv_id: str | None) -> dict[str, Any]:
    """Summarise SPF / DKIM / DMARC / ARC verdicts.

    Anyone can put an Authentication-Results header in a message they send,
    so only headers stamped by our own receiving server count. With
    `trusted_authserv_id` set, only matching headers are used (`trusted`
    true). Without it the topmost header (the one added last, normally by
    the receiving server) is used and `trusted` is false.
    """
    raw = [decode(v) for v in msg.get_all("Authentication-Results") or []]
    chosen: list[tuple[str, list[dict[str, str]]]] = []
    trusted = False
    parsed = [_parse_one_ar(v) for v in raw]
    if trusted_authserv_id:
        want = trusted_authserv_id.lower()
        chosen = [p for p in parsed if p[0] == want]
        trusted = bool(chosen)
    elif parsed:
        chosen = parsed[:1]

    spf: dict[str, str] | None = None
    dmarc: dict[str, str] | None = None
    arc: dict[str, str] | None = None
    dkim: list[dict[str, str]] = []
    for _, results in chosen:
        for r in results:
            m = r["method"]
            if m == "spf" and spf is None:
                mf = r.get("smtp.mailfrom", "")
                spf = {"result": r["result"], "domain": mf.rsplit("@", 1)[-1].lower()}
            elif m == "dkim":
                dkim.append(
                    {
                        "result": r["result"],
                        "domain": r.get("header.d", r.get("header.i", "").lstrip("@")).lower(),
                        "selector": r.get("header.s", ""),
                    }
                )
            elif m == "dmarc" and dmarc is None:
                dmarc = {"result": r["result"], "domain": r.get("header.from", "").lower()}
            elif m == "arc" and arc is None:
                arc = {"result": r["result"]}

    return {
        "trusted": trusted,
        "authserv_id": chosen[0][0] if chosen else None,
        "spf": spf,
        "dkim": dkim,
        "dmarc": dmarc,
        "arc": arc,
        "raw": raw,
    }


def security_signals(msg: Message, auth: dict[str, Any]) -> list[str]:
    """Cheap phishing tells derived from headers and the authentication verdicts."""
    signals: list[str] = []
    from_raw = decode(msg.get("From"))
    from_name, from_addr = parseaddr(from_raw)
    from_dom = domain_of(from_raw)

    reply_to = decode(msg.get("Reply-To"))
    if reply_to and from_dom:
        rt_doms = {domain_of(a) for _, a in getaddresses([reply_to]) if "@" in a}
        if any(org_domain(d) != org_domain(from_dom) for d in rt_doms if d):
            signals.append("reply_to_domain_mismatch")

    return_path = decode(msg.get("Return-Path"))
    rp_dom = domain_of(return_path)
    if rp_dom and from_dom and org_domain(rp_dom) != org_domain(from_dom):
        signals.append("return_path_domain_mismatch")

    embedded = re.findall(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+", from_name or "")
    if any(e.lower() != from_addr.lower() for e in embedded):
        signals.append("display_name_spoof")

    if not auth.get("raw"):
        signals.append("no_authentication_results")
    elif not auth.get("trusted"):
        signals.append("authentication_untrusted")

    spf = auth.get("spf") or {}
    if spf.get("result") in ("fail", "softfail", "permerror"):
        signals.append(f"spf_{spf['result']}")
    dmarc = auth.get("dmarc") or {}
    if dmarc.get("result") in ("fail", "permerror"):
        signals.append("dmarc_fail")
    dkim = auth.get("dkim") or []
    if dkim and not any(d["result"] == "pass" for d in dkim):
        signals.append("dkim_fail")
    if from_dom and dkim and not any(
        d["result"] == "pass" and d["domain"] and org_domain(d["domain"]) == org_domain(from_dom)
        for d in dkim
    ):
        signals.append("dkim_not_aligned")
    return signals


# ── Parts and attachments ────────────────────────────────────────────────


def iter_parts(msg: Message) -> Iterator[tuple[str, Message]]:
    """Yield (part_id, part) for every leaf part. part_id is its walk() index."""
    for i, part in enumerate(msg.walk()):
        if part.is_multipart():
            continue
        yield str(i), part


def is_attachment(part: Message) -> bool:
    disp = (part.get("Content-Disposition") or "").lower()
    if disp.startswith("attachment"):
        return True
    if part.get_filename() and not part.get_content_type().startswith("text/"):
        return True
    return disp.startswith("inline") and bool(part.get_filename())


def attachment_info(part_id: str, part: Message) -> dict[str, Any]:
    payload = part.get_payload(decode=True)
    data = payload if isinstance(payload, bytes) else b""
    return {
        "part_id": part_id,
        "filename": safe_filename(decode(part.get_filename() or "")) or f"part-{part_id}",
        "content_type": part.get_content_type(),
        "size": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "inline": (part.get("Content-Disposition") or "").lower().startswith("inline"),
    }


def get_part(msg: Message, part_id: str) -> Message | None:
    for pid, part in iter_parts(msg):
        if pid == part_id:
            return part
    return None


def safe_filename(name: str) -> str:
    name = name.replace("\\", "/").rsplit("/", 1)[-1]
    name = re.sub(r"[\x00-\x1f\x7f\"<>:|?*]", "_", name).strip(" .")
    return name[:200]


# ── HTML safety ──────────────────────────────────────────────────────────

_HIDDEN_STYLE = re.compile(
    r"display\s*:\s*none|visibility\s*:\s*hidden|font-size\s*:\s*0(?![.\d]*[1-9])"
    r"|opacity\s*:\s*0(?:\.0+)?\s*(?:;|$)|max-height\s*:\s*0"
    r"|height\s*:\s*0(?:px)?\s*;.*overflow\s*:\s*hidden",
    re.I,
)
_COLOR = re.compile(r"(?<![-\w])color\s*:\s*([^;]+)", re.I)
_BG = re.compile(r"background(?:-color)?\s*:\s*([^;]+)", re.I)
_URLISH = re.compile(r"(?:https?://)?(?:[\w-]+\.)+[a-z]{2,}(?:/\S*)?", re.I)
_SHORTENERS = {
    "bit.ly",
    "tinyurl.com",
    "t.co",
    "goo.gl",
    "ow.ly",
    "is.gd",
    "buff.ly",
    "rebrand.ly",
    "cutt.ly",
    "shorturl.at",
    "rb.gy",
}
_VOID = {"br", "img", "hr", "meta", "link", "input", "area", "base", "col", "source", "wbr"}


def _norm_color(v: str) -> str:
    return v.strip().lower().replace(" ", "").replace("!important", "")


class _Analyzer(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[tuple[str, bool]] = []
        self.hidden: list[str] = []
        self._hidden_buf: list[str] = []
        self.links: list[dict[str, str]] = []
        self._link: dict[str, Any] | None = None

    def _hidden_now(self) -> bool:
        return any(h for _, h in self.stack)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {k.lower(): (v or "") for k, v in attrs}
        style = a.get("style", "")
        hidden = "hidden" in a or bool(_HIDDEN_STYLE.search(style))
        fg, bg = _COLOR.search(style), _BG.search(style)
        if fg and bg and _norm_color(fg.group(1)) == _norm_color(bg.group(1)):
            hidden = True
        if tag in ("style", "script", "title", "head"):
            hidden = False  # not displayed, but not a hiding trick either
        if tag == "a":
            self._link = {"href": a.get("href", ""), "text": []}
        if tag not in _VOID:
            self.stack.append((tag, hidden))

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._link is not None:
            self.links.append(
                {"href": self._link["href"], "text": " ".join("".join(self._link["text"]).split())}
            )
            self._link = None
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                was_hidden = self._hidden_now()
                del self.stack[i:]
                if was_hidden and not self._hidden_now():
                    self._flush_hidden()
                return

    def _flush_hidden(self) -> None:
        text = " ".join("".join(self._hidden_buf).split())
        if text:
            self.hidden.append(text)
        self._hidden_buf = []

    def handle_data(self, data: str) -> None:
        if any(t in ("style", "script", "title", "head") for t, _ in self.stack):
            return
        if self._hidden_now():
            self._hidden_buf.append(data)
        if self._link is not None:
            self._link["text"].append(data)

    def close(self) -> None:
        super().close()
        self._flush_hidden()


def _host(url: str) -> str:
    try:
        return (urlsplit(url if "://" in url else "http://" + url).hostname or "").lower()
    except ValueError:
        return ""


def analyze_html(html: str) -> dict[str, Any]:
    """Find text hidden from the human reader and links that lie about their target."""
    p = _Analyzer()
    try:
        p.feed(html)
        p.close()
    except Exception:  # malformed markup: report what we have
        pass
    suspicious: list[dict[str, Any]] = []
    for link in p.links:
        href, text = link["href"].strip(), link["text"]
        reasons: list[str] = []
        lower = href.lower()
        if lower.startswith(("javascript:", "data:", "vbscript:")):
            reasons.append("script_or_data_url")
        host = _host(href) if lower.startswith(("http://", "https://", "//")) else ""
        if host:
            try:
                ipaddress.ip_address(host.strip("[]"))
                reasons.append("ip_literal")
            except ValueError:
                pass
            if any(lbl.startswith("xn--") for lbl in host.split(".")):
                reasons.append("punycode")
            if host in _SHORTENERS:
                reasons.append("url_shortener")
            if "@" in (urlsplit(href).netloc or ""):
                reasons.append("userinfo_in_url")
            m = _URLISH.fullmatch(text.strip()) if text else None
            if m:
                shown = _host(text.strip())
                if shown and org_domain(shown) != org_domain(host):
                    reasons.append("text_domain_mismatch")
        if reasons:
            suspicious.append({"href": href, "text": text, "reasons": reasons})
    return {"hidden_text": p.hidden, "suspicious_links": suspicious, "link_count": len(p.links)}


def sanitize_html(html: str) -> str:
    """Allow-list sanitising; remote images are neutralised (tracking pixels)."""
    import nh3

    tags = set(nh3.ALLOWED_TAGS) | {"span", "div", "font", "center"}
    tags -= {"img"}
    attrs = {k: set(v) for k, v in nh3.ALLOWED_ATTRIBUTES.items()}
    attrs["a"] = {"href", "title"}
    return nh3.clean(
        html,
        tags=tags,
        attributes=attrs,
        url_schemes={"http", "https", "mailto"},
        link_rel="noopener noreferrer nofollow",
        strip_comments=True,
    )
