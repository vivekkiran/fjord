from functools import wraps
from hashlib import md5
import re
import urlparse

from elasticsearch.exceptions import ElasticsearchException
from ratelimit.helpers import is_ratelimited
from statsd import statsd

from django.utils.encoding import force_str

from fjord.feedback import config
from fjord.search.index import es_analyze


def actual_ip(req):
    """Returns the actual ip address

    Our dev, stage and prod servers are behind a reverse proxy, so the ip
    address in REMOTE_ADDR is the reverse proxy server and not the client
    ip address. The actual client ip address is in HTTP_X_CLUSTER_CLIENT_IP.

    In our local development and test environments, the client ip address
    is in REMOTE_ADDR.

    """
    return req.META.get('HTTP_X_CLUSTER_CLIENT_IP', req.META['REMOTE_ADDR'])


def actual_ip_plus_desc(req):
    """Returns actual ip address plus first 30 characters of desc

    This key is formulated to reduce double-submits.

    """
    # This pulls out the description and make sure it's bytes.
    desc = force_str(req.POST.get('description', u'no description'))

    # md5 hash that.
    hasher = md5()
    hasher.update(desc)
    desc = hasher.hexdigest()

    # Then return the ip address plus a : plus the desc md5 hash.
    return actual_ip(req) + ':' + desc


def ratelimit(rulename, keyfun=None, rate='5/m'):
    """Rate-limiting decorator that keeps metrics via statsd

    This is just like the django-ratelimit ratelimit decorator, but is
    stacking-friendly, performs some statsd fancypants and also has
    Fjord-friendly defaults.

    :arg rulename: rulename for statsd logging---must be a string
        with letters only! look for this in statsd under
        "throttled." + rulename.
    :arg keyfun: (optional) function to generate a key for this
        throttling. defaults to actual_ip.
    :arg rate: (optional) rate to throttle at. defaults to 5/m.

    """
    if keyfun is None:
        keyfun = actual_ip

    def decorator(fn):
        @wraps(fn)
        def _wrapped(request, *args, **kwargs):
            already_limited = getattr(request, 'limited', False)
            ratelimited = is_ratelimited(
                request=request, increment=True, ip=False, method=['POST'],
                field=None, rate=rate, keys=keyfun)

            if not already_limited and ratelimited:
                statsd.incr('throttled.' + rulename)

            return fn(request, *args, **kwargs)
        return _wrapped
    return decorator


TOKEN_SPLIT_RE = re.compile(r'[\s\.\,\/\\\?\;\:\"\*\&\^\%\$\#\@\!]+')


def tokenize(text):
    """Tokenizes the text

    1. lowercases text
    2. throws out all non-alpha-characters
    3. nixes all stop words

    """
    # Lowercase the text
    text = text.lower()

    # Nix all non-word characters
    tokens = TOKEN_SPLIT_RE.split(text)

    # Nix all stopwords and one-letter characters
    tokens = [token for token in tokens
              if (token not in config.ANALYSIS_STOPWORDS
                  and len(token) > 1)]

    # Return whatever we have left
    return tokens


def compute_grams(text):
    """Computes bigrams from analyzed text

    :arg text: text to analyze and generate bigrams from

    :returns: list of bigrams

    >>> compute_grams(u'The quick brown fox jumped')
    [u'quick brown', u'brown fox', u'fox jumped']

    """
    if not text:
        return []

    tokens = tokenize(text)

    # Generate set of bigrams. A bigram is a set of two consecutive
    # tokens. We put them in a set because we don't want duplicates.
    # We sort them so that "youtube crash" will match "crash youtube".
    bigrams = set()
    if len(tokens) >= 2:
        for i in range(len(tokens) - 1):
            bigrams.add(u' '.join(
                sorted([tokens[i], tokens[i+1]])))

    return list(bigrams)


def clean_url(url):
    """Takes a user-supplied url and cleans bits out

    This removes:

    1. nixes any non http/https/chrome/about urls
    2. port numbers
    3. query string variables
    4. hashes

    """
    if not url:
        return url

    # Don't mess with about: urls.
    if url.startswith('about:'):
        return url

    parsed = urlparse.urlparse(url)

    if parsed.scheme not in ('http', 'https', 'chrome'):
        return u''

    # Rebuild url to drop querystrings, hashes, etc
    new_url = (parsed.scheme, parsed.hostname, parsed.path, None, None, None)

    return urlparse.urlunparse(new_url)
