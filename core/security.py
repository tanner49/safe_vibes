import ipaddress
from html.parser import HTMLParser
from urllib.parse import urlparse

from django.core.exceptions import PermissionDenied


DEFAULT_REPORT_URL_WHITELIST = [
    "cdn.jsdelivr.net",
    "cdnjs.cloudflare.com",
    "unpkg.com",
    "esm.sh",
    "cdn.skypack.dev",
    "ga.jspm.io",
    "jspm.dev",
    "ajax.googleapis.com",
    "ajax.aspnetcdn.com",
    "code.jquery.com",
    "stackpath.bootstrapcdn.com",
    "cdn.datatables.net",
    "d3js.org",
]

URL_ATTRS = {"src", "href", "action", "poster"}
SRCSET_ATTRS = {"srcset"}


def split_policy_lines(value):
    return [
        line.strip()
        for line in (value or "").replace(",", "\n").splitlines()
        if line.strip()
    ]


def normalize_domain(value):
    value = value.strip().lower()
    if "://" in value:
        parsed = urlparse(value)
        value = parsed.netloc or parsed.path
    value = value.split("/", 1)[0].split(":", 1)[0].strip(".")
    if value.startswith("*."):
        value = value[2:]
    return value


def domain_matches(hostname, policy_domain):
    hostname = normalize_domain(hostname or "")
    policy_domain = normalize_domain(policy_domain)
    return hostname == policy_domain or hostname.endswith(f".{policy_domain}")


def url_hostname(url):
    parsed = urlparse(url)
    if parsed.scheme in {"http", "https"}:
        return parsed.hostname or ""
    if url.startswith("//"):
        return urlparse(f"https:{url}").hostname or ""
    return ""


def external_url_allowed(organization, url):
    hostname = url_hostname(url)
    if not hostname:
        return True
    blacklist = split_policy_lines(organization.report_url_blacklist)
    if organization.report_url_blacklist_enabled and any(
        domain_matches(hostname, domain) for domain in blacklist
    ):
        return False
    whitelist = split_policy_lines(organization.report_url_whitelist)
    if organization.report_url_whitelist_enabled:
        return any(domain_matches(hostname, domain) for domain in whitelist)
    return True


def csp_source_for_domain(domain):
    normalized = normalize_domain(domain)
    return f"https://{normalized} https://*.{normalized}"


def report_csp(organization):
    sources = ["'self'"]
    if organization.report_url_whitelist_enabled:
        sources.extend(
            csp_source_for_domain(domain)
            for domain in split_policy_lines(organization.report_url_whitelist)
        )
    else:
        sources.extend(["https:", "data:", "blob:"])
    source_text = " ".join(sources)
    return (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' 'unsafe-eval' "
        f"{source_text}; "
        f"connect-src {source_text}; "
        f"img-src {source_text} data: blob:; "
        f"style-src 'self' 'unsafe-inline' {source_text}; "
        f"font-src {source_text} data:; "
        "frame-ancestors 'self'; base-uri 'none'; form-action 'self'"
    )


def client_ip_from_request(request):
    forwarded_for = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if forwarded_for:
        return forwarded_for.split(",", 1)[0].strip()
    return request.META.get("REMOTE_ADDR", "")


def ip_allowed(ip_value, allowed_ranges):
    try:
        ip = ipaddress.ip_address(ip_value)
    except ValueError:
        return False
    for entry in split_policy_lines(allowed_ranges):
        try:
            if "/" in entry:
                if ip in ipaddress.ip_network(entry, strict=False):
                    return True
            elif ip == ipaddress.ip_address(entry):
                return True
        except ValueError:
            continue
    return False


def enforce_report_ip_policy(request, organization):
    if not organization.report_ip_allowlist_enabled:
        return
    if not ip_allowed(client_ip_from_request(request), organization.report_ip_allowlist):
        raise PermissionDenied("Your network is not allowed to access reports for this organization.")


class ReportHtmlUrlSanitizer(HTMLParser):
    def __init__(self, organization):
        super().__init__(convert_charrefs=False)
        self.organization = organization
        self.parts = []

    def handle_starttag(self, tag, attrs):
        self.parts.append(self.render_tag(tag, attrs, closed=False))

    def handle_startendtag(self, tag, attrs):
        self.parts.append(self.render_tag(tag, attrs, closed=True))

    def handle_endtag(self, tag):
        self.parts.append(f"</{tag}>")

    def handle_data(self, data):
        self.parts.append(data)

    def handle_entityref(self, name):
        self.parts.append(f"&{name};")

    def handle_charref(self, name):
        self.parts.append(f"&#{name};")

    def handle_comment(self, data):
        self.parts.append(f"<!--{data}-->")

    def render_tag(self, tag, attrs, closed=False):
        sanitized_attrs = []
        for name, value in attrs:
            if value is not None and name.lower() in URL_ATTRS and not external_url_allowed(
                self.organization,
                value,
            ):
                sanitized_attrs.append((f"data-blocked-{name}", value))
                continue
            if value is not None and name.lower() in SRCSET_ATTRS:
                sanitized_attrs.append((name, sanitize_srcset(self.organization, value)))
                continue
            sanitized_attrs.append((name, value))
        attr_text = "".join(format_attr(name, value) for name, value in sanitized_attrs)
        suffix = " /" if closed else ""
        return f"<{tag}{attr_text}{suffix}>"


def format_attr(name, value):
    if value is None:
        return f" {name}"
    escaped = (
        str(value)
        .replace("&", "&amp;")
        .replace('"', "&quot;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )
    return f' {name}="{escaped}"'


def sanitize_srcset(organization, value):
    candidates = []
    for candidate in value.split(","):
        candidate = candidate.strip()
        if not candidate:
            continue
        url = candidate.split(None, 1)[0]
        if external_url_allowed(organization, url):
            candidates.append(candidate)
    return ", ".join(candidates)


def sanitize_report_html_urls(organization, html):
    parser = ReportHtmlUrlSanitizer(organization)
    parser.feed(html or "")
    parser.close()
    return "".join(parser.parts)
