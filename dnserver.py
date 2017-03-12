#!/usr/bin/env python3.6
import json
import logging
import os
import signal
from datetime import datetime
from pathlib import Path
from textwrap import wrap
from time import sleep

from dnslib import DNSLabel, QTYPE, RR, dns
from dnslib.proxy import ProxyResolver
from dnslib.server import DNSServer
from cachetools import TTLCache
import pprint

cache = TTLCache(maxsize=256, ttl=60)

SERIAL_NO = int((datetime.utcnow() - datetime(1970, 1, 1)).total_seconds())

handler = logging.StreamHandler()
handler.setLevel(logging.INFO)
handler.setFormatter(logging.Formatter('%(asctime)s: %(message)s', datefmt='%H:%M:%S'))

logger = logging.getLogger(__name__)
logger.addHandler(handler)
logger.setLevel(logging.INFO)

TYPE_LOOKUP = {
    'A': (dns.A, QTYPE.A),
    'AAAA': (dns.AAAA, QTYPE.AAAA),
    'CAA': (dns.CAA, QTYPE.CAA),
    'CNAME': (dns.CNAME, QTYPE.CNAME),
    'DNSKEY': (dns.DNSKEY, QTYPE.DNSKEY),
    'MX': (dns.MX, QTYPE.MX),
    'NAPTR': (dns.NAPTR, QTYPE.NAPTR),
    'NS': (dns.NS, QTYPE.NS),
    'PTR': (dns.PTR, QTYPE.PTR),
    'RRSIG': (dns.RRSIG, QTYPE.RRSIG),
    'SOA': (dns.SOA, QTYPE.SOA),
    'SRV': (dns.SRV, QTYPE.SRV),
    'TXT': (dns.TXT, QTYPE.TXT),
    'SPF': (dns.TXT, QTYPE.TXT),
}


class Record:
    def __init__(self, rname, rtype, args):
        self._rname = DNSLabel(rname)

        rd_cls, self._rtype = TYPE_LOOKUP[rtype]

        if self._rtype == QTYPE.SOA and len(args) == 2:
            # add sensible times to SOA
            args += (SERIAL_NO, 3600, 3600 * 3, 3600 * 24, 3600),

        if self._rtype == QTYPE.TXT and len(args) == 1 and isinstance(args[0], str) and len(args[0]) > 255:
            # wrap long TXT records as per dnslib's docs.
            args = wrap(args[0], 255),

        if self._rtype in (QTYPE.NS, QTYPE.SOA):
            ttl = 3600 * 24
        else:
            ttl = 300

        self.rr = RR(
            rname=self._rname,
            rtype=self._rtype,
            rdata=rd_cls(*args),
            ttl=ttl,
        )

    def match(self, q):
        return q.qname == self._rname and (q.qtype == QTYPE.ANY or q.qtype == self._rtype)

    def sub_match(self, q):
        return self._rtype == QTYPE.SOA and q.qname.matchSuffix(self._rname)

    def sub_match_ns(self, q):
        return self._rtype == QTYPE.NS and q.qname.matchSuffix(self._rname)

    def __str__(self):
        return str(self.rr)


class Resolver(ProxyResolver):
    def __init__(self, nameservers, zone_file):
        # self.upstream = upstream
        # super().__init__(nameservers[0], 53, 5)
        self.nameservers = nameservers
        self.records = self.load_zones(zone_file)

    def zone_lines(self):
        current_line = ''
        for line in zone_file.open():
            if line.startswith('#'):
                continue
            line = line.rstrip('\r\n\t ')
            if not line.startswith(' ') and current_line:
                yield current_line
                current_line = ''
            current_line += line.lstrip('\r\n\t ')
        if current_line:
            yield current_line

    def load_zones(self, zone_file):
        assert zone_file.exists(), f'zone files "{zone_file}" does not exist'
        logger.info('loading zone file "%s":', zone_file)
        zones = []
        for line in self.zone_lines():
            try:
                rname, rtype, args_ = line.split(maxsplit=2)

                if args_.startswith('['):
                    args = tuple(json.loads(args_))
                else:
                    args = (args_,)
                record = Record(rname, rtype, args)
                zones.append(record)
                logger.info(' %2d: %s', len(zones), record)
            except Exception as e:
                raise RuntimeError(f'Error processing line ({e.__class__.__name__}: {e}) "{line.strip()}"') from e
        logger.info('%d zone resource records generated from zone file', len(zones))
        return zones

    def resolve(self, request, handler):
        type_name = QTYPE[request.q.qtype]
        #reply = request.reply()

        # logger.info('no local zone found, proxying %s[%s]', request.q.qname, type_name)
        # pprint.pprint(request.header.id)
        
        #Cache nameservers
        upstream = self.nameservers[0]

        try:
            hit = cache[request.q.qname]
            upstream = hit
            logger.info('cache hit upstream %s %s[%s]', upstream, request.q.qname, type_name)
            super().__init__(upstream, 53, 5)
            response = super().resolve(request, handler)
        except KeyError:
            logger.info('cache miss starting from the top %s[%s]', request.q.qname, type_name)
            for upstream in self.nameservers:
                super().__init__(upstream, 53, 5)
                response = super().resolve(request, handler)
                logger.info('upstream %s header code %s', upstream, response.header.rcode)
                if response.header.rcode == 0:
                    break

            if response.header.rcode == 0:
                cache[request.q.qname] = upstream

        return response


def handle_sig(signum, frame):
    logger.info('pid=%d, got signal: %s, stopping...', os.getpid(), signal.Signals(signum).name)
    exit(0)


if __name__ == '__main__':
    signal.signal(signal.SIGTERM, handle_sig)

    port = int(os.getenv('PORT', 53))
    upstream = os.getenv('UPSTREAM', ['ns1.cmslauncher.com', 'ns1.cmslauncher.co.uk', 'ns1.cmslauncher.asia'])
    zone_file = Path(os.getenv('ZONE_FILE', '/zones/zones.txt'))
    resolver = Resolver(upstream, zone_file)
    udp_server = DNSServer(resolver, port=port)
    tcp_server = DNSServer(resolver, port=port, tcp=True)

    logger.info('starting DNS server on port %d, upstream DNS server "%s"', port, upstream)
    udp_server.start_thread()
    tcp_server.start_thread()

    try:
        while udp_server.isAlive():
            sleep(1)
    except KeyboardInterrupt:
        pass