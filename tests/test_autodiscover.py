# coding=utf-8
from collections import namedtuple
import glob
import os
import tempfile
from types import MethodType
import warnings

import dns
import requests_mock

import exchangelib.autodiscover.discovery
from exchangelib import Credentials, NTLM, FailFast, Configuration, Account
from exchangelib.autodiscover import close_connections, clear_cache, autodiscover_cache, AutodiscoverProtocol, \
    Autodiscovery
from exchangelib.autodiscover.properties import Autodiscover
from exchangelib.errors import ErrorNonExistentMailbox, AutoDiscoverCircularRedirect, AutoDiscoverFailed
from exchangelib.protocol import BaseProtocol, NoVerifyHTTPAdapter, FaultTolerance
from exchangelib.util import get_domain
from .common import EWSTest


class AutodiscoverTest(EWSTest):
    def setUp(self):
        super(AutodiscoverTest, self).setUp()
        # Enable fault tolerance
        exchangelib.autodiscover.discovery.Autodiscovery.INITIAL_RETRY_POLICY = FaultTolerance(max_wait=10)
        exchangelib.autodiscover.discovery.Autodiscovery.RETRY_WAIT = 2
        # Each test should start with a clean autodiscover cache
        clear_cache()

    def test_magic(self):
        # Just test we don't fail when calling repr() and str()
        exchangelib.autodiscover.discovery.discover(
            email=self.account.primary_smtp_address,
            credentials=self.account.protocol.credentials,
            retry_policy=self.retry_policy,
        )
        self.assertEqual(len(autodiscover_cache), 1)
        str(autodiscover_cache)
        repr(autodiscover_cache)
        for protocol in autodiscover_cache._protocols.values():
            str(protocol)
            repr(protocol)

    def test_autodiscover(self):
        ad_response, protocol = exchangelib.autodiscover.discovery.discover(
            email=self.account.primary_smtp_address,
            credentials=self.account.protocol.credentials,
            retry_policy=self.retry_policy,
        )
        self.assertEqual(ad_response.autodiscover_smtp_address, self.account.primary_smtp_address)
        self.assertEqual(protocol.service_endpoint.lower(), self.account.protocol.service_endpoint.lower())
        self.assertEqual(protocol.version.build, self.account.protocol.version.build)

    def test_autodiscover_failure(self):
        # Test that error is raised with an empty cache
        self.assertEqual(len(autodiscover_cache), 0)
        with self.assertRaises(ErrorNonExistentMailbox):
            exchangelib.autodiscover.discovery.discover(
                email='XXX.' + self.account.primary_smtp_address,
                credentials=self.account.protocol.credentials,
                retry_policy=self.retry_policy,
            )
        self.assertEqual(len(autodiscover_cache), 1)
        # Test that error is raised with a non-empty cache
        with self.assertRaises(ErrorNonExistentMailbox):
            exchangelib.autodiscover.discovery.discover(
                email='XXX.' + self.account.primary_smtp_address,
                credentials=self.account.protocol.credentials,
                retry_policy=self.retry_policy,
            )

    def test_close_autodiscover_connections(self):
        exchangelib.autodiscover.discovery.discover(
            email=self.account.primary_smtp_address,
            credentials=self.account.protocol.credentials,
            retry_policy=self.retry_policy,
        )
        self.assertEqual(len(autodiscover_cache), 1)
        close_connections()

    def test_autodiscover_direct_gc(self):
        # This is what Python garbage collection does
        exchangelib.autodiscover.discovery.discover(
            email=self.account.primary_smtp_address,
            credentials=self.account.protocol.credentials,
            retry_policy=self.retry_policy,
        )
        self.assertEqual(len(autodiscover_cache), 1)
        autodiscover_cache.__del__()

    @requests_mock.mock(real_http=True)
    def testautodiscover_cache(self, m):
        # Empty the cache
        autodiscover_cache.clear()
        discovery = Autodiscovery(
            email=self.account.primary_smtp_address,
            credentials=self.account.protocol.credentials,
            retry_policy=self.retry_policy,
        )
        # Not cached
        self.assertNotIn(discovery._cache_key, autodiscover_cache)
        discovery.discover()
        # Now it's cached
        self.assertIn(discovery._cache_key, autodiscover_cache)
        # Make sure the cache can be looked by value, not by id(). This is important for multi-threading/processing
        self.assertIn((
            self.account.primary_smtp_address.split('@')[1],
            Credentials(self.account.protocol.credentials.username, self.account.protocol.credentials.password),
            True
        ), autodiscover_cache)
        # Poison the cache with a failing autodiscover endpoint. discover() must handle this and rebuild the cache
        autodiscover_cache[discovery._cache_key] = AutodiscoverProtocol(config=Configuration(
            service_endpoint='https://example.com/Autodiscover.xml',
            credentials=Credentials('leet_user', 'cannaguess'),
            auth_type=NTLM,
            retry_policy=FailFast(),
        ))
        m.post('https://example.com/Autodiscover.xml', status_code=404)
        discovery.discover()
        self.assertIn(discovery._cache_key, autodiscover_cache)

        # Make sure that the cache is actually used on the second call to discover()
        _orig = discovery._step_1

        def _mock(slf, *args, **kwargs):
            raise NotImplementedError()

        discovery._step_1 = MethodType(_mock, discovery)
        discovery.discover()

        # Fake that another thread added the cache entry into the persistent storage but we don't have it in our
        # in-memory cache. The cache should work anyway.
        autodiscover_cache._protocols.clear()
        discovery.discover()
        discovery._step_1 = _orig

        # Make sure we can delete cache entries even though we don't have it in our in-memory cache
        autodiscover_cache._protocols.clear()
        del autodiscover_cache[discovery._cache_key]
        # This should also work if the cache does not contain the entry anymore
        del autodiscover_cache[discovery._cache_key]

    def test_corrupt_autodiscover_cache(self):
        # Insert a fake Protocol instance into the cache
        key = (2, 'foo', 4)
        autodiscover_cache[key] = namedtuple('P', ['service_endpoint', 'auth_type', 'retry_policy'])(1, 'bar', 'baz')
        # Check that it exists. 'in' goes directly to the file
        self.assertTrue(key in autodiscover_cache)
        # Destroy the backing cache file(s)
        for db_file in glob.glob(autodiscover_cache._storage_file + '*'):
            with open(db_file, 'w') as f:
                f.write('XXX')
        # Check that we can recover from a destroyed file and that the entry no longer exists
        self.assertFalse(key in autodiscover_cache)

    def test_autodiscover_from_account(self):
        self.assertEqual(len(autodiscover_cache), 0)
        account = Account(
            primary_smtp_address=self.account.primary_smtp_address,
            config=Configuration(
                credentials=self.account.protocol.credentials,
                retry_policy=self.retry_policy,
            ),
            autodiscover=True,
            locale='da_DK',
        )
        self.assertEqual(account.primary_smtp_address, self.account.primary_smtp_address)
        self.assertEqual(account.protocol.service_endpoint.lower(), self.account.protocol.service_endpoint.lower())
        self.assertEqual(account.protocol.version.build, self.account.protocol.version.build)
        # Make sure cache is full
        self.assertEqual(len(autodiscover_cache), 1)
        self.assertTrue((account.domain, self.account.protocol.credentials, True) in autodiscover_cache)
        # Test that autodiscover works with a full cache
        account = Account(
            primary_smtp_address=self.account.primary_smtp_address,
            config=Configuration(
                credentials=self.account.protocol.credentials,
                retry_policy=self.retry_policy,
            ),
            autodiscover=True,
            locale='da_DK',
        )
        self.assertEqual(account.primary_smtp_address, self.account.primary_smtp_address)
        # Test cache manipulation
        key = (account.domain, self.account.protocol.credentials, True)
        self.assertTrue(key in autodiscover_cache)
        del autodiscover_cache[key]
        self.assertFalse(key in autodiscover_cache)

    @requests_mock.mock(real_http=True)
    def test_autodiscover_redirect(self, m):
        # Prime the cache
        discovery = Autodiscovery(
            email=self.account.primary_smtp_address,
            credentials=self.account.protocol.credentials,
            retry_policy=self.retry_policy,
        )
        discovery.discover()
        ad_endpoint = autodiscover_cache[discovery._cache_key].service_endpoint

        # Make sure we discover a different return address
        m.post(ad_endpoint, status_code=200, content=b'''\
<?xml version="1.0" encoding="utf-8"?>
<Autodiscover xmlns="http://schemas.microsoft.com/exchange/autodiscover/responseschema/2006">
    <Response xmlns="http://schemas.microsoft.com/exchange/autodiscover/outlook/responseschema/2006a">
        <User>
            <AutoDiscoverSMTPAddress>john@example.com</AutoDiscoverSMTPAddress>
        </User>
        <Account>
            <AccountType>email</AccountType>
            <Action>settings</Action>
            <Protocol>
                <Type>EXPR</Type>
                <EwsUrl>https://expr.example.com/EWS/Exchange.asmx</EwsUrl>
            </Protocol>
        </Account>
    </Response>
</Autodiscover>''')
        # Also mock the EWS URL. We try to guess its auth method as part of autodiscovery
        m.post('https://expr.example.com/EWS/Exchange.asmx', status_code=200)
        ad_response, p = discovery.discover()
        self.assertEqual(ad_response.autodiscover_smtp_address, 'john@example.com')

        # Make sure we discover an address redirect to the same domain. We have to mock the same URL with two different
        # responses. We do that with a response list.
        domain = get_domain(discovery.email)
        redirect_addr_content = b'''\
<?xml version="1.0" encoding="utf-8"?>
<Autodiscover xmlns="http://schemas.microsoft.com/exchange/autodiscover/responseschema/2006">
    <Response xmlns="http://schemas.microsoft.com/exchange/autodiscover/outlook/responseschema/2006a">
        <Account>
            <Action>redirectAddr</Action>
            <RedirectAddr>redirect_me@%s</RedirectAddr>
        </Account>
    </Response>
</Autodiscover>''' % domain.encode()
        settings_content = b'''\
<?xml version="1.0" encoding="utf-8"?>
<Autodiscover xmlns="http://schemas.microsoft.com/exchange/autodiscover/responseschema/2006">
    <Response xmlns="http://schemas.microsoft.com/exchange/autodiscover/outlook/responseschema/2006a">
        <User>
            <AutoDiscoverSMTPAddress>redirected@%s</AutoDiscoverSMTPAddress>
        </User>
        <Account>
            <AccountType>email</AccountType>
            <Action>settings</Action>
            <Protocol>
                <Type>EXPR</Type>
                <EwsUrl>https://redirected.%s/EWS/Exchange.asmx</EwsUrl>
            </Protocol>
        </Account>
    </Response>
</Autodiscover>''' % (domain.encode(), domain.encode())
        # Also mock the EWS URL. We try to guess its auth method as part of autodiscovery
        m.post('https://redirected.%s/EWS/Exchange.asmx' % domain, status_code=200)

        m.post(ad_endpoint, [
            dict(status_code=200, content=redirect_addr_content),
            dict(status_code=200, content=settings_content),
        ])
        ad_response, p = discovery.discover()
        self.assertEqual(ad_response.autodiscover_smtp_address, 'redirected@%s' % domain)
        self.assertEqual(ad_response.protocol.ews_url, 'https://redirected.%s/EWS/Exchange.asmx' % domain)

        # Test that we catch circular redirects on the same domain with a primed cache. Just mock the endpoint to
        # return the same redirect response on every request.
        self.assertEqual(len(autodiscover_cache), 1)
        m.post(ad_endpoint, status_code=200, content=b'''\
<?xml version="1.0" encoding="utf-8"?>
<Autodiscover xmlns="http://schemas.microsoft.com/exchange/autodiscover/responseschema/2006">
    <Response xmlns="http://schemas.microsoft.com/exchange/autodiscover/outlook/responseschema/2006a">
        <Account>
            <Action>redirectAddr</Action>
            <RedirectAddr>foo@%s</RedirectAddr>
        </Account>
    </Response>
</Autodiscover>''' % domain.encode())
        self.assertEqual(len(autodiscover_cache), 1)
        with self.assertRaises(AutoDiscoverCircularRedirect):
            discovery.discover()

        # Test that we also catch circular redirects when cache is empty
        clear_cache()
        self.assertEqual(len(autodiscover_cache), 0)
        with self.assertRaises(AutoDiscoverCircularRedirect):
            discovery.discover()

        # Test that we can handle being asked to redirect to an address on a different domain
        m.post(ad_endpoint, status_code=200, content=b'''\
<?xml version="1.0" encoding="utf-8"?>
<Autodiscover xmlns="http://schemas.microsoft.com/exchange/autodiscover/responseschema/2006">
    <Response xmlns="http://schemas.microsoft.com/exchange/autodiscover/outlook/responseschema/2006a">
        <Account>
            <Action>redirectAddr</Action>
            <RedirectAddr>john@example.com</RedirectAddr>
        </Account>
    </Response>
</Autodiscover>''')
        m.post('https://example.com/Autodiscover/Autodiscover.xml', status_code=200, content=b'''\
<?xml version="1.0" encoding="utf-8"?>
<Autodiscover xmlns="http://schemas.microsoft.com/exchange/autodiscover/responseschema/2006">
    <Response xmlns="http://schemas.microsoft.com/exchange/autodiscover/outlook/responseschema/2006a">
        <User>
            <AutoDiscoverSMTPAddress>john@redirected.example.com</AutoDiscoverSMTPAddress>
        </User>
        <Account>
            <AccountType>email</AccountType>
            <Action>settings</Action>
            <Protocol>
                <Type>EXPR</Type>
                <EwsUrl>https://redirected.example.com/EWS/Exchange.asmx</EwsUrl>
            </Protocol>
        </Account>
    </Response>
</Autodiscover>''')
        # Also mock the EWS URL. We try to guess its auth method as part of autodiscovery
        m.post('https://redirected.example.com/EWS/Exchange.asmx', status_code=200)
        ad_response, p = discovery.discover()
        self.assertEqual(ad_response.autodiscover_smtp_address, 'john@redirected.example.com')
        self.assertEqual(ad_response.protocol.ews_url, 'https://redirected.example.com/EWS/Exchange.asmx')

    def test_get_srv_records(self):
        from exchangelib.autodiscover.discovery import _get_srv_records, SrvRecord
        # Unknown domain
        self.assertEqual(_get_srv_records('example.XXXXX'), [])
        # No SRV record
        self.assertEqual(_get_srv_records('example.com'), [])
        # Finding a real server that has a correct SRV record is not easy. Mock it
        _orig = dns.resolver.Resolver

        class _Mock1:
            def query(self, hostname, cat):
                class A:
                    def to_text(self):
                        # Return a valid record
                        return '1 2 3 example.com.'
                return [A()]

        dns.resolver.Resolver = _Mock1
        # Test a valid record
        self.assertEqual(_get_srv_records('example.com.'), [SrvRecord(priority=1, weight=2, port=3, srv='example.com')])

        class _Mock2:
            def query(self, hostname, cat):
                class A:
                    def to_text(self):
                        # Return malformed data
                        return 'XXXXXXX'
                return [A()]

        dns.resolver.Resolver = _Mock2
        # Test an invalid record
        self.assertEqual(_get_srv_records('example.com'), [])
        dns.resolver.Resolver = _orig

    def test_select_srv_host(self):
        from exchangelib.autodiscover.discovery import _select_srv_host, SrvRecord
        with self.assertRaises(ValueError):
            # Empty list
            _select_srv_host([])
        with self.assertRaises(ValueError):
            # No records with TLS port
            _select_srv_host([SrvRecord(priority=1, weight=2, port=3, srv='example.com')])
        # One record
        self.assertEqual(
            _select_srv_host([SrvRecord(priority=1, weight=2, port=443, srv='example.com')]),
            'example.com'
        )
        # Highest priority record
        self.assertEqual(
            _select_srv_host([
                SrvRecord(priority=10, weight=2, port=443, srv='10.example.com'),
                SrvRecord(priority=1, weight=2, port=443, srv='1.example.com'),
            ]),
            '10.example.com'
        )
        # Highest priority record no matter how it's sorted
        self.assertEqual(
            _select_srv_host([
                SrvRecord(priority=1, weight=2, port=443, srv='1.example.com'),
                SrvRecord(priority=10, weight=2, port=443, srv='10.example.com'),
            ]),
            '10.example.com'
        )

    def test_parse_response(self):
        with self.assertRaises(ValueError):
            Autodiscover.from_bytes(b'XXX')  # Invalid response

        xml = b'''<?xml version="1.0" encoding="utf-8"?><foo>bar</foo>'''
        with self.assertRaises(ValueError):
            Autodiscover.from_bytes(xml)  # Invalid XML response

        # Redirect to different email address
        xml = b'''\
<?xml version="1.0" encoding="utf-8"?>
<Autodiscover xmlns="http://schemas.microsoft.com/exchange/autodiscover/responseschema/2006">
    <Response xmlns="http://schemas.microsoft.com/exchange/autodiscover/outlook/responseschema/2006a">
        <User>
            <AutoDiscoverSMTPAddress>john@demo.affect-it.dk</AutoDiscoverSMTPAddress>
        </User>
        <Account>
            <Action>redirectAddr</Action>
            <RedirectAddr>foo@example.com</RedirectAddr>
        </Account>
    </Response>
</Autodiscover>'''
        self.assertEqual(Autodiscover.from_bytes(xml).response.redirect_address, 'foo@example.com')

        # Redirect to different URL
        xml = b'''\
<?xml version="1.0" encoding="utf-8"?>
<Autodiscover xmlns="http://schemas.microsoft.com/exchange/autodiscover/responseschema/2006">
    <Response xmlns="http://schemas.microsoft.com/exchange/autodiscover/outlook/responseschema/2006a">
        <User>
            <AutoDiscoverSMTPAddress>john@demo.affect-it.dk</AutoDiscoverSMTPAddress>
        </User>
        <Account>
            <Action>redirectUrl</Action>
            <RedirectURL>https://example.com/foo.asmx</RedirectURL>
        </Account>
    </Response>
</Autodiscover>'''
        self.assertEqual(Autodiscover.from_bytes(xml).response.redirect_url, 'https://example.com/foo.asmx')

        # Select EXPR if it's there, and there are multiple available
        xml = b'''\
<?xml version="1.0" encoding="utf-8"?>
<Autodiscover xmlns="http://schemas.microsoft.com/exchange/autodiscover/responseschema/2006">
    <Response xmlns="http://schemas.microsoft.com/exchange/autodiscover/outlook/responseschema/2006a">
        <User>
            <AutoDiscoverSMTPAddress>john@demo.affect-it.dk</AutoDiscoverSMTPAddress>
        </User>
        <Account>
            <AccountType>email</AccountType>
            <Action>settings</Action>
            <Protocol>
                <Type>EXCH</Type>
                <EwsUrl>https://exch.example.com/EWS/Exchange.asmx</EwsUrl>
            </Protocol>
            <Protocol>
                <Type>EXPR</Type>
                <EwsUrl>https://expr.example.com/EWS/Exchange.asmx</EwsUrl>
            </Protocol>
        </Account>
    </Response>
</Autodiscover>'''
        self.assertEqual(
            Autodiscover.from_bytes(xml).response.protocol.ews_url,
            'https://expr.example.com/EWS/Exchange.asmx'
        )

        # Select EXPR if EXPR is unavailable
        xml = b'''\
<?xml version="1.0" encoding="utf-8"?>
<Autodiscover xmlns="http://schemas.microsoft.com/exchange/autodiscover/responseschema/2006">
    <Response xmlns="http://schemas.microsoft.com/exchange/autodiscover/outlook/responseschema/2006a">
        <User>
            <AutoDiscoverSMTPAddress>john@demo.affect-it.dk</AutoDiscoverSMTPAddress>
        </User>
        <Account>
            <AccountType>email</AccountType>
            <Action>settings</Action>
            <Protocol>
                <Type>EXCH</Type>
                <EwsUrl>https://exch.example.com/EWS/Exchange.asmx</EwsUrl>
            </Protocol>
        </Account>
    </Response>
</Autodiscover>'''
        self.assertEqual(
            Autodiscover.from_bytes(xml).response.protocol.ews_url,
            'https://exch.example.com/EWS/Exchange.asmx'
        )

        # Fail if neither EXPR nor EXPR are unavailable
        xml = b'''\
<?xml version="1.0" encoding="utf-8"?>
<Autodiscover xmlns="http://schemas.microsoft.com/exchange/autodiscover/responseschema/2006">
    <Response xmlns="http://schemas.microsoft.com/exchange/autodiscover/outlook/responseschema/2006a">
        <User>
            <AutoDiscoverSMTPAddress>john@demo.affect-it.dk</AutoDiscoverSMTPAddress>
        </User>
        <Account>
            <AccountType>email</AccountType>
            <Action>settings</Action>
            <Protocol>
                <Type>XXX</Type>
                <EwsUrl>https://xxx.example.com/EWS/Exchange.asmx</EwsUrl>
            </Protocol>
        </Account>
    </Response>
</Autodiscover>'''
        with self.assertRaises(ValueError):
            Autodiscover.from_bytes(xml).response.protocol

    def test_disable_ssl_verification(self):
        if not self.verify_ssl:
            # We can only run this test if we haven't already disabled TLS
            raise self.skipTest('TLS verification already disabled')

        default_adapter_cls = BaseProtocol.HTTP_ADAPTER_CLS

        # A normal discover should succeed
        discovery = Autodiscovery(
            email=self.account.primary_smtp_address,
            credentials=self.account.protocol.credentials,
            retry_policy=self.retry_policy,
        )
        clear_cache()
        discovery.discover()

        # Smash TLS verification using an untrusted certificate
        with tempfile.NamedTemporaryFile() as f:
            f.write(b'''\
 -----BEGIN CERTIFICATE-----
MIIENzCCAx+gAwIBAgIJAOYfYfw7NCOcMA0GCSqGSIb3DQEBBQUAMIGxMQswCQYD
VQQGEwJVUzERMA8GA1UECAwITWFyeWxhbmQxFDASBgNVBAcMC0ZvcmVzdCBIaWxs
MScwJQYDVQQKDB5UaGUgQXBhY2hlIFNvZnR3YXJlIEZvdW5kYXRpb24xFjAUBgNV
BAsMDUFwYWNoZSBUaHJpZnQxEjAQBgNVBAMMCWxvY2FsaG9zdDEkMCIGCSqGSIb3
DQEJARYVZGV2QHRocmlmdC5hcGFjaGUub3JnMB4XDTE0MDQwNzE4NTgwMFoXDTIy
MDYyNDE4NTgwMFowgbExCzAJBgNVBAYTAlVTMREwDwYDVQQIDAhNYXJ5bGFuZDEU
MBIGA1UEBwwLRm9yZXN0IEhpbGwxJzAlBgNVBAoMHlRoZSBBcGFjaGUgU29mdHdh
cmUgRm91bmRhdGlvbjEWMBQGA1UECwwNQXBhY2hlIFRocmlmdDESMBAGA1UEAwwJ
bG9jYWxob3N0MSQwIgYJKoZIhvcNAQkBFhVkZXZAdGhyaWZ0LmFwYWNoZS5vcmcw
ggEiMA0GCSqGSIb3DQEBAQUAA4IBDwAwggEKAoIBAQCqE9TE9wEXp5LRtLQVDSGQ
GV78+7ZtP/I/ZaJ6Q6ZGlfxDFvZjFF73seNhAvlKlYm/jflIHYLnNOCySN8I2Xw6
L9MbC+jvwkEKfQo4eDoxZnOZjNF5J1/lZtBeOowMkhhzBMH1Rds351/HjKNg6ZKg
2Cldd0j7HbDtEixOLgLbPRpBcaYrLrNMasf3Hal+x8/b8ue28x93HSQBGmZmMIUw
AinEu/fNP4lLGl/0kZb76TnyRpYSPYojtS6CnkH+QLYnsRREXJYwD1Xku62LipkX
wCkRTnZ5nUsDMX6FPKgjQFQCWDXG/N096+PRUQAChhrXsJ+gF3NqWtDmtrhVQF4n
AgMBAAGjUDBOMB0GA1UdDgQWBBQo8v0wzQPx3EEexJPGlxPK1PpgKjAfBgNVHSME
GDAWgBQo8v0wzQPx3EEexJPGlxPK1PpgKjAMBgNVHRMEBTADAQH/MA0GCSqGSIb3
DQEBBQUAA4IBAQBGFRiJslcX0aJkwZpzTwSUdgcfKbpvNEbCNtVohfQVTI4a/oN5
U+yqDZJg3vOaOuiAZqyHcIlZ8qyesCgRN314Tl4/JQ++CW8mKj1meTgo5YFxcZYm
T9vsI3C+Nzn84DINgI9mx6yktIt3QOKZRDpzyPkUzxsyJ8J427DaimDrjTR+fTwD
1Dh09xeeMnSa5zeV1HEDyJTqCXutLetwQ/IyfmMBhIx+nvB5f67pz/m+Dv6V0r3I
p4HCcdnDUDGJbfqtoqsAATQQWO+WWuswB6mOhDbvPTxhRpZq6AkgWqv4S+u3M2GO
r5p9FrBgavAw5bKO54C0oQKpN/5fta5l6Ws0
-----END CERTIFICATE-----''')
            try:
                os.environ['REQUESTS_CA_BUNDLE'] = f.name

                # Now discover should fail. TLS errors mean we exhaust all autodiscover attempts
                clear_cache()
                with self.assertRaises(AutoDiscoverFailed):
                    discovery.clear()
                    discovery.discover()

                # Disable insecure TLS warnings
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore")
                    # Make sure we can handle TLS validation errors when using the custom adapter
                    BaseProtocol.HTTP_ADAPTER_CLS = NoVerifyHTTPAdapter
                    clear_cache()
                    discovery.clear()
                    discovery.discover()

                    # Test that the custom adapter also works when validation is OK again
                    del os.environ['REQUESTS_CA_BUNDLE']
                    clear_cache()
                    discovery.clear()
                    discovery.discover()
            finally:
                # Reset environment
                os.environ.pop('REQUESTS_CA_BUNDLE', None)  # May already have been deleted
                BaseProtocol.HTTP_ADAPTER_CLS = default_adapter_cls
