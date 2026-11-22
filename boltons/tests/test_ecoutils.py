import os
import socket
import sys

from boltons import ecoutils


def test_basic():
    # basic sanity test
    prof = ecoutils.get_profile()

    assert prof['python']['bin'] == sys.executable


def test_scrub():
    prof = ecoutils.get_profile(scrub=True)

    assert prof['username'] == '-'
    assert prof['hostname'] == '-'
    assert prof['hostfqdn'] == '-'
    assert prof['cwd'] == '-'
    assert prof['uname']['node'] == '-'
    assert prof['python']['bin'] == '-'
    assert prof['python']['argv'] == '-'


